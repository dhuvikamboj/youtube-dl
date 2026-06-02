from __future__ import annotations

import os
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
from urllib.parse import urlparse

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
CONTROLLED_STATUSES = {"queued", "downloading", "processing", "paused"}


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
                playlist INTEGER NOT NULL DEFAULT 0,
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
        if "logs" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN logs TEXT NOT NULL DEFAULT ''")


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
    return job


def create_job(url: str, kind: str, quality: str, playlist: bool) -> str:
    job_id = uuid.uuid4().hex
    now = utc_now()
    with db_lock, get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, url, kind, quality, playlist, status, progress,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, url, kind, quality, int(playlist), "queued", "Waiting in queue", now, now),
        )
    return job_id


def get_job_by_id(job_id: str) -> dict[str, Any] | None:
    with db_lock, get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return row_to_job(row) if row else None


def list_jobs(limit: int = 25) -> list[dict[str, Any]]:
    with db_lock, get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [row_to_job(row) for row in rows]


def update_job(job_id: str, **changes: Any) -> None:
    if not changes:
        return
    changes["updated_at"] = utc_now()
    assignments = ", ".join(f"{key} = ?" for key in changes)
    values = list(changes.values())
    values.append(job_id)
    with db_lock, get_db() as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", values)


def append_job_log(job_id: str, message: str) -> None:
    line = f"[{utc_now()}] {message}".strip()
    with db_lock, get_db() as conn:
        row = conn.execute("SELECT logs FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return
        logs = "\n".join((row["logs"], line)).strip()
        conn.execute("UPDATE jobs SET logs = ?, updated_at = ? WHERE id = ?", (logs[-12000:], utc_now(), job_id))


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
    for path in sorted(DOWNLOAD_DIR.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.is_file() and not path.name.endswith(".part"):
            files.append(
                {
                    "name": path.name,
                    "size": path.stat().st_size,
                    "url": f"/downloads/{path.name}",
                }
            )
    return files


def disk_usage() -> dict[str, int]:
    usage = shutil.disk_usage(DOWNLOAD_DIR)
    downloads_size = sum(path.stat().st_size for path in DOWNLOAD_DIR.iterdir() if path.is_file())
    return {
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "downloads": downloads_size,
    }


def safe_download_path(filename: str) -> Path | None:
    candidate = (DOWNLOAD_DIR / filename).resolve()
    if candidate.parent != DOWNLOAD_DIR or not candidate.is_file():
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
        bits = [bit for bit in (percent, speed, f"ETA {eta}" if eta else "") if bit]
        update_job(job_id, status="downloading", progress=" | ".join(bits) or "Downloading")
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

    filename_template = "%(playlist_index)03d - %(title).180B [%(id)s].%(ext)s" if job["playlist"] else "%(title).180B [%(id)s].%(ext)s"
    output_template = str(DOWNLOAD_DIR / filename_template)
    options: dict[str, Any] = {
        "outtmpl": output_template,
        "restrictfilenames": True,
        "noplaylist": not job["playlist"],
        "ignoreerrors": job["playlist"],
        "skip_unavailable_fragments": True,
        "logger": JobLogger(job_id),
        "progress_hooks": [lambda data: progress_hook(job_id, data)],
    }

    if job["kind"] == "audio":
        options.update(
            {
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": job["quality"],
                    }
                ],
            }
        )
    else:
        options.update({"format": video_format(job["quality"])})

    update_job(job_id, status="downloading", progress="Starting download")
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(job["url"], download=True)
            if info is None:
                raise RuntimeError("No downloadable entries were found.")
            if job["playlist"]:
                final_name = "Playlist complete"
            else:
                prepared = Path(ydl.prepare_filename(info))
                final_name = prepared.with_suffix(".mp3").name if job["kind"] == "audio" else prepared.name
        update_job(job_id, status="complete", progress="Complete", filename=final_name, error=None)
    except DownloadCanceled:
        append_job_log(job_id, "Download canceled by user.")
        update_job(job_id, status="canceled", progress="Canceled")
    except Exception as exc:  # yt-dlp exposes several exception types; show the useful message.
        append_job_log(job_id, str(exc))
        update_job(job_id, status="error", progress="Failed", error=str(exc))


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


@app.get("/")
def index():
    return render_template(
        "index.html",
        downloads=list_downloads(),
        jobs=list_jobs(),
        disk=disk_usage(),
        video_formats=VIDEO_FORMATS.keys(),
        audio_qualities=sorted(AUDIO_QUALITIES, key=int),
        max_workers=MAX_CONCURRENT_DOWNLOADS,
    )


@app.post("/api/downloads")
def create_download():
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url", "")).strip()
    kind = str(payload.get("kind", "video")).strip()
    quality = str(payload.get("quality", "best")).strip()
    playlist = bool(payload.get("playlist", False))

    if kind not in {"video", "audio"}:
        return jsonify({"error": "Choose either video or audio."}), 400
    if kind == "video" and quality not in VIDEO_FORMATS:
        return jsonify({"error": "Choose a valid video quality."}), 400
    if kind == "audio" and quality not in AUDIO_QUALITIES:
        return jsonify({"error": "Choose a valid audio quality."}), 400
    if not is_allowed_url(url):
        return jsonify({"error": "Enter a valid YouTube URL."}), 400

    job_id = create_job(url, kind, quality, playlist)
    executor.submit(run_download, job_id)
    return jsonify({"id": job_id}), 202


@app.get("/api/jobs")
def get_jobs():
    return jsonify({"jobs": list_jobs()})


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


@app.get("/api/downloads")
def get_downloads():
    return jsonify({"downloads": list_downloads(), "disk": disk_usage()})


@app.delete("/api/downloads/<path:filename>")
def delete_download(filename: str):
    path = safe_download_path(filename)
    if not path:
        return jsonify({"error": "File not found."}), 404
    path.unlink()
    return jsonify({"ok": True, "downloads": list_downloads(), "disk": disk_usage()})


@app.get("/downloads/<path:filename>")
def download_file(filename: str):
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True)


@app.cli.command("hash-password")
def hash_password_command():
    password = input("Password: ")
    print(generate_password_hash(password))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
