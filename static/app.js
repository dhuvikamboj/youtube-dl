const form = document.querySelector("#download-form");
const statusBox = document.querySelector("#status");
const filesList = document.querySelector("#files");
const jobsList = document.querySelector("#jobs");
const diskStats = document.querySelector("#disk");
const diskBar = document.querySelector("#disk-bar");
const refreshButton = document.querySelector("#refresh");
const refreshJobsButton = document.querySelector("#refresh-jobs");
const clearFinishedButton = document.querySelector("#clear-finished");
const videoQuality = document.querySelector("#video-quality");
const audioQuality = document.querySelector("#audio-quality");
const audioFormat = document.querySelector("#audio-format");
const kindInputs = document.querySelectorAll("input[name='kind']");
const playlistCheckbox = document.querySelector("#playlist-checkbox");
const playlistItemsRow = document.querySelector("#playlist-items-row");
const urlInput = document.querySelector("#url");
const infoPreview = document.querySelector("#info-preview");
const loadMoreBtn = document.querySelector("#load-more");

let pollTimer = null;
let jobsOffset = 50;
let totalJobs = 0;

// ── Notifications ─────────────────────────────────────────────────────────────

async function requestNotifications() {
  if ("Notification" in window && Notification.permission === "default") {
    await Notification.requestPermission();
  }
}

