import os
import re
import shutil
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


APP_NAME = "Smart Downloader Pro Web"

BASE_DIR = Path(__file__).resolve().parent

DOWNLOAD_ROOT = Path(
    os.getenv("DOWNLOAD_DIR", BASE_DIR / "server_downloads")
)
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

MAX_PLAYLIST_ITEMS = int(
    os.getenv("MAX_PLAYLIST_ITEMS", "999")
)

MAX_SEARCH_RESULTS = int(
    os.getenv("MAX_SEARCH_RESULTS", "30")
)

MAX_CONCURRENT_JOBS = int(
    os.getenv("MAX_CONCURRENT_JOBS", "2")
)

jobs = {}
jobs_lock = threading.RLock()
job_semaphore = threading.Semaphore(MAX_CONCURRENT_JOBS)


class DownloadRequest(BaseModel):
    url: str
    mode: str = Field(
        default="video",
        pattern="^(video|audio)$"
    )
    quality: int = Field(
        default=1080,
        ge=144,
        le=2160
    )
    audio_language: str = ""
    playlist: bool = False
    selected_indexes: list[int] = []


class SearchRequest(BaseModel):
    query: str
    limit: int = Field(
        default=9,
        ge=1,
        le=30
    )


app = FastAPI(title=APP_NAME)

app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static")),
    name="static"
)


# ---------------------------------------------------------
# BASIC HELPERS
# ---------------------------------------------------------

def is_youtube_url(url: str) -> bool:
    try:
        p = urlparse((url or "").strip().lower())
        host = p.netloc.split(":")[0]

        return host in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
            "youtu.be",
            "www.youtu.be",
            "youtube-nocookie.com",
            "www.youtube-nocookie.com",
        }

    except Exception:
        return False


def safe_title(name: str) -> str:
    name = str(name or "video").strip()

    name = re.sub(
        r"[\x00-\x1f<>:\"/\\|?*]",
        "_",
        name
    )

    name = re.sub(
        r"\s+",
        " ",
        name
    )

    name = name.strip(" .")

    return name[:240] or "video"


def format_bytes(value):
    try:
        value = float(value)
    except Exception:
        return "Unknown"

    units = [
        "B",
        "KB",
        "MB",
        "GB",
        "TB"
    ]

    i = 0

    while value >= 1024 and i < len(units) - 1:
        value /= 1024
        i += 1

    return f"{value:.1f} {units[i]}"


# ---------------------------------------------------------
# YT-DLP / YOUTUBE CONFIGURATION
# ---------------------------------------------------------

def youtube_ydl_opts(extra=None):
    """
    Common yt-dlp configuration used by:
    - URL info
    - Search
    - Playlist
    - Single video
    - Actual downloads

    Deno + yt-dlp-ejs are required for current
    YouTube JavaScript challenge handling.
    """

    deno_path = shutil.which("deno")

    if not deno_path:
        raise RuntimeError(
            "Deno JavaScript runtime is not available "
            "inside the server."
        )

    opts = {
        "quiet": True,

        # Keep useful errors in Render logs.
        "no_warnings": False,

        "skip_download": True,

        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,

        "socket_timeout": 30,

        # Current YouTube JS challenge support.
        "js_runtimes": {
            "deno": {
               "path": deno_path
         }
    },

        # Get the EJS challenge scripts from GitHub.
        "remote_components": "ejs:github",

        "ffmpeg_location": (
            shutil.which("ffmpeg") or "ffmpeg"
        ),
    }

    if extra:
        opts.update(extra)

    return opts


def base_ydl_opts(job_id: str):
    """
    Download configuration.
    """

    deno_path = shutil.which("deno")

    if not deno_path:
        raise RuntimeError(
            "Deno JavaScript runtime is not available "
            "inside the server."
        )

    return {
        "quiet": True,
        "no_warnings": False,
        "noprogress": False,

        "windowsfilenames": False,

        "continuedl": True,

        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,

        "socket_timeout": 30,

        "progress_hooks": [
            lambda d: progress_hook(job_id, d)
        ],

        "ffmpeg_location": (
            shutil.which("ffmpeg") or "ffmpeg"
        ),

        # YouTube JS runtime
        "js_runtimes": f"deno:{deno_path}",

        # EJS challenge scripts
        "remote_components": "ejs:github",
    }


