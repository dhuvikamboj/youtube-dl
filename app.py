from __future__ import annotations

import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash
from yt_dlp import YoutubeDL


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", BASE_DIR / "downloads")).resolve()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", BASE_DIR / "downloads.sqlite3")).resolve()
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "1"))
APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "changeme")
APP_PASSWORD_HASH = os.environ.get("APP_PASSWORD_HASH")
ARCHIVE_FILE = DOWNLOAD_DIR / ".ytdl-archive.txt"


def extract_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.hostname == "youtu.be":
        return parsed.path.lstrip("/").split("?")[0] or None
    return parse_qs(parsed.query).get("v", [None])[0]


def extract_playlist_id(url: str) -> str | None:
    parsed = urlparse(url)
    pl_id = parse_qs(parsed.query).get("list", [None])[0]
    if pl_id and re.match(r"^[A-Za-z0-9_\-]+$", pl_id):
        return pl_id
    return None


def check_archive(video_id: str) -> bool:
    if not ARCHIVE_FILE.exists():
        return False
    needle = f"youtube {video_id}"
    with ARCHIVE_FILE.open() as f:
        return any(line.strip() == needle for line in f)


def remove_from_archive(video_id: str) -> bool:
    if not ARCHIVE_FILE.exists():
        return False
    needle = f"youtube {video_id}"
    lines = ARCHIVE_FILE.read_text().splitlines(keepends=True)
    new_lines = [l for l in lines if l.strip() != needle]
    if len(new_lines) == len(lines):
        return False
    ARCHIVE_FILE.write_text("".join(new_lines))
    return True


def find_on_disk(video_id: str) -> list[str]:
    return [
        str(p.relative_to(DOWNLOAD_DIR))
        for p in DOWNLOAD_DIR.rglob("*")
        if p.is_file() and not p.name.startswith(".") and f"[{video_id}]" in p.name
    ]

ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}

VIDEO_FORMATS = {
    "best": "bestvideo+bestaudio/best",
    "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
}
AUDIO_QUALITIES = {"128", "192", "320"}
AUDIO_FORMATS = {"mp3", "m4a", "opus", "wav"}
CONTROLLED_STATUSES = {"queued", "downloading", "processing", "paused"}
TERMINAL_STATUSES = {"complete", "error", "canceled"}
PLAYLIST_ITEMS_RE = re.compile(r"^[\d,\-]+$")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS)
db_lock = threading.Lock()


class DownloadCanceled(Exception):
    pass


class JobLogger:
    def __init__(self, job_id: str):
        self.job_id = job_id

    def debug(self, message: str) -> None:
        if message.startswith("[debug]"):
            append_job_log(self.job_id, message)

    def warning(self, message: str) -> None:
        append_job_log(self.job_id, f"WARNING: {message}")

    def error(self, message: str) -> None:
        append_job_log(self.job_id, f"ERROR: {message}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                kind TEXT NOT NULL,
                quality TEXT NOT NULL,
                audio_format TEXT NOT NULL DEFAULT 'mp3',
                playlist INTEGER NOT NULL DEFAULT 0,
                playlist_items TEXT,
                embed_meta INTEGER NOT NULL DEFAULT 0,
                embed_thumb INTEGER NOT NULL DEFAULT 0,
                subtitles INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                progress TEXT NOT NULL,
                filename TEXT,
                error TEXT,
                logs TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        migrations = {
            "logs": "TEXT NOT NULL DEFAULT ''",
            "audio_format": "TEXT NOT NULL DEFAULT 'mp3'",
            "playlist_items": "TEXT",
            "embed_meta": "INTEGER NOT NULL DEFAULT 0",
            "embed_thumb": "INTEGER NOT NULL DEFAULT 0",
            "subtitles": "INTEGER NOT NULL DEFAULT 0",
            "force": "INTEGER NOT NULL DEFAULT 0",
        }
        for col, definition in migrations.items():
            if col not in columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {definition}")


init_db()


def is_logged_in() -> bool:
    return bool(session.get("logged_in"))


def verify_password(password: str) -> bool:
    if APP_PASSWORD_HASH:
        return check_password_hash(APP_PASSWORD_HASH, password)
    return secrets.compare_digest(password, APP_PASSWORD)


@app.before_request
def require_login():
    allowed = {"login", "login_post", "static"}
    if request.endpoint in allowed or is_logged_in():
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "Login required."}), 401
    return redirect(url_for("login"))


