import os
import urllib.request
import urllib.error
import re
import shutil
import threading
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


# ============================================================
# APP CONFIG
# ============================================================

APP_NAME = "Smart Downloader Pro Web"
APP_VERSION = "3.1.0"

BASE_DIR = Path(__file__).resolve().parent

DOWNLOAD_ROOT = Path(
    os.getenv(
        "DOWNLOAD_DIR",
        str(BASE_DIR / "server_downloads")
    )
)

MAX_PLAYLIST_ITEMS = int(
    os.getenv("MAX_PLAYLIST_ITEMS", "999")
)

MAX_SEARCH_RESULTS = int(
    os.getenv("MAX_SEARCH_RESULTS", "30")
)

MAX_CONCURRENT_JOBS = int(
    os.getenv("MAX_CONCURRENT_JOBS", "2")
)

DOWNLOAD_ROOT.mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION
)

STATIC_DIR = BASE_DIR / "static"

if STATIC_DIR.exists():
    app.mount(
        "/static",
        StaticFiles(
            directory=str(STATIC_DIR)
        ),
        name="static"
    )


# ============================================================
# JOB STORAGE
# ============================================================

jobs = {}
jobs_lock = threading.Lock()

active_jobs = 0
active_jobs_lock = threading.Lock()


# ============================================================
# MODELS
# ============================================================

class DownloadRequest(BaseModel):
    url: str = ""
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


# ============================================================
# BASIC HELPERS
# ============================================================

def is_youtube_url(url: str) -> bool:
    try:
        parsed = urlparse(
            url.strip()
        )
        host = (
            parsed.netloc
            .lower()
            .split(":")[0]
        )
        return host in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
            "youtu.be",
            "www.youtu.be",
        }
    except Exception:
        return False


def safe_title(value: str) -> str:
    if not value:
        return "download"
    
    value = re.sub(
        r'[<>:"/\\|?*\x00-\x1F]',
        "_",
        value
    )
    value = re.sub(
        r"\s+",
        " ",
        value
    ).strip()
    value = value.rstrip(". ")
    
    if not value:
        return "download"
        
    return value[:180]


def format_bytes(value):
    try:
        value = float(
            value or 0
        )
    except Exception:
        return "0 B"
        
    units = [
        "B",
        "KB",
        "MB",
        "GB",
        "TB"
    ]
    
    for unit in units:
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
        
    return f"{value:.1f} PB"


def get_ffmpeg():
    return (
        shutil.which("ffmpeg")
        or "ffmpeg"
    )


def get_ffprobe():
    return (
        shutil.which("ffprobe")
        or "ffprobe"
    )


def get_deno():
    return shutil.which(
        "deno"
    )


DENO_PATH = get_deno()


def resolve_url(
    request: DownloadRequest | None,
    query_url: str = ""
) -> str:
    if request and request.url:
        return request.url.strip()
    return (
        query_url or ""
    ).strip()


def compact_error(exc):
    text = str(
        exc or ""
    ).strip()
    if not text:
        return "Unknown yt-dlp error."
    return text[-6000:]


# ============================================================
# YOUTUBE CLIENT STRATEGIES
# ============================================================
YOUTUBE_CLIENT_STRATEGIES = [
    ("android", {"extractor_args": {"youtube": {"player_client": ["android"]}}}),
    ("ios", {"extractor_args": {"youtube": {"player_client": ["ios"]}}}),
    ("mweb", {"extractor_args": {"youtube": {"player_client": ["mweb"]}}}),
    ("tv", {"extractor_args": {"youtube": {"player_client": ["tv"]}}}),
    ("web_embedded", {"extractor_args": {"youtube": {"player_client": ["web_embedded"]}}}),
    ("default", None),
]


# ============================================================
# COMMON YT-DLP OPTIONS
# ============================================================

def add_bypass_options(opts):
    # 1. JavaScript Runtimes for PO-Tokens
    if DENO_PATH:
        opts["js_runtimes"] = {
            "deno": {
                "path": DENO_PATH
            }
        }
    opts["remote_components"] = ["ejs:github"]
    
    # 2. TLS Impersonation (Bypasses YouTube 429 fingerprint blocks)
    # Ab curl_cffi install ho gaya hai, toh ye bina error ke chalega aur block se bachayega!
    opts["impersonate"] = "chrome"
    
    # 3. Automatic Cookie Detection (The ultimate fallback)
    cookie_path = BASE_DIR / "cookies.txt"
    if cookie_path.exists():
        opts["cookiefile"] = str(cookie_path)
        
    return opts