# ---------------------------------------------------------
# FORMAT SELECTION
# ---------------------------------------------------------

def choose_format(
    mode: str,
    quality: int,
    audio_language: str
):
    langs = [
        x.strip().lower()
        for x in re.split(
            r"[,;]+",
            audio_language or ""
        )
        if x.strip()
    ]

    # AUDIO ONLY
    if mode == "audio":

        alternatives = []

        for lang in langs:
            alternatives.append(
                f"bestaudio[language^={lang}]"
            )

            alternatives.append(
                f"bestaudio[language^={lang}]"
                f"[acodec^=mp4a]"
            )

        alternatives.extend([
            "bestaudio[acodec^=mp4a]",
            "bestaudio",
        ])

        return "/".join(
            dict.fromkeys(alternatives)
        )

    # VIDEO
    video = (
        f"bestvideo"
        f"[height<={quality}]"
        f"[vcodec^=avc1]"
    )

    progressive = (
        f"best"
        f"[height<={quality}]"
        f"[vcodec^=avc1]"
    )

    audio = []

    for lang in langs:

        audio.append(
            f"bestaudio"
            f"[language^={lang}]"
            f"[acodec^=mp4a]"
        )

        audio.append(
            f"bestaudio"
            f"[language^={lang}]"
        )

    audio.extend([
        "bestaudio[acodec^=mp4a]",
        "bestaudio",
    ])

    pairs = [
        f"{video}+{a}"
        for a in dict.fromkeys(audio)
    ]

    return "/".join(
        pairs + [progressive]
    )


# ---------------------------------------------------------
# PROGRESS
# ---------------------------------------------------------

def progress_hook(job_id, d):

    with jobs_lock:

        job = jobs.get(job_id)

        if not job:
            return

        status = d.get("status")

        job["status"] = status

        if status == "downloading":

            total = (
                d.get("total_bytes")
                or d.get("total_bytes_estimate")
                or 0
            )

            done = (
                d.get("downloaded_bytes")
                or 0
            )

            percent = (
                done / total * 100.0
                if total
                else 0.0
            )

            job["percent"] = round(
                max(
                    0.0,
                    min(100.0, percent)
                ),
                2
            )

            job["speed"] = (
                d.get("_speed_str")
                or ""
            )

            job["eta"] = (
                d.get("_eta_str")
                or ""
            )

            job["filename"] = Path(
                d.get("filename") or ""
            ).name

        elif status == "finished":

            job["percent"] = 100.0
            job["speed"] = ""
            job["eta"] = "00:00"


# ---------------------------------------------------------
# INFO
# ---------------------------------------------------------

def get_info(url: str, flat=False):

    if not is_youtube_url(url):

        raise HTTPException(
            400,
            "Only YouTube URLs are accepted."
        )

    opts = youtube_ydl_opts({
        "skip_download": True,
        "extract_flat": flat,
        "noplaylist": not flat,
    })

    try:

        with yt_dlp.YoutubeDL(opts) as ydl:

            return ydl.extract_info(
                url,
                download=False
            )

    except Exception as exc:

        raise HTTPException(
            400,
            f"yt-dlp error: {exc}"
        )


# ---------------------------------------------------------
# SINGLE VIDEO
# ---------------------------------------------------------

