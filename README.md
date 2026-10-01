# Smart Downloader Pro Web

A browser-based FastAPI + yt-dlp downloader derived from the desktop app concept.

## Included

- Single YouTube video downloads
- Single-video filename uses the YouTube title
- Playlist scanner + item selection
- Playlist output numbering: 001, 002, 003 ... 999
- Playlist audio output: 001.m4a, 002.m4a, ...
- Video output: MP4 with H.264 preference and AAC conversion
- Audio output: M4A
- Audio language preference
- YouTube search
- Progress polling
- Docker + FFmpeg included

## Run locally

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt
uvicorn app:app --reload
```

Open:
http://127.0.0.1:8000

FFmpeg must be installed locally when not using Docker.

## Docker

```bash
docker build -t smart-downloader-web .
docker run --rm -p 10000:10000 smart-downloader-web
```

Open:
http://127.0.0.1:10000

## Render

Push this project to GitHub, create a Render Web Service, select Docker as the runtime, and deploy from the repository.

The container listens on the `PORT` environment variable and binds to `0.0.0.0`.

Important production note:
The download directory is local server storage. On hosting plans with ephemeral storage, completed files can disappear after a restart/redeploy. For a serious public service, put finished files in object storage and add authentication/rate limits.

## Environment variables

- `PORT` - HTTP port, default 10000
- `DOWNLOAD_DIR` - output directory, default ./server_downloads
- `MAX_PLAYLIST_ITEMS` - default 999
- `MAX_SEARCH_RESULTS` - default 30
- `MAX_CONCURRENT_JOBS` - default 2