def youtube_ydl_opts(
    extra=None,
    playlist=False
):
    opts = {
        "quiet": True,
        "no_warnings": False,
        "skip_download": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "sleep_interval_requests": 2,
        "sleep_interval": 2,
        "max_sleep_interval": 5,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        "ffmpeg_location": get_ffmpeg(),
        "noplaylist": not playlist,
    }
    
    opts = add_bypass_options(opts)
            
    if extra:
        opts.update(extra)
        
    return opts


def base_ydl_opts(job_id):
    output_dir = (
        DOWNLOAD_ROOT /
        job_id
    )
    
    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )
    
    opts = {
        "quiet": True,
        "no_warnings": False,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "sleep_interval_requests": 2,
        "sleep_interval": 2,
        "max_sleep_interval": 5,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        "ffmpeg_location": get_ffmpeg(),
        "progress_hooks": [
            progress_hook
        ],
        "outtmpl": str(
            output_dir /
            "%(title)s.%(ext)s"
        ),
        "restrictfilenames": False,
        "windowsfilenames": True,
        "continuedl": True,
        "nopart": False,
        "noprogress": True,
    }
    
    opts = add_bypass_options(opts)
            
    return opts


# ============================================================
# ERROR CLASSIFICATION
# ============================================================

def is_local_error(
    error_text
):
    text = (
        error_text or ""
    ).lower()
    
    local_error_words = [
        "permission denied",
        "no space left",
        "ffmpeg not found",
        "ffprobe not found",
        "disk quota",
        "read-only file system",
        "cannot write",
        "could not write",
        "input/output error",
    ]
    
    return any(
        word in text
        for word in local_error_words
    )


def is_extraction_error(
    exc
):
    text = str(
        exc or ""
    ).lower()
    
    indicators = [
        "failed to extract any player response",
        "unable to extract",
        "player response",
        "unable to extract yt initial data",
        "unable to extract player version",
        "incomplete data received",
        "sign in to confirm",
        "confirm you're not a bot",
        "confirm you are not a bot",
        "video unavailable",
        "this content isn't available",
        "this content is not available",
        "http error 403",
        "http error 429",
        "po token",
        "challenge",
        "page needs to be reloaded",
    ]
    
    return any(
        item in text
        for item in indicators
    )


# ============================================================
# YOUTUBE INFO EXTRACTION
# ============================================================

def extract_youtube_info(
    url,
    extra=None,
    allow_playlist=False
):
    if not is_youtube_url(url):
        raise ValueError("Please enter a valid YouTube URL.")
        
    errors = []
    
    for strategy_name, strategy_kwargs in YOUTUBE_CLIENT_STRATEGIES:
        try:
            print(f"[YT] Trying strategy: {strategy_name}")
            
            opts_extra = dict(extra or {})
            opts_extra["noplaylist"] = not allow_playlist
            opts_extra["skip_download"] = True
            
            opts = youtube_ydl_opts(
                extra=opts_extra,
                playlist=allow_playlist
            )
            
            if strategy_kwargs:
                opts.update(strategy_kwargs)
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(
                    url,
                    download=False
                )
                
            if info:
                print(f"[YT] SUCCESS: {strategy_name}")
                return info
                
        except Exception as exc:
            error_text = compact_error(exc)
            errors.append(f"{strategy_name}: {error_text}")
            print(f"[YT] FAILED: {strategy_name}")
            print(error_text)
            
            if is_local_error(error_text):
                raise
                
    joined = "\n\n".join(errors)
    raise RuntimeError(
        "YouTube extraction failed after trying "
        "all available extraction strategies.\n\n"
        f"{joined}"
    )


# ============================================================
# SEARCH EXTRACTION
# ============================================================

def extract_youtube_search(
    search_query,
    limit
):
    errors = []
    
    for strategy_name, strategy_kwargs in YOUTUBE_CLIENT_STRATEGIES:
        try:
            print(f"[SEARCH] Trying strategy: {strategy_name}")
            
            opts = youtube_ydl_opts(
                extra={
                    "extract_flat": True,
                    "skip_download": True,
                    "noplaylist": False,
                },
                playlist=True
            )
            
            if strategy_kwargs:
                opts.update(strategy_kwargs)
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                data = ydl.extract_info(
                    search_query,
                    download=False
                )
                
            if data:
                print(f"[SEARCH] SUCCESS: {strategy_name}")
                return data
                
        except Exception as exc:
            error_text = compact_error(exc)
            errors.append(f"{strategy_name}: {error_text}")
            print(f"[SEARCH] FAILED: {strategy_name}: {error_text}")
            
            if is_local_error(error_text):
                raise
                
    raise RuntimeError(
        "YouTube search failed.\n\n"
        + "\n\n".join(errors)
    )


