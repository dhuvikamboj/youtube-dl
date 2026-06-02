const form = document.querySelector("#download-form");
const statusBox = document.querySelector("#status");
const filesList = document.querySelector("#files");
const jobsList = document.querySelector("#jobs");
const diskStats = document.querySelector("#disk");
const diskBar = document.querySelector("#disk-bar");
const refreshButton = document.querySelector("#refresh");
const refreshJobsButton = document.querySelector("#refresh-jobs");
const videoQuality = document.querySelector("#video-quality");
const audioQuality = document.querySelector("#audio-quality");
const kindInputs = document.querySelectorAll("input[name='kind']");

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

function selectedKind() {
  return document.querySelector("input[name='kind']:checked").value;
}

function syncQualityControls() {
  const audio = selectedKind() === "audio";
  videoQuality.disabled = audio;
  audioQuality.disabled = !audio;
}

function updateDisk(disk) {
  diskStats.innerHTML = "";
  const downloads = document.createElement("span");
  const free = document.createElement("span");
  downloads.textContent = `Downloads: ${formatMb(disk.downloads)}`;
  free.textContent = `Free: ${formatGb(disk.free)}`;
  diskStats.append(downloads, free);
  diskBar.style.width = `${((disk.used / disk.total) * 100).toFixed(1)}%`;
}

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

function renderJobs(jobs) {
  jobsList.innerHTML = "";

  if (!jobs.length) {
    const item = document.createElement("li");
    item.className = "empty";
    item.textContent = "No jobs yet.";
    jobsList.append(item);
    return;
  }

  for (const job of jobs) {
    const item = document.createElement("li");
    const body = document.createElement("div");
    const title = document.createElement("strong");
    const status = document.createElement("span");
    const url = document.createElement("small");
    const time = document.createElement("time");

    title.textContent = `${job.kind} ${job.quality}${job.playlist ? " playlist" : ""}`;
    status.textContent = `${job.status}: ${job.progress}`;
    url.textContent = job.url;
    time.textContent = job.created_at;
    body.append(title, status, url);
    item.append(body, time);
    jobsList.append(item);
  }
}

async function refreshFiles() {
  const response = await fetch("/api/downloads");
  const data = await response.json();
  renderFiles(data.downloads);
  updateDisk(data.disk);
}

async function refreshJobs() {
  const response = await fetch("/api/jobs");
  const data = await response.json();
  renderJobs(data.jobs);
}

async function pollJob(jobId) {
  const response = await fetch(`/api/jobs/${jobId}`);
  const job = await response.json();
  await refreshJobs();

  if (job.status === "complete") {
    setStatus(`Complete: ${job.filename}`, "complete");
    await refreshFiles();
    return;
  }

  if (job.status === "error") {
    setStatus(`Error: ${job.error}`, "error");
    return;
  }

  setStatus(`${job.status}: ${job.progress}`);
  window.setTimeout(() => pollJob(jobId), 1200);
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const formData = new FormData(form);
  const kind = formData.get("kind");

  setStatus("Submitting download...");
  const response = await fetch("/api/downloads", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      url: formData.get("url"),
      kind,
      quality: kind === "audio" ? formData.get("audio_quality") : formData.get("video_quality"),
      playlist: formData.get("playlist") === "1",
    }),
  });

  const data = await response.json();
  if (!response.ok) {
    setStatus(data.error || "Could not start download.", "error");
    return;
  }

  pollJob(data.id);
});

filesList.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-delete]");
  if (!button) return;

  const filename = button.dataset.delete;
  const confirmed = window.confirm(`Delete ${filename}?`);
  if (!confirmed) return;

  const response = await fetch(`/api/downloads/${encodeURIComponent(filename)}`, {
    method: "DELETE",
  });
  const data = await response.json();
  if (!response.ok) {
    setStatus(data.error || "Could not delete file.", "error");
    return;
  }

  renderFiles(data.downloads);
  updateDisk(data.disk);
  setStatus(`Deleted: ${filename}`, "complete");
});

for (const input of kindInputs) {
  input.addEventListener("change", syncQualityControls);
}

refreshButton.addEventListener("click", refreshFiles);
refreshJobsButton.addEventListener("click", refreshJobs);
syncQualityControls();