def is_allowed_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.hostname in ALLOWED_HOSTS


def row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    job = dict(row)
    job["playlist"] = bool(job["playlist"])
    job["embed_meta"] = bool(job.get("embed_meta", 0))
    job["embed_thumb"] = bool(job.get("embed_thumb", 0))
    job["subtitles"] = bool(job.get("subtitles", 0))
    job["force"] = bool(job.get("force", 0))
    return job


def create_job(
    url: str,
    kind: str,
    quality: str,
    audio_format: str,
    playlist: bool,
    playlist_items: str | None,
    embed_meta: bool,
    embed_thumb: bool,
    subtitles: bool,
    force: bool = False,
) -> str:
    job_id = uuid.uuid4().hex
    now = utc_now()
    with db_lock, get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, url, kind, quality, audio_format, playlist, playlist_items,
                embed_meta, embed_thumb, subtitles, force,
                status, progress, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id, url, kind, quality, audio_format,
                int(playlist), playlist_items or None,
                int(embed_meta), int(embed_thumb), int(subtitles), int(force),
                "queued", "Waiting in queue", now, now,
            ),
        )
    return job_id


def get_job_by_id(job_id: str) -> dict[str, Any] | None:
    with db_lock, get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return row_to_job(row) if row else None


def list_jobs(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    with db_lock, get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [row_to_job(row) for row in rows]


def count_jobs() -> int:
    with db_lock, get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


def update_job(job_id: str, **changes: Any) -> None:
    if not changes:
        return
    changes["updated_at"] = utc_now()
    assignments = ", ".join(f"{key} = ?" for key in changes)
    values = list(changes.values())
    values.append(job_id)
    with db_lock, get_db() as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", values)


def delete_job_by_id(job_id: str) -> bool:
    with db_lock, get_db() as conn:
        cursor = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return cursor.rowcount > 0


def clear_finished_jobs() -> int:
    with db_lock, get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM jobs WHERE status IN ('complete', 'error', 'canceled')"
        )
        return cursor.rowcount