# ============================================================
# DOWNLOAD WITH FALLBACK
# ============================================================

def download_with_fallback(
    job_id,
    url,
    opts_factory
):
    errors = []
    
    for strategy_name, strategy_kwargs in YOUTUBE_CLIENT_STRATEGIES:
        try:
            print(f"[DOWNLOAD] Trying strategy: {strategy_name}")
            
            opts = opts_factory()
            
            if strategy_kwargs:
                opts.update(strategy_kwargs)
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
                
            print(f"[DOWNLOAD] SUCCESS: {strategy_name}")
            return
            
        except Exception as exc:
            error_text = compact_error(exc)
            errors.append(f"{strategy_name}: {error_text}")
            print(f"[DOWNLOAD] FAILED: {strategy_name}")
            print(error_text)
            
            if is_local_error(error_text):
                raise
                
    raise RuntimeError(
        "YouTube download failed after trying "
        "all extraction strategies.\n\n"
        + "\n\n".join(errors)
    )


# ============================================================
# FORMAT SELECTION
# ============================================================

def choose_format(
    quality=1080,
    audio_language=""
):
    height = int(
        quality
    )
    
    if audio_language:
        lang = re.sub(
            r"[^a-zA-Z0-9-]",
            "",
            str(
                audio_language
            )
        )
        audio_selector = (
            f"bestaudio[language^={lang}]"
            "/bestaudio"
        )
    else:
        audio_selector = (
            "bestaudio[acodec^=mp4a]"
            "/bestaudio"
        )
        
    video_selector = (
        f"bestvideo"
        f"[vcodec^=avc1]"
        f"[height<={height}]"
    )
    
    fallback_video = (
        f"bestvideo"
        f"[height<={height}]"
    )
    
    return (
        f"{video_selector}+{audio_selector}/"
        f"{fallback_video}+{audio_selector}/"
        f"best[height<={height}]/"
        "best"
    )


# ============================================================
# JOB HELPERS
# ============================================================

def update_job(
    job_id,
    **values
):
    with jobs_lock:
        if job_id not in jobs:
            return
        jobs[job_id].update(
            values
        )


def progress_hook(
    data
):
    job_id = data.get(
        "_smart_job_id"
    )
    if not job_id:
        return
        
    status = data.get(
        "status"
    )
    
    if status == "downloading":
        downloaded = data.get(
            "downloaded_bytes",
            0
        )
        total = (
            data.get(
                "total_bytes"
            )
            or
            data.get(
                "total_bytes_estimate"
            )
            or 0
        )
        
        percent = 0
        if total:
            percent = (
                downloaded /
                total
            ) * 100
            
        speed = data.get(
            "speed"
        )
        eta = data.get(
            "eta"
        )
        filename = data.get(
            "filename",
            ""
        )
        
        update_job(
            job_id,
            status="downloading",
            percent=round(
                min(
                    max(
                        percent,
                        0
                    ),
                    100
                ),
                1
            ),
            downloaded=format_bytes(
                downloaded
            ),
            total=format_bytes(
                total
            ),
            speed=(
                f"{format_bytes(speed)}/s"
                if speed
                else ""
            ),
            eta=(
                f"{eta}s"
                if eta is not None
                else ""
            ),
            filename=(
                Path(
                    filename
                ).name
                if filename
                else ""
            )
        )
        
    elif status == "finished":
        update_job(
            job_id,
            status="processing",
            percent=100
        )
        
    elif status == "error":
        update_job(
            job_id,
            status="error",
            error=(
                "yt-dlp reported a download error."
            )
        )


# ============================================================
# INFO
# ============================================================

def get_info(
    url
):
    return extract_youtube_info(
        url,
        extra={
            "extract_flat": False,
            "noplaylist": True,
        },
        allow_playlist=False
    )