function notify(title, body) {
  if ("Notification" in window && Notification.permission === "granted") {
    new Notification(title, { body });
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function setStatus(message, state = "") {
  statusBox.hidden = false;
  statusBox.textContent = message;
  statusBox.dataset.state = state;
}

function formatMb(bytes) {
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatGb(bytes) {
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function formatDuration(seconds) {
  if (!seconds) return "";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function selectedKind() {
  return document.querySelector("input[name='kind']:checked").value;
}

function syncQualityControls() {
  const audio = selectedKind() === "audio";
  videoQuality.disabled = audio;
  audioQuality.disabled = !audio;
  audioFormat.disabled = !audio;
}

function syncPlaylistControls() {
  playlistItemsRow.hidden = !playlistCheckbox.checked;
}

// ── Disk ──────────────────────────────────────────────────────────────────────

function updateDisk(disk) {
  diskStats.innerHTML = "";
  const downloads = document.createElement("span");
  const free = document.createElement("span");
  downloads.textContent = `Downloads: ${formatMb(disk.downloads)}`;
  free.textContent = `Free: ${formatGb(disk.free)}`;
  diskStats.append(downloads, free);
  diskBar.style.width = `${((disk.used / disk.total) * 100).toFixed(1)}%`;
}

// ── Files ─────────────────────────────────────────────────────────────────────

function renderFiles(downloads) {
  filesList.innerHTML = "";
  if (!downloads.length) {
    const item = document.createElement("li");
    item.className = "empty";
    item.textContent = "No downloads yet.";
    filesList.append(item);
    return;
  }
  for (const file of downloads) {
    const item = document.createElement("li");
    const link = document.createElement("a");
    const size = document.createElement("span");
    const remove = document.createElement("button");
    link.href = file.url;
    link.textContent = file.name;
    size.textContent = formatMb(file.size);
    remove.className = "danger";
    remove.type = "button";
    remove.dataset.delete = file.name;
    remove.textContent = "Delete";
    item.append(link, size, remove);
    filesList.append(item);
  }
}

// ── Jobs ──────────────────────────────────────────────────────────────────────

function jobStatusBadge(status) {
  const badge = document.createElement("span");
  badge.className = `badge badge-${status}`;
  badge.textContent = status;
  return badge;
}

function renderJobs(jobs, append = false) {
  if (!append) jobsList.innerHTML = "";

  if (!jobs.length && !append) {
    const item = document.createElement("li");
    item.className = "empty";
    item.textContent = "No jobs yet.";
    jobsList.append(item);
    return;
  }

  for (const job of jobs) {
    const item = document.createElement("li");
    item.dataset.jobId = job.id;
    const body = document.createElement("div");
    const titleRow = document.createElement("div");
    titleRow.className = "job-title-row";
    const title = document.createElement("strong");
    const badge = jobStatusBadge(job.status);

    const kindLabel = `${job.kind} ${job.quality}${job.audio_format && job.kind === "audio" ? ` ${job.audio_format}` : ""}`;
    title.textContent = `${kindLabel}${job.playlist ? " · playlist" : ""}`;

    const progress = document.createElement("span");
    progress.textContent = job.progress;

    const url = document.createElement("small");
    url.textContent = job.url;

    titleRow.append(title, badge);
    body.append(titleRow, progress, url);

    if (job.status === "complete" && job.filename && job.filename !== "Playlist complete") {
      const link = document.createElement("a");
      link.href = `/downloads/${encodeURIComponent(job.filename)}`;
      link.textContent = job.filename;
      link.className = "job-file-link";
      body.append(link);
    } else if (job.status === "complete" && job.filename === "Playlist complete") {
      const note = document.createElement("span");
      note.className = "job-file-link";
      note.textContent = "Playlist complete — see Downloaded files";
      body.append(note);
    }

    if (job.logs) {
      const logs = document.createElement("pre");
      logs.textContent = job.logs;
      body.append(logs);
    }

    const side = document.createElement("div");
    side.className = "job-side";
    const time = document.createElement("time");
    time.textContent = job.created_at;
    const actions = document.createElement("div");
    actions.className = "job-actions";

    if (job.status === "paused") {
      actions.append(jobActionButton(job.id, "resume", "Resume", "secondary"));
    } else if (["queued", "downloading", "processing"].includes(job.status)) {
      actions.append(jobActionButton(job.id, "pause", "Pause", "secondary"));
    }

    if (["queued", "downloading", "processing", "paused"].includes(job.status)) {
      actions.append(jobActionButton(job.id, "cancel", "Cancel", "danger"));
    }

    if (["error", "canceled", "skipped"].includes(job.status)) {
      const retryBtn = jobActionButton(job.id, "retry", job.status === "skipped" ? "Force re-download" : "Retry", "secondary");
      if (job.status === "skipped") retryBtn.dataset.force = "1";
      actions.append(retryBtn);
    }

    if (["complete", "error", "canceled", "skipped"].includes(job.status)) {
      actions.append(jobActionButton(job.id, "delete", "Delete", "danger"));
    }

    side.append(time, actions);
    item.append(body, side);
    jobsList.append(item);
  }
}

function jobActionButton(jobId, action, label, className) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.dataset.jobAction = action;
  button.dataset.jobId = jobId;
  button.textContent = label;
  return button;
}

// ── Polling ───────────────────────────────────────────────────────────────────

function hasActiveJobs(jobs) {
  return jobs.some((j) => ["queued", "downloading", "processing", "paused"].includes(j.status));
}

async function poll() {
  pollTimer = null;
  const res = await fetch("/api/jobs?limit=50");
  const data = await res.json();
  totalJobs = data.total;
  updateLoadMore();
  renderJobs(data.jobs);

  if (hasActiveJobs(data.jobs)) {
    pollTimer = setTimeout(poll, 1500);
  } else {
    const completed = data.jobs.filter((j) => j.status === "complete");
    if (completed.length) {
      await refreshFiles();
      notify("Download complete", completed[completed.length - 1].filename || "");
    }
  }
}

function startPolling() {
  if (!pollTimer) poll();
}

// ── Load more ─────────────────────────────────────────────────────────────────

function updateLoadMore() {
  if (loadMoreBtn) loadMoreBtn.hidden = jobsOffset >= totalJobs;
}

async function loadMoreJobs() {
  const res = await fetch(`/api/jobs?limit=50&offset=${jobsOffset}`);
  const data = await res.json();
  totalJobs = data.total;
  jobsOffset += data.jobs.length;
  renderJobs(data.jobs, true);
  updateLoadMore();
}

// ── Info preview ──────────────────────────────────────────────────────────────

let infoDebounce = null;

async function fetchVideoInfo(url) {
  if (!url || !url.includes("youtube") && !url.includes("youtu.be")) {
    infoPreview.hidden = true;
    return;
  }
  clearTimeout(infoDebounce);
  infoDebounce = setTimeout(async () => {
    infoPreview.hidden = false;
    infoPreview.innerHTML = '<span class="info-loading">Fetching info…</span>';
    try {
      const res = await fetch(`/api/info?url=${encodeURIComponent(url)}`);
      const data = await res.json();
      if (!res.ok) {
        infoPreview.innerHTML = `<span class="info-error">${data.error}</span>`;
        return;
      }
      infoPreview.innerHTML = "";
      if (data.thumbnail) {
        const img = document.createElement("img");
        img.src = data.thumbnail;
        img.className = "info-thumb";
        img.alt = "";
        infoPreview.append(img);
      }
      const meta = document.createElement("div");
      meta.className = "info-meta";
      const titleEl = document.createElement("strong");
      titleEl.textContent = data.title || "Unknown title";
      meta.append(titleEl);
      if (data.uploader) {
        const up = document.createElement("span");
        up.textContent = data.uploader;
        meta.append(up);
      }
      const details = document.createElement("span");
      const parts = [];
      if (data.is_playlist && data.playlist_count) parts.push(`${data.playlist_count} videos`);
      if (data.duration) parts.push(formatDuration(data.duration));
      details.textContent = parts.join(" · ");
      meta.append(details);
      infoPreview.append(meta);
    } catch {
      infoPreview.innerHTML = '<span class="info-error">Could not load info.</span>';
    }
  }, 600);
}

// ── Refresh ───────────────────────────────────────────────────────────────────

async function refreshFiles() {
  const response = await fetch("/api/downloads");
  const data = await response.json();
  renderFiles(data.downloads);
  updateDisk(data.disk);
}

async function refreshJobs() {
  jobsOffset = 50;
  const res = await fetch("/api/jobs?limit=50");
  const data = await res.json();
  totalJobs = data.total;
  updateLoadMore();
  renderJobs(data.jobs);
}

// ── Duplicate handling ────────────────────────────────────────────────────────

function showDuplicateWarning(data, formData) {
  statusBox.hidden = false;
  statusBox.dataset.state = "warn";
  statusBox.innerHTML = "";

  const msg = document.createElement("span");
  if (data.files && data.files.length) {
    msg.textContent = `Already on disk: ${data.files.join(", ")}`;
  } else {
    msg.textContent = `${data.error} (ID: ${data.video_id})`;
  }
  statusBox.append(msg);

  const forceBtn = document.createElement("button");
  forceBtn.type = "button";
  forceBtn.className = "secondary";
  forceBtn.style.marginLeft = "12px";
  forceBtn.textContent = "Force re-download";
  forceBtn.addEventListener("click", async () => {
    forceBtn.disabled = true;
    const kind = formData.get("kind");
    const response = await fetch("/api/downloads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: formData.get("url"),
        kind,
        quality: kind === "audio" ? formData.get("audio_quality") : formData.get("video_quality"),
        audio_format: formData.get("audio_format"),
        playlist: formData.get("playlist") === "1",
        playlist_items: formData.get("playlist") === "1" ? (formData.get("playlist_items") || null) : null,
        embed_meta: formData.get("embed_meta") === "1",
        embed_thumb: formData.get("embed_thumb") === "1",
        subtitles: formData.get("subtitles") === "1",
        force: true,
      }),
    });
    const result = await response.json();
    if (!response.ok) {
      setStatus(result.error || "Could not force re-download.", "error");
      return;
    }
    setStatus("Force re-download queued…");
    jobsOffset = 50;
    startPolling();
  });
  statusBox.append(forceBtn);
}

// ── Form submit ───────────────────────────────────────────────────────────────

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  requestNotifications();
  const formData = new FormData(form);
  const kind = formData.get("kind");

  setStatus("Submitting download…");
  const response = await fetch("/api/downloads", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      url: formData.get("url"),
      kind,
      quality: kind === "audio" ? formData.get("audio_quality") : formData.get("video_quality"),
      audio_format: formData.get("audio_format"),
      playlist: formData.get("playlist") === "1",
      playlist_items: formData.get("playlist") === "1" ? (formData.get("playlist_items") || null) : null,
      embed_meta: formData.get("embed_meta") === "1",
      embed_thumb: formData.get("embed_thumb") === "1",
      subtitles: formData.get("subtitles") === "1",
    }),
  });

  const data = await response.json();
  if (!response.ok) {
    if (response.status === 409 && data.duplicate) {
      showDuplicateWarning(data, new FormData(form));
    } else {
      setStatus(data.error || "Could not start download.", "error");
    }
    return;
  }

  setStatus("Queued — polling for progress…");
  jobsOffset = 50;
  startPolling();
});

