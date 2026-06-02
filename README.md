# Self-hosted YouTube Downloader

A small Flask web UI for downloading YouTube videos or extracting audio as MP3 on your own server.

## Disclaimer

This project is for personal, private, self-hosted use. Only download content that you own, have permission to download, or are otherwise legally allowed to save in your jurisdiction.

Using this tool may violate YouTube's Terms of Service or a creator's copyright if used improperly. You are responsible for how you use it, where you host it, and what content you download or store.

Do not expose this app publicly without strong authentication, HTTPS, and appropriate access controls.

## Features

- Login page with configurable username/password
- Queued downloads with a configurable concurrency limit
- SQLite download history
- Delete downloaded files from the UI
- Video quality and MP3 bitrate choices
- Optional playlist downloads
- Storage usage and free disk display

## Setup

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Install `ffmpeg` on the server too. Audio conversion to MP3 needs it.

Create your runtime config:

```sh
cp .env.example .env
```

Then edit `.env` and set:

- `APP_USERNAME`
- `APP_PASSWORD`
- `SECRET_KEY`
- `DOWNLOAD_DIR`
- `MAX_CONCURRENT_DOWNLOADS`

## Run with PM2

```sh
pm2 start ecosystem.config.cjs
pm2 save
```

Open `http://localhost:8000`.

Downloaded files are saved to `./downloads`.
Job history is saved to `./downloads.sqlite3`.

Change `DOWNLOAD_DIR` in `.env` to store downloads somewhere else, for example:

```sh
DOWNLOAD_DIR=/mnt/media/youtube
```

## Local development

```sh
source .venv/bin/activate
python app.py
```

Default login:

- Username: `admin`
- Password: `changeme`

## Notes

- This is intended for a private self-hosted server.
- Only YouTube URLs are accepted.
- Run one PM2 instance for this version. SQLite stores history, but the active download queue lives in the Python process.