def build_info_response(
    info
):
    formats = []
    
    for fmt in info.get(
        "formats",
        []
    ):
        formats.append({
            "format_id": fmt.get(
                "format_id"
            ),
            "ext": fmt.get(
                "ext"
            ),
            "height": fmt.get(
                "height"
            ),
            "width": fmt.get(
                "width"
            ),
            "fps": fmt.get(
                "fps"
            ),
            "vcodec": fmt.get(
                "vcodec"
            ),
            "acodec": fmt.get(
                "acodec"
            ),
            "filesize": fmt.get(
                "filesize"
            ),
            "filesize_approx": fmt.get(
                "filesize_approx"
            ),
            "tbr": fmt.get(
                "tbr"
            ),
            "language": fmt.get(
                "language"
            ),
            "dynamic_range": fmt.get(
                "dynamic_range"
            ),
            "protocol": fmt.get(
                "protocol"
            ),
            "url": None,
        })
        
    return {
        "id": info.get(
            "id"
        ),
        "title": info.get(
            "title"
        ),
        "description": info.get(
            "description"
        ),
        "channel": (
            info.get("channel")
            or
            info.get("uploader")
        ),
        "uploader": info.get(
            "uploader"
        ),
        "duration": info.get(
            "duration"
        ),
        "thumbnail": info.get(
            "thumbnail"
        ),
        "webpage_url": info.get(
            "webpage_url"
        ),
        "upload_date": info.get(
            "upload_date"
        ),
        "view_count": info.get(
            "view_count"
        ),
        "formats": formats,
        "subtitles": list(
            (
                info.get(
                    "subtitles"
                )
                or {}
            ).keys()
        ),
        "automatic_captions": list(
            (
                info.get(
                    "automatic_captions"
                )
                or {}
            ).keys()
        ),
    }


# ============================================================
# SINGLE VIDEO
# ============================================================

def run_single_video(
    job_id,
    url,
    mode,
    quality,
    audio_language,
    output_dir
):
    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )
    
    update_job(
        job_id,
        status="preparing",
        percent=0
    )
    
    # --------------------------------------------------------
    # Extract metadata
    # --------------------------------------------------------
    info = extract_youtube_info(
        url,
        extra={
            "extract_flat": False,
            "noplaylist": True,
        },
        allow_playlist=False
    )
    
    title = safe_title(
        info.get(
            "title",
            "download"
        )
    )
    
    final_template = str(
        output_dir /
        f"{title}.%(ext)s"
    )
    
    update_job(
        job_id,
        status="preparing",
        title=title,
        percent=0
    )
    
    # --------------------------------------------------------
    # Build download options
    # --------------------------------------------------------
    def make_options():
        opts = base_ydl_opts(job_id)
        
        opts.update({
            "outtmpl":
                final_template,
            "noplaylist":
                True,
        })
        
        if mode == "audio":
            opts.update({
                "format": (
                    "bestaudio[acodec^=mp4a]"
                    "/bestaudio"
                ),
                "postprocessors": [
                    {
                        "key":
                            "FFmpegExtractAudio",
                        "preferredcodec":
                            "m4a",
                        "preferredquality":
                            "0",
                    }
                ],
            })
        else:
            opts.update({
                "format": choose_format(
                    quality,
                    audio_language
                ),
                "merge_output_format":
                    "mp4",
                "postprocessor_args": [
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-movflags",
                    "+faststart",
                ],
            })
            
        def job_progress(
            data
        ):
            data[
                "_smart_job_id"
            ] = job_id
            
            progress_hook(
                data
            )
            
        opts[
            "progress_hooks"
        ] = [
            job_progress
        ]
        
        return opts
        
    update_job(
        job_id,
        status="downloading",
        title=title,
        percent=0
    )
    
    download_with_fallback(
        job_id,
        url,
        make_options
    )
    
    # --------------------------------------------------------
    # Find final file
    # --------------------------------------------------------
    files = [
        file
        for file in output_dir.iterdir()
        if file.is_file()
    ]
    
    if not files:
        raise RuntimeError(
            "Download finished but no output "
            "file was found."
        )
        
    result = max(
        files,
        key=lambda p: p.stat().st_mtime
    )
    
    update_job(
        job_id,
        status="completed",
        percent=100,
        file=str(
            result
        ),
        filename=result.name,
        title=title
    )
    
    return result


# ============================================================
# PLAYLIST
# ============================================================