def append_job_log(job_id: str, message: str) -> None:
    line = f"[{utc_now()}] {message}".strip()
    with db_lock, get_db() as conn:
        row = conn.execute("SELECT logs FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return
        logs = "\n".join((row["logs"], line)).strip()
        conn.execute(
            "UPDATE jobs SET logs = ?, updated_at = ? WHERE id = ?",
            (logs[-12000:], utc_now(), job_id),
        )


def wait_while_paused(job_id: str) -> None:
    while True:
        job = get_job_by_id(job_id)
        if not job or job["status"] == "canceled":
            raise DownloadCanceled("Download canceled.")
        if job["status"] != "paused":
            return
        time.sleep(0.5)


def list_downloads() -> list[dict[str, Any]]:
    files = []
    for path in sorted(DOWNLOAD_DIR.rglob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.is_file() and not path.name.endswith(".part") and not path.name.startswith("."):
            rel = path.relative_to(DOWNLOAD_DIR)
            files.append({"name": str(rel), "size": path.stat().st_size, "url": f"/downloads/{rel}"})
    return files


def disk_usage() -> dict[str, int]:
    usage = shutil.disk_usage(DOWNLOAD_DIR)
    downloads_size = sum(
        p.stat().st_size for p in DOWNLOAD_DIR.rglob("*") if p.is_file() and not p.name.startswith(".")
    )
    return {"total": usage.total, "used": usage.used, "free": usage.free, "downloads": downloads_size}


def safe_download_path(filename: str) -> Path | None:
    candidate = (DOWNLOAD_DIR / filename).resolve()
    if not str(candidate).startswith(str(DOWNLOAD_DIR) + os.sep) or not candidate.is_file():
        return None
    return candidate


def progress_hook(job_id: str, data: dict[str, Any]) -> None:
    wait_while_paused(job_id)
    job = get_job_by_id(job_id)
    if not job or job["status"] == "canceled":
        raise DownloadCanceled("Download canceled.")

    status = data.get("status")
    if status == "downloading":
        percent = data.get("_percent_str", "").strip()
        speed = data.get("_speed_str", "").strip()
        eta = data.get("_eta_str", "").strip()

        info = data.get("info_dict", {})
        playlist_index = info.get("playlist_index")
        n_entries = info.get("n_entries")
        title = info.get("title", "")

        prefix = ""
        if playlist_index and n_entries:
            short = title[:35] + "…" if len(title) > 35 else title
            prefix = f"{playlist_index}/{n_entries} — {short}: " if short else f"{playlist_index}/{n_entries}: "

        bits = [b for b in (percent, speed, f"ETA {eta}" if eta else "") if b]
        update_job(job_id, status="downloading", progress=prefix + (" | ".join(bits) or "Downloading"))
    elif status == "finished":
        filename = Path(data.get("filename", "")).name or None
        update_job(job_id, status="processing", progress="Processing file", filename=filename)


def video_format(quality: str) -> str:
    return VIDEO_FORMATS.get(quality, VIDEO_FORMATS["best"])


def run_download(job_id: str) -> None:
    job = get_job_by_id(job_id)
    if not job:
        return
    try:
        wait_while_paused(job_id)
    except DownloadCanceled:
        update_job(job_id, status="canceled", progress="Canceled")
        return

    is_playlist = job["playlist"]
    audio_fmt = job.get("audio_format") or "mp3"

    if is_playlist:
        output_template = str(DOWNLOAD_DIR / "%(playlist_title)s" / "%(playlist_index)03d - %(title).180B [%(id)s].%(ext)s")
    else:
        output_template = str(DOWNLOAD_DIR / "%(title).180B [%(id)s].%(ext)s")

    postprocessors: list[dict[str, Any]] = []

    if job["kind"] == "audio":
        format_str = "bestaudio/best"
        postprocessors.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_fmt,
            "preferredquality": job["quality"],
        })
    else:
        format_str = video_format(job["quality"])

    if job["embed_meta"]:
        postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})

    if job["embed_thumb"] and audio_fmt != "wav":
        postprocessors.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg"})
        postprocessors.append({"key": "EmbedThumbnail"})

    finished_count: dict[str, int] = {"n": 0}

    def _progress_hook(data: dict[str, Any]) -> None:
        if data.get("status") == "finished":
            finished_count["n"] += 1
        progress_hook(job_id, data)

    options: dict[str, Any] = {
        "outtmpl": output_template,
        "restrictfilenames": True,
        "noplaylist": not is_playlist,
        "ignoreerrors": is_playlist,
        "skip_unavailable_fragments": True,
        "logger": JobLogger(job_id),
        "progress_hooks": [_progress_hook],
        "format": format_str,
        "postprocessors": postprocessors,
    }

    if job.get("force"):
        options["force_overwrites"] = True
    elif is_playlist:
        playlist_id = extract_playlist_id(job["url"])
        if playlist_id:
            options["download_archive"] = str(DOWNLOAD_DIR / f".archive-{playlist_id}.txt")
    else:
        options["download_archive"] = str(ARCHIVE_FILE)

    if job["embed_thumb"] and audio_fmt != "wav":
        options["writethumbnail"] = True

    if job.get("playlist_items"):
        options["playlist_items"] = job["playlist_items"]

    if job["subtitles"]:
        options["writesubtitles"] = True
        options["writeautomaticsub"] = True
        options["subtitlesformat"] = "srt/vtt/best"
        options["subtitleslangs"] = ["en", "en-US"]

    update_job(job_id, status="downloading", progress="Starting download")
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(job["url"], download=True)
            if info is None:
                raise RuntimeError("No downloadable entries were found.")
            if is_playlist:
                final_name = "Playlist complete"
            else:
                prepared = Path(ydl.prepare_filename(info))
                final_name = prepared.with_suffix(f".{audio_fmt}").name if job["kind"] == "audio" else prepared.name
        if not is_playlist and finished_count["n"] == 0:
            update_job(job_id, status="skipped", progress="Already downloaded — skipped by archive", filename=final_name, error=None)
        else:
            update_job(job_id, status="complete", progress="Complete", filename=final_name, error=None)
    except DownloadCanceled:
        append_job_log(job_id, "Download canceled by user.")
        update_job(job_id, status="canceled", progress="Canceled")
    except Exception as exc:
        append_job_log(job_id, str(exc))
        update_job(job_id, status="error", progress="Failed", error=str(exc))


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.get("/login")
def login():
    return render_template("login.html")