def run_single_video(
    job_id,
    url,
    output_dir,
    mode,
    quality,
    audio_language,
    fixed_basename=None,
    playlist_index=None
):

    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    # First extract metadata using
    # the same Deno/EJS configuration.
    info_opts = youtube_ydl_opts({
        "skip_download": True,
        "noplaylist": True,
    })

    with yt_dlp.YoutubeDL(info_opts) as ydl:

        info = ydl.extract_info(
            url,
            download=False
        )

    title = info.get("title") or "video"

    safe = safe_title(title)

    if fixed_basename:

        template = str(
            output_dir /
            f"{fixed_basename}.%(ext)s"
        )

    else:

        template = str(
            output_dir /
            "%(title)s.%(ext)s"
        )

    opts = base_ydl_opts(job_id)

    opts.update({

        "format": choose_format(
            mode,
            quality,
            audio_language
        ),

        "outtmpl": template,

        "noplaylist": True,

        "postprocessors": [],

        "embedmetadata": True,

        "writethumbnail": False,
    })

    # AUDIO
    if mode == "audio":

        opts.update({

            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "0",
            }],

        })

    # VIDEO
    else:

        opts["merge_output_format"] = "mp4"

        opts["postprocessor_args"] = {

            "Merger+ffmpeg": [
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k"
            ]

        }

    with jobs_lock:

        jobs[job_id]["title"] = title

        jobs[job_id][
            "playlist_index"
        ] = playlist_index

    with yt_dlp.YoutubeDL(opts) as ydl:

        ydl.download([url])

    files = [

        p
        for p in output_dir.iterdir()

        if p.is_file()
        and not p.name.endswith(
            (
                ".part",
                ".ytdl"
            )
        )

    ]

    # PLAYLIST NAMING
    if fixed_basename:

        desired_ext = (
            ".m4a"
            if mode == "audio"
            else ".mp4"
        )

        desired = (
            output_dir /
            f"{fixed_basename}{desired_ext}"
        )

        if desired.exists():

            return desired

        matching = [
            p
            for p in files
            if p.stem == fixed_basename
        ]

        if matching:

            return matching[0]

    # SINGLE VIDEO
    matching = [
        p
        for p in files
        if p.stem == safe
    ]

    if matching:

        return max(
            matching,
            key=lambda p: p.stat().st_mtime
        )

    return (
        max(
            files,
            key=lambda p: p.stat().st_mtime
        )
        if files
        else None
    )


# ---------------------------------------------------------
# JOB EXECUTION
# ---------------------------------------------------------

def execute_job(
    job_id,
    request: DownloadRequest
):

    work_dir = (
        DOWNLOAD_ROOT /
        job_id
    )

    work_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    try:

        with job_semaphore:

            with jobs_lock:

                jobs[job_id][
                    "state"
                ] = "running"

                jobs[job_id][
                    "message"
                ] = "Preparing download…"

            # ---------------------------------------------
            # PLAYLIST
            # ---------------------------------------------

            if request.playlist:

                info = get_info(
                    request.url,
                    flat=True
                )

                entries = [
                    e
                    for e in (
                        info.get("entries")
                        or []
                    )
                    if e
                ]

                if not entries:

                    raise RuntimeError(
                        "No downloadable playlist "
                        "entries found."
                    )

                if len(entries) > MAX_PLAYLIST_ITEMS:

                    entries = entries[
                        :MAX_PLAYLIST_ITEMS
                    ]

                selected = (
                    request.selected_indexes
                    or list(
                        range(
                            1,
                            len(entries) + 1
                        )
                    )
                )

                selected = [
                    i
                    for i in selected
                    if 1 <= i <= len(entries)
                ]

                if not selected:

                    raise RuntimeError(
                        "No playlist items were selected."
                    )

                playlist_title = (
                    info.get("title")
                    or "YouTube Playlist"
                )

                with jobs_lock:

                    jobs[job_id].update({

                        "kind": "playlist",

                        "playlist_title":
                            playlist_title,

                        "total":
                            len(selected),

                        "current":
                            0,

                    })

                for position, original_index in enumerate(
                    selected,
                    start=1
                ):

                    entry = (
                        entries[
                            original_index - 1
                        ]
                    )

                    entry_url = (
                        entry.get("webpage_url")
                        or entry.get("url")
                        or (
                            f"https://www.youtube.com/watch?v="
                            f"{entry.get('id')}"
                            if entry.get("id")
                            else None
                        )
                    )

                    if not entry_url:

                        continue

                    number = f"{position:03d}"

                    with jobs_lock:

                        jobs[job_id].update({

                            "current":
                                position,

                            "percent":
                                0,

                            "message":
                                (
                                    "Downloading playlist "
                                    f"item {number}/"
                                    f"{len(selected):03d}"
                                ),

                        })

                    run_single_video(

                        job_id,

                        entry_url,

                        work_dir,

                        request.mode,

                        request.quality,

                        request.audio_language,

                        fixed_basename=number,

                        playlist_index=original_index,
                    )

                archive = shutil.make_archive(

                    str(
                        DOWNLOAD_ROOT /
                        f"{job_id}_playlist"
                    ),

                    "zip",

                    root_dir=work_dir,
                )

                with jobs_lock:

                    jobs[job_id].update({

                        "state":
                            "completed",

                        "message":
                            (
                                "Playlist complete: "
                                f"{len(selected)} item(s)."
                            ),

                        "percent":
                            100,

                        "file":
                            archive,

                        "filename":
                            (
                                f"{safe_title(playlist_title)}.zip"
                            ),

                    })

            # ---------------------------------------------
            # SINGLE
            # ---------------------------------------------

            else:

                with jobs_lock:

                    jobs[job_id].update({

                        "kind":
                            "single",

                        "total":
                            1,

                        "current":
                            1,

                        "percent":
                            0,

                        "message":
                            "Downloading…",

                    })

                result = run_single_video(

                    job_id,

                    request.url,

                    work_dir,

                    request.mode,

                    request.quality,

                    request.audio_language,
                )

                if (
                    not result
                    or not result.exists()
                ):

                    raise RuntimeError(
                        "The download completed but "
                        "no output file was found."
                    )

                final_path = (
                    DOWNLOAD_ROOT /
                    f"{job_id}_{result.name}"
                )

                shutil.move(
                    str(result),
                    str(final_path)
                )

                with jobs_lock:

                    jobs[job_id].update({

                        "state":
                            "completed",

                        "message":
                            "Download complete.",

                        "percent":
                            100,

                        "file":
                            str(final_path),

                        "filename":
                            result.name,

                    })

    except Exception as exc:

        with jobs_lock:

            jobs[job_id].update({

                "state":
                    "failed",

                "message":
                    str(exc),

                "error":
                    str(exc),

            })