def run_playlist(
    job_id,
    url,
    mode,
    quality,
    audio_language,
    selected_indexes
):
    update_job(
        job_id,
        status="reading_playlist",
        percent=0
    )
    
    info = extract_youtube_info(
        url,
        extra={
            "extract_flat": True,
            "skip_download": True,
            "noplaylist": False,
            "playlistend":
                MAX_PLAYLIST_ITEMS,
        },
        allow_playlist=True
    )
    
    entries = [
        item
        for item in (
            info.get(
                "entries"
            )
            or []
        )
        if item
    ]
    
    if not entries:
        raise RuntimeError(
            "No videos were found "
            "in the playlist."
        )
        
    if selected_indexes:
        selected_set = {
            int(x)
            for x in selected_indexes
        }
        
        entries = [
            item
            for index, item in enumerate(
                entries,
                start=1
            )
            if index in selected_set
        ]
        
    entries = entries[
        :MAX_PLAYLIST_ITEMS
    ]
    
    playlist_dir = (
        DOWNLOAD_ROOT /
        job_id /
        "playlist"
    )
    
    playlist_dir.mkdir(
        parents=True,
        exist_ok=True
    )
    
    downloaded_files = []
    
    total = len(
        entries
    )
    
    for position, item in enumerate(
        entries,
        start=1
    ):
        video_id = item.get(
            "id"
        )
        
        video_url = (
            item.get(
                "url"
            )
            or
            item.get(
                "webpage_url"
            )
            or
            (
                f"https://www.youtube.com/watch?v={video_id}"
                if video_id
                else ""
            )
        )
        
        if not video_url:
            continue
            
        item_title = item.get("title", "video")
        numbered_name = (
            f"{position:03d} - "
            f"{safe_title(item_title)}"
        )
        
        update_job(
            job_id,
            status="downloading",
            current=position,
            total=total,
            percent=round(
                (
                    (position - 1)
                    / total
                ) * 100,
                1
            ),
            title=item.get(
                "title"
            )
        )
        
        template = str(
            playlist_dir /
            f"{numbered_name}.%(ext)s"
        )
        
        def make_options(
            pos=position,
            count=total,
            out_template=template
        ):
            opts = base_ydl_opts(job_id)
            
            opts.update({
                "outtmpl":
                    out_template,
                "noplaylist":
                    True,
            })
            
            if mode == "audio":
                opts.update({
                    "format": (
                        "bestaudio[acodec^=mp4a]"
                        "/bestaudio"
                    ),
                    "postprocessors": [
                        {
                            "key":
                                "FFmpegExtractAudio",
                            "preferredcodec":
                                "m4a",
                            "preferredquality":
                                "0",
                        }
                    ],
                })
            else:
                opts.update({
                    "format": choose_format(
                        quality,
                        audio_language
                    ),
                    "merge_output_format":
                        "mp4",
                    "postprocessor_args": [
                        "-c:v",
                        "copy",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                        "-movflags",
                        "+faststart",
                    ],
                })
                
            def playlist_progress(
                data,
                jid=job_id,
                current_pos=pos,
                item_count=count
            ):
                if data.get(
                    "status"
                ) != "downloading":
                    return
                    
                downloaded = data.get(
                    "downloaded_bytes",
                    0
                )
                
                total_bytes = (
                    data.get(
                        "total_bytes"
                    )
                    or
                    data.get(
                        "total_bytes_estimate"
                    )
                    or 0
                )
                
                item_percent = 0
                
                if total_bytes:
                    item_percent = (
                        downloaded /
                        total_bytes
                    ) * 100
                    
                overall = (
                    (
                        (current_pos - 1)
                        + item_percent / 100
                    )
                    / item_count
                ) * 100
                
                update_job(
                    jid,
                    status="downloading",
                    current=current_pos,
                    total=item_count,
                    percent=round(
                        overall,
                        1
                    ),
                    downloaded=format_bytes(
                        downloaded
                    ),
                    speed=(
                        f"{format_bytes(data.get('speed'))}/s"
                        if data.get(
                            "speed"
                        )
                        else ""
                    ),
                    eta=(
                        f"{data.get('eta')}s"
                        if data.get(
                            "eta"
                        ) is not None
                        else ""
                    )
                )
                
            opts[
                "progress_hooks"
            ] = [
                playlist_progress
            ]
            
            return opts
            
        download_with_fallback(
            job_id,
            video_url,
            make_options
        )
        
        generated = [
            file
            for file in playlist_dir.iterdir()
            if file.is_file()
        ]
        
        if generated:
            newest = max(
                generated,
                key=lambda p: p.stat().st_mtime
            )
            downloaded_files.append(
                newest
            )
            
    if not downloaded_files:
        raise RuntimeError(
            "Playlist download completed but "
            "no files were created."
        )
        
    zip_path = (
        DOWNLOAD_ROOT /
        job_id /
        "playlist_download.zip"
    )
    
    with zipfile.ZipFile(
        zip_path,
        "w",
        compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for file in downloaded_files:
            archive.write(
                file,
                arcname=file.name
            )
            
    update_job(
        job_id,
        status="completed",
        percent=100,
        file=str(
            zip_path
        ),
        filename=zip_path.name,
        count=len(
            downloaded_files
        )
    )
    
    return zip_path


# ============================================================
# JOB EXECUTION
# ============================================================

def execute_job(
    job_id,
    request
):
    global active_jobs
    
    with active_jobs_lock:
        active_jobs += 1
        
    try:
        update_job(
            job_id,
            status="starting",
            percent=0,
            error=None
        )
        
        url = request.url.strip()
        
        job_dir = (
            DOWNLOAD_ROOT /
            job_id
        )
        
        job_dir.mkdir(
            parents=True,
            exist_ok=True
        )
        
        if request.playlist:
            result = run_playlist(
                job_id=job_id,
                url=url,
                mode=request.mode,
                quality=request.quality,
                audio_language=(
                    request.audio_language
                    or ""
                ),
                selected_indexes=(
                    request.selected_indexes
                    or []
                )
            )
        else:
            result = run_single_video(
                job_id=job_id,
                url=url,
                mode=request.mode,
                quality=request.quality,
                audio_language=(
                    request.audio_language
                    or ""
                ),
                output_dir=job_dir
            )
            
        update_job(
            job_id,
            status="completed",
            percent=100,
            file=str(
                result
            ),
            filename=Path(
                result
            ).name
        )
        
    except Exception as exc:
        error_text = compact_error(
            exc
        )
        print(
            f"[JOB ERROR] {job_id}: "
            f"{error_text}"
        )
        update_job(
            job_id,
            status="error",
            error=error_text
        )
    finally:
        with active_jobs_lock:
            active_jobs -= 1


# ============================================================
# HOME
# ============================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
async def index():
    index_file = (
        STATIC_DIR /
        "index.html"
    )
    
    if not index_file.exists():
        return HTMLResponse(
            f"""
            <!doctype html>
            <html>
            <head>
                <title>{APP_NAME}</title>
            </head>
            <body>
                <h1>{APP_NAME}</h1>
                <p>Frontend file is missing.</p>
            </body>
            </html>
            """,
            status_code=500
        )
        
    return index_file.read_text(
        encoding="utf-8"
    )


# ============================================================
# INFO API
# ============================================================

@app.api_route(
    "/api/info",
    methods=[
        "GET",
        "POST"
    ]
)
async def api_info(
    request: DownloadRequest | None = None,
    url: str = Query(
        default=""
    )
):
    final_url = resolve_url(
        request,
        url
    )
    
    if not final_url:
        raise HTTPException(
            400,
            "YouTube URL is required."
        )
        
    try:
        info = get_info(
            final_url
        )
        return build_info_response(
            info
        )
    except Exception as exc:
        error_text = compact_error(
            exc
        )
        print(
            "[INFO ERROR]",
            error_text
        )
        raise HTTPException(
            400,
            "Unable to extract video information: "
            f"{error_text}"
        )


# ============================================================
# SEARCH API
# ============================================================

@app.post(
    "/api/search"
)
async def api_search(
    request: SearchRequest
):
    query = request.query.strip()
    
    if not query:
        raise HTTPException(
            400,
            "Enter a search term."
        )
        
    search_limit = min(
        request.limit,
        MAX_SEARCH_RESULTS
    )
    
    search_query = (
        f"ytsearch{search_limit}:{query}"
    )
    
    try:
        data = extract_youtube_search(
            search_query,
            search_limit
        )
    except Exception as exc:
        error_text = compact_error(
            exc
        )
        print(
            "[SEARCH ERROR]",
            error_text
        )
        raise HTTPException(
            400,
            f"Search failed: {error_text}"
        )
        
    results = []
    
    for item in (
        data.get(
            "entries"
        )
        or []
    ):
        if not item:
            continue
            
        vid = item.get(
            "id"
        )
        
        video_url = (
            item.get(
                "webpage_url"
            )
            or
            (
                f"https://www.youtube.com/watch?v={vid}"
                if vid
                else ""
            )
        )
        
        results.append({
            "id": vid,
            "title": item.get(
                "title"
            ),
            "channel": (
                item.get(
                    "channel"
                )
                or
                item.get(
                    "uploader"
                )
            ),
            "duration": item.get(
                "duration"
            ),
            "url": video_url,
            "thumbnail": (
                f"https://i.ytimg.com/vi/"
                f"{vid}/mqdefault.jpg"
                if vid
                else ""
            ),
        })
        
    return {
        "results": results
    }


# ============================================================
# PLAYLIST API
# ============================================================

@app.api_route(
    "/api/playlist",
    methods=[
        "GET",
        "POST"
    ]
)
async def api_playlist(
    request: DownloadRequest | None = None,
    url: str = Query(
        default=""
    )
):
    final_url = resolve_url(
        request,
        url
    )
    
    if not final_url:
        raise HTTPException(
            400,
            "Playlist URL is required."
        )
        
    if not is_youtube_url(
        final_url
    ):
        raise HTTPException(
            400,
            "Only YouTube playlist URLs "
            "are supported."
        )
        
    try:
        data = extract_youtube_info(
            final_url,
            extra={
                "extract_flat": True,
                "skip_download": True,
                "noplaylist": False,
                "playlistend":
                    MAX_PLAYLIST_ITEMS,
            },
            allow_playlist=True
        )
    except Exception as exc:
        error_text = compact_error(
            exc
        )
        print(
            "[PLAYLIST ERROR]",
            error_text
        )
        raise HTTPException(
            400,
            "Playlist extraction failed: "
            f"{error_text}"
        )
        
    entries = []
    
    for index, item in enumerate(
        data.get(
            "entries"
        )
        or [],
        start=1
    ):
        if not item:
            continue
            
        vid = item.get(
            "id"
        )
        
        video_url = (
            item.get(
                "webpage_url"
            )
            or
            (
                f"https://www.youtube.com/watch?v={vid}"
                if vid
                else ""
            )
        )
        
        entries.append({
            "index": index,
            "id": vid,
            "title": item.get(
                "title"
            ),
            "url": video_url,
            "duration": item.get(
                "duration"
            ),
            "thumbnail": (
                f"https://i.ytimg.com/vi/"
                f"{vid}/mqdefault.jpg"
                if vid
                else ""
            ),
        })
        
    return {
        "title": data.get(
            "title"
        ),
        "channel": (
            data.get(
                "channel"
            )
            or
            data.get(
                "uploader"
            )
        ),
        "count": len(
            entries
        ),
        "entries": entries,
    }


# ============================================================
# DOWNLOAD API
# ============================================================

@app.post(
    "/api/download"
)
async def api_download(
    request: DownloadRequest
):
    global active_jobs
    
    if not request.url.strip():
        raise HTTPException(
            400,
            "URL is required."
        )
        
    if not is_youtube_url(
        request.url
    ):
        raise HTTPException(
            400,
            "Only YouTube URLs are supported."
        )
        
    with active_jobs_lock:
        if active_jobs >= MAX_CONCURRENT_JOBS:
            raise HTTPException(
                429,
                "Server is currently busy. "
                "Please try again shortly."
            )
            
    job_id = uuid.uuid4().hex
    
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "percent": 0,
            "downloaded": "0 B",
            "total": "0 B",
            "speed": "",
            "eta": "",
            "filename": "",
            "file": "",
            "error": None,
            "title": "",
            "current": 0,
            "count": 0,
        }
        
    thread = threading.Thread(
        target=execute_job,
        args=(
            job_id,
            request
        ),
        daemon=True
    )
    thread.start()
    
    return {
        "job_id": job_id,
        "status": "queued"
    }