@app.post("/login")
def login_post():
    username = str(request.form.get("username", "")).strip()
    password = str(request.form.get("password", ""))
    if secrets.compare_digest(username, APP_USERNAME) and verify_password(password):
        session.clear()
        session["logged_in"] = True
        return redirect(url_for("index"))
    return render_template("login.html", error="Invalid username or password."), 401


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return render_template(
        "index.html",
        downloads=list_downloads(),
        jobs=list_jobs(),
        total_jobs=count_jobs(),
        disk=disk_usage(),
        video_formats=VIDEO_FORMATS.keys(),
        audio_qualities=sorted(AUDIO_QUALITIES, key=int),
        audio_formats=sorted(AUDIO_FORMATS),
        max_workers=MAX_CONCURRENT_DOWNLOADS,
    )


# ── Download jobs ─────────────────────────────────────────────────────────────

@app.post("/api/downloads")
def create_download():
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url", "")).strip()
    kind = str(payload.get("kind", "video")).strip()
    quality = str(payload.get("quality", "best")).strip()
    audio_format = str(payload.get("audio_format", "mp3")).strip()
    playlist = bool(payload.get("playlist", False))
    playlist_items = re.sub(r"\s+", "", str(payload.get("playlist_items", ""))).strip() or None
    embed_meta = bool(payload.get("embed_meta", False))
    embed_thumb = bool(payload.get("embed_thumb", False))
    subtitles = bool(payload.get("subtitles", False))
    force = bool(payload.get("force", False))

    if kind not in {"video", "audio"}:
        return jsonify({"error": "Choose either video or audio."}), 400
    if kind == "video" and quality not in VIDEO_FORMATS:
        return jsonify({"error": "Choose a valid video quality."}), 400
    if kind == "audio" and quality not in AUDIO_QUALITIES:
        return jsonify({"error": "Choose a valid audio quality."}), 400
    if kind == "audio" and audio_format not in AUDIO_FORMATS:
        return jsonify({"error": "Choose a valid audio format."}), 400
    if not is_allowed_url(url):
        return jsonify({"error": "Enter a valid YouTube URL."}), 400
    if playlist_items and not PLAYLIST_ITEMS_RE.match(playlist_items):
        return jsonify({"error": "Playlist items must be like: 1-10 or 1,3,5"}), 400

    # Dedup: reject if same URL already active
    with db_lock, get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM jobs WHERE url = ? AND status NOT IN ('complete','error','canceled','skipped') LIMIT 1",
            (url,),
        ).fetchone()
    if existing:
        return jsonify({"error": "This URL is already queued or downloading.", "id": existing["id"]}), 409

    if not force and not playlist:
        video_id = extract_video_id(url)
        if video_id:
            on_disk = find_on_disk(video_id)
            if on_disk:
                return jsonify({
                    "error": f"File already exists on disk.",
                    "video_id": video_id,
                    "files": on_disk,
                    "duplicate": True,
                }), 409
            if check_archive(video_id):
                return jsonify({
                    "error": "Already downloaded (found in archive).",
                    "video_id": video_id,
                    "in_archive": True,
                    "duplicate": True,
                }), 409

    job_id = create_job(url, kind, quality, audio_format, playlist, playlist_items, embed_meta, embed_thumb, subtitles, force)
    executor.submit(run_download, job_id)
    return jsonify({"id": job_id}), 202


@app.get("/api/jobs")
def get_jobs():
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))
    return jsonify({"jobs": list_jobs(limit, offset), "total": count_jobs()})