# ---------------------------------------------------------
# HOME
# ---------------------------------------------------------

@app.get(
    "/",
    response_class=HTMLResponse
)
async def home():

    return (
        BASE_DIR /
        "static" /
        "index.html"
    ).read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------
# VIDEO INFO
# ---------------------------------------------------------

@app.get("/api/info")
async def api_info(url: str):

    info = get_info(
        url,
        flat=False
    )

    return {

        "id":
            info.get("id"),

        "title":
            info.get("title"),

        "channel":
            (
                info.get("channel")
                or info.get("uploader")
            ),

        "duration":
            info.get("duration"),

        "thumbnail":
            info.get("thumbnail"),

        "height":
            info.get("height"),

        "view_count":
            info.get("view_count"),

    }


# ---------------------------------------------------------
# SEARCH
# ---------------------------------------------------------

@app.post("/api/search")
async def api_search(
    request: SearchRequest
):

    query = request.query.strip()

    if not query:

        raise HTTPException(
            400,
            "Enter a search term."
        )

    opts = youtube_ydl_opts({

        "extract_flat":
            True,

        "skip_download":
            True,

    })

    try:

        with yt_dlp.YoutubeDL(opts) as ydl:

            data = ydl.extract_info(

                f"ytsearch"
                f"{min(request.limit, MAX_SEARCH_RESULTS)}:"
                f"{query}",

                download=False

            )

    except Exception as exc:

        raise HTTPException(
            400,
            f"Search failed: {exc}"
        )

    results = []

    for item in (
        data.get("entries")
        or []
    ):

        if not item:

            continue

        vid = item.get("id")

        url = (
            item.get("webpage_url")
            or (
                f"https://www.youtube.com/watch?v={vid}"
                if vid
                else ""
            )
        )

        results.append({

            "id":
                vid,

            "title":
                item.get("title"),

            "channel":
                (
                    item.get("channel")
                    or item.get("uploader")
                ),

            "duration":
                item.get("duration"),

            "url":
                url,

            "thumbnail":
                (
                    f"https://i.ytimg.com/vi/"
                    f"{vid}/mqdefault.jpg"
                    if vid
                    else ""
                ),

        })

    return {
        "results":
            results
    }