# ============================================================
# JOB STATUS
# ============================================================

@app.get(
    "/api/jobs/{job_id}"
)
async def api_job_status(
    job_id: str
):
    with jobs_lock:
        job = jobs.get(
            job_id
        )
        
        if not job:
            raise HTTPException(
                404,
                "Job not found."
            )
            
        return dict(
            job
        )


# ============================================================
# RESULT FILE
# ============================================================

@app.get(
    "/api/jobs/{job_id}/file"
)
async def api_job_file(
    job_id: str
):
    with jobs_lock:
        job = jobs.get(
            job_id
        )
        
        if not job:
            raise HTTPException(
                404,
                "Job not found."
            )
            
        file_path = job.get(
            "file"
        )
        
    if not file_path:
        raise HTTPException(
            404,
            "File is not ready."
        )
        
    path = Path(
        file_path
    )
    
    if not path.exists():
        raise HTTPException(
            404,
            "Output file no longer exists."
        )
        
    return FileResponse(
        path=str(
            path
        ),
        filename=path.name,
        media_type=(
            "application/octet-stream"
        )
    )


# ============================================================
# HEALTH
# ============================================================

@app.get(
    "/api/health"
)
async def api_health():
    deno = get_deno()
    
    ffmpeg = shutil.which(
        "ffmpeg"
    )
    ffprobe = shutil.which(
        "ffprobe"
    )
    
    try:
        ytdlp_version = (
            yt_dlp.version.__version__
        )
    except Exception:
        ytdlp_version = "unknown"
        
    try:
        import yt_dlp_ejs
        ejs_available = True
    except Exception:
        ejs_available = False
        
    return {
        "status": "ok",
        "app": APP_NAME,
        "version": APP_VERSION,
        "yt_dlp": ytdlp_version,
        "deno": bool(
            deno
        ),
        "deno_path": (
            deno or ""
        ),
        "ffmpeg": bool(
            ffmpeg
        ),
        "ffmpeg_path": (
            ffmpeg or ""
        ),
        "ffprobe": bool(
            ffprobe
        ),
        "ffprobe_path": (
            ffprobe or ""
        ),
        "yt_dlp_ejs": ejs_available,
        "max_playlist_items":
            MAX_PLAYLIST_ITEMS,
        "max_search_results":
            MAX_SEARCH_RESULTS,
        "max_concurrent_jobs":
            MAX_CONCURRENT_JOBS,
        "youtube_clients": [
            x[0]
            for x in
            YOUTUBE_CLIENT_STRATEGIES
        ],
        "info_methods": [
            "GET",
            "POST"
        ],
        "playlist_methods": [
            "GET",
            "POST"
        ],
        "download_methods": [
            "POST"
        ],
    }