@app.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    job = get_job_by_id(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    return jsonify(job)


@app.post("/api/jobs/<job_id>/pause")
def pause_job(job_id: str):
    job = get_job_by_id(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    if job["status"] not in CONTROLLED_STATUSES:
        return jsonify({"error": f"Cannot pause a {job['status']} job."}), 400
    if job["status"] != "paused":
        append_job_log(job_id, "Pause requested by user.")
        update_job(job_id, status="paused", progress="Paused")
    return jsonify(get_job_by_id(job_id))


@app.post("/api/jobs/<job_id>/resume")
def resume_job(job_id: str):
    job = get_job_by_id(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    if job["status"] != "paused":
        return jsonify({"error": f"Cannot resume a {job['status']} job."}), 400
    append_job_log(job_id, "Resume requested by user.")
    update_job(job_id, status="queued", progress="Resumed; waiting for worker")
    return jsonify(get_job_by_id(job_id))


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id: str):
    job = get_job_by_id(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    if job["status"] not in CONTROLLED_STATUSES:
        return jsonify({"error": f"Cannot cancel a {job['status']} job."}), 400
    append_job_log(job_id, "Cancel requested by user.")
    update_job(job_id, status="canceled", progress="Cancel requested")
    return jsonify(get_job_by_id(job_id))


@app.post("/api/jobs/<job_id>/retry")
def retry_job(job_id: str):
    job = get_job_by_id(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    if job["status"] not in {*TERMINAL_STATUSES, "skipped"}:
        return jsonify({"error": f"Cannot retry a {job['status']} job."}), 400
    force_retry = bool(request.get_json(silent=True) or {}).get("force", job["status"] == "skipped")
    new_id = create_job(
        url=job["url"],
        kind=job["kind"],
        quality=job["quality"],
        audio_format=job.get("audio_format") or "mp3",
        playlist=job["playlist"],
        playlist_items=job.get("playlist_items"),
        embed_meta=job["embed_meta"],
        embed_thumb=job["embed_thumb"],
        subtitles=job["subtitles"],
        force=force_retry,
    )
    executor.submit(run_download, new_id)
    return jsonify({"id": new_id}), 202


@app.delete("/api/jobs/<job_id>")
def delete_job(job_id: str):
    job = get_job_by_id(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    if job["status"] in CONTROLLED_STATUSES:
        return jsonify({"error": "Cancel the job before deleting it."}), 400
    delete_job_by_id(job_id)
    return jsonify({"ok": True})


@app.post("/api/jobs/clear-finished")
def clear_finished():
    removed = clear_finished_jobs()
    return jsonify({"ok": True, "removed": removed, "jobs": list_jobs(), "total": count_jobs()})


# ── File info preview ─────────────────────────────────────────────────────────

@app.get("/api/info")
def video_info():
    url = request.args.get("url", "").strip()
    if not is_allowed_url(url):
        return jsonify({"error": "Invalid YouTube URL."}), 400
    try:
        with YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": "in_playlist"}) as ydl:
            info = ydl.extract_info(url, download=False)
        if info is None:
            return jsonify({"error": "Could not fetch video info."}), 400
        is_pl = info.get("_type") == "playlist"
        return jsonify({
            "title": info.get("title"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader") or info.get("channel"),
            "playlist_count": info.get("playlist_count") or (len(info.get("entries", [])) if is_pl else None),
            "is_playlist": is_pl,
        })
    except Exception as exc:
        return jsonify({"error": str(exc[:300])}), 400


# ── Downloaded files ──────────────────────────────────────────────────────────

@app.get("/api/downloads")
def get_downloads():
    return jsonify({"downloads": list_downloads(), "disk": disk_usage()})


@app.delete("/api/downloads/<path:filename>")
def delete_download(filename: str):
    path = safe_download_path(filename)
    if not path:
        return jsonify({"error": "File not found."}), 404
    path.unlink()
    # Remove empty parent dirs (playlist subfolders)
    try:
        path.parent.relative_to(DOWNLOAD_DIR)
        if path.parent != DOWNLOAD_DIR and not any(path.parent.iterdir()):
            path.parent.rmdir()
    except (ValueError, OSError):
        pass
    return jsonify({"ok": True, "downloads": list_downloads(), "disk": disk_usage()})


@app.get("/downloads/<path:filename>")
def download_file(filename: str):
    path = safe_download_path(filename)
    if not path:
        from flask import abort
        abort(404)
    return send_from_directory(path.parent, path.name, as_attachment=True)


# ── Archive management ────────────────────────────────────────────────────────

@app.get("/api/archive")
def get_archive():
    result: dict[str, list[str]] = {}
    for archive_path in [ARCHIVE_FILE, *DOWNLOAD_DIR.glob(".archive-*.txt")]:
        if not archive_path.exists():
            continue
        entries = [l.strip() for l in archive_path.read_text().splitlines() if l.strip()]
        result[archive_path.name] = entries
    return jsonify({"archives": result})


@app.delete("/api/archive/<video_id>")
def delete_archive_entry(video_id: str):
    if not re.match(r"^[A-Za-z0-9_\-]{6,15}$", video_id):
        return jsonify({"error": "Invalid video ID."}), 400
    removed = remove_from_archive(video_id)
    return jsonify({"ok": True, "removed": removed})


@app.cli.command("hash-password")
def hash_password_command():
    password = input("Password: ")
    print(generate_password_hash(password))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