# ---------------------------------------------------------
# PLAYLIST
# ---------------------------------------------------------

@app.get("/api/playlist")
async def api_playlist(
    url: str
):

    info = get_info(
        url,
        flat=True
    )

    entries = []

    for index, item in enumerate(
        info.get("entries") or [],
        start=1
    ):

        if not item:

            continue

        vid = item.get("id")

        entry_url = (
            item.get("webpage_url")
            or item.get("url")
            or (
                f"https://www.youtube.com/watch?v={vid}"
                if vid
                else ""
            )
        )

        if not entry_url:

            continue

        entries.append({

            "index":
                index,

            "title":
                item.get("title")
                or f"Video {index}",

            "duration":
                item.get("duration"),

            "url":
                entry_url,

            "id":
                vid,

            "thumbnail":
                (
                    f"https://i.ytimg.com/vi/"
                    f"{vid}/mqdefault.jpg"
                    if vid
                    else ""
                ),

        })

        if len(entries) >= MAX_PLAYLIST_ITEMS:

            break

    return {

        "title":
            info.get("title")
            or "YouTube Playlist",

        "count":
            len(entries),

        "entries":
            entries,

    }


# ---------------------------------------------------------
# START DOWNLOAD
# ---------------------------------------------------------

@app.post("/api/download")
async def api_download(
    request: DownloadRequest
):

    if not is_youtube_url(
        request.url
    ):

        raise HTTPException(
            400,
            "Only YouTube URLs are accepted."
        )

    if (
        request.playlist
        and len(request.selected_indexes)
        > MAX_PLAYLIST_ITEMS
    ):

        raise HTTPException(

            400,

            f"At most "
            f"{MAX_PLAYLIST_ITEMS} "
            f"playlist items can be selected."

        )

    job_id = uuid.uuid4().hex

    with jobs_lock:

        jobs[job_id] = {

            "id":
                job_id,

            "state":
                "queued",

            "kind":
                (
                    "playlist"
                    if request.playlist
                    else "single"
                ),

            "percent":
                0,

            "speed":
                "",

            "eta":
                "",

            "message":
                "Queued…",

        }

    thread = threading.Thread(

        target=execute_job,

        args=(
            job_id,
            request
        ),

        daemon=True,

    )

    thread.start()

    return {
        "job_id":
            job_id
    }


# ---------------------------------------------------------
# JOB STATUS
# ---------------------------------------------------------

@app.get(
    "/api/jobs/{job_id}"
)
async def api_job(
    job_id: str
):

    with jobs_lock:

        data = jobs.get(
            job_id
        )

    if not data:

        raise HTTPException(
            404,
            "Job not found."
        )

    return data


# ---------------------------------------------------------
# DOWNLOAD FILE
# ---------------------------------------------------------

@app.get(
    "/api/jobs/{job_id}/file"
)
async def api_job_file(
    job_id: str
):

    with jobs_lock:

        data = jobs.get(
            job_id
        )

    if (
        not data
        or data.get("state")
        != "completed"
    ):

        raise HTTPException(
            404,
            "Completed file not available yet."
        )

    path = data.get("file")

    if (
        not path
        or not Path(path).is_file()
    ):

        raise HTTPException(
            404,
            "Output file no longer exists."
        )

    return FileResponse(

        path,

        filename=(
            data.get("filename")
            or Path(path).name
        ),

        media_type=
            "application/octet-stream",

    )


# ---------------------------------------------------------
# HEALTH
# ---------------------------------------------------------

@app.get("/api/health")
async def health():

    deno_path = shutil.which("deno")
    ffmpeg_path = shutil.which("ffmpeg")

    return {

        "ok":
            True,

        "ffmpeg":
            bool(ffmpeg_path),

        "deno":
            bool(deno_path),

        "yt_dlp":
            True,

        "yt_dlp_version":
            yt_dlp.version.__version__,

    }
    