// ── File actions ──────────────────────────────────────────────────────────────

filesList.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-delete]");
  if (!button) return;
  const filename = button.dataset.delete;
  if (!window.confirm(`Delete ${filename}?`)) return;
  const response = await fetch(`/api/downloads/${encodeURIComponent(filename)}`, { method: "DELETE" });
  const data = await response.json();
  if (!response.ok) {
    setStatus(data.error || "Could not delete file.", "error");
    return;
  }
  renderFiles(data.downloads);
  updateDisk(data.disk);
  setStatus(`Deleted: ${filename}`, "complete");
});

// ── Job actions ───────────────────────────────────────────────────────────────

jobsList.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-job-action]");
  if (!button) return;

  const action = button.dataset.jobAction;
  const jobId = button.dataset.jobId;

  if (action === "delete") {
    if (!window.confirm("Remove this job from history?")) return;
    const res = await fetch(`/api/jobs/${jobId}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) { setStatus(data.error || "Could not delete job.", "error"); return; }
    await refreshJobs();
    return;
  }

  if (action === "retry") {
    const forceRetry = button.dataset.force === "1";
    const res = await fetch(`/api/jobs/${jobId}/retry`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force: forceRetry }),
    });
    const data = await res.json();
    if (!res.ok) { setStatus(data.error || "Could not retry job.", "error"); return; }
    setStatus(forceRetry ? "Force re-download queued…" : "Retrying…");
    jobsOffset = 50;
    startPolling();
    return;
  }

  const response = await fetch(`/api/jobs/${jobId}/${action}`, { method: "POST" });
  const data = await response.json();
  if (!response.ok) {
    setStatus(data.error || `Could not ${action} job.`, "error");
    return;
  }
  setStatus(`${action}: ${data.status}`);
  await refreshJobs();
  if (action === "resume") startPolling();
});

// ── Clear finished ────────────────────────────────────────────────────────────

clearFinishedButton.addEventListener("click", async () => {
  const res = await fetch("/api/jobs/clear-finished", { method: "POST" });
  const data = await res.json();
  if (!res.ok) { setStatus(data.error || "Failed.", "error"); return; }
  totalJobs = data.total;
  jobsOffset = 50;
  updateLoadMore();
  renderJobs(data.jobs);
  setStatus(`Cleared ${data.removed} finished job${data.removed !== 1 ? "s" : ""}.`, "complete");
});

// ── URL info on change ────────────────────────────────────────────────────────

urlInput.addEventListener("input", () => fetchVideoInfo(urlInput.value.trim()));

// ── Load more ─────────────────────────────────────────────────────────────────

if (loadMoreBtn) loadMoreBtn.addEventListener("click", loadMoreJobs);

// ── Wire up misc controls ─────────────────────────────────────────────────────

for (const input of kindInputs) input.addEventListener("change", syncQualityControls);
playlistCheckbox.addEventListener("change", syncPlaylistControls);
refreshButton.addEventListener("click", refreshFiles);
refreshJobsButton.addEventListener("click", refreshJobs);

syncQualityControls();
syncPlaylistControls();