# ============================================================
# YOUTUBE TEST ENDPOINT
# ============================================================

@app.get("/api/youtube-test")
def youtube_test():
    results = {}
    
    urls = {
        "youtube_home": "https://www.youtube.com/",
        "youtube_watch": "https://www.youtube.com/watch?v=62WUWa29iDE",
    }
    
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    
    for name, url in urls.items():
        try:
            request = urllib.request.Request(
                url,
                headers=headers,
                method="GET",
            )
            
            with urllib.request.urlopen(request, timeout=20) as response:
                data = response.read(512)
                
                results[name] = {
                    "ok": True,
                    "status": response.status,
                    "content_type": response.headers.get("Content-Type"),
                    "sample_bytes": len(data),
                }
                
        except urllib.error.HTTPError as e:
            results[name] = {
                "ok": False,
                "status": e.code,
                "reason": str(e.reason),
            }
            
        except Exception as e:
            results[name] = {
                "ok": False,
                "error": str(e),
            }
            
    return {
        "status": "ok",
        "tests": results,
    }


# ============================================================
# STARTUP
# ============================================================

@app.on_event(
    "startup"
)
async def startup_event():
    DOWNLOAD_ROOT.mkdir(
        parents=True,
        exist_ok=True
    )
    
    print(
        "=" * 70
    )
    print(
        APP_NAME
    )
    print(
        "Version:",
        APP_VERSION
    )
    print(
        "=" * 70
    )
    
    print(
        "yt-dlp:",
        getattr(
            yt_dlp.version,
            "__version__",
            "unknown"
        )
    )
    print(
        "Deno:",
        get_deno()
        or
        "NOT FOUND"
    )
    print(
        "FFmpeg:",
        shutil.which(
            "ffmpeg"
        )
        or
        "NOT FOUND"
    )
    print(
        "FFprobe:",
        shutil.which(
            "ffprobe"
        )
        or
        "NOT FOUND"
    )
    
    try:
        import yt_dlp_ejs
        print(
            "yt-dlp-ejs: OK"
        )
    except Exception as exc:
        print(
            "yt-dlp-ejs: NOT AVAILABLE",
            exc
        )
        
    print(
        "YouTube strategies:",
        ", ".join(
            x[0]
            for x in
            YOUTUBE_CLIENT_STRATEGIES
        )
    )
    print(
        "Download directory:",
        DOWNLOAD_ROOT
    )
    print(
        "INFO API: GET + POST"
    )
    print(
        "PLAYLIST API: GET + POST"
    )
    print(
        "SEARCH API: POST"
    )
    print(
        "DOWNLOAD API: POST"
    )
    print(
        "=" * 70
    )
