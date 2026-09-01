import os
import sys
import re
import uuid
import json
import asyncio
import logging
import tempfile
import threading
import urllib.parse
from pathlib import Path
from typing import Dict, Any
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, BackgroundTasks, Response
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("YouTubeDownloader")

TEMP_DIR = Path(tempfile.gettempdir()) / "youtube_downloader_temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

executor = ThreadPoolExecutor(max_workers=8)
progress_store: Dict[str, Dict[str, Any]] = {}
cancel_store: Dict[str, bool] = {}
store_lock = threading.Lock()

def update_yt_engine():
    if getattr(sys, 'frozen', False):
        return
    try:
        import subprocess
        logger.info("Updating yt-dlp engine...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp", "--quiet"])
    except Exception as err:
        logger.warning(f"Engine update warning: {err}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        for old_file in TEMP_DIR.glob("*.part"):
            old_file.unlink()
        for old_file in TEMP_DIR.glob("*.ytdl"):
            old_file.unlink()
    except Exception as e:
        logger.warning(f"Temp cleanup warning: {e}")

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(executor, update_yt_engine)
    yield
    executor.shutdown(wait=False)

app = FastAPI(title="YouTube Downloader", version="8.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "http://127.0.0.1", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)

async def delayed_cleanup_task(filepath: Path, client_id: str = None, delay: int = 600):
    await asyncio.sleep(delay)
    try:
        if filepath and filepath.exists():
            filepath.unlink()
        if client_id:
            for extra_file in TEMP_DIR.glob(f"yt_{client_id}*"):
                if extra_file.exists():
                    extra_file.unlink()
    except Exception as e:
        logger.error(f"Cleanup error: {e}")
    if client_id:
        with store_lock:
            progress_store.pop(client_id, None)
            cancel_store.pop(client_id, None)

def clean_ansi_text(text: str) -> str:
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)

def format_bytes(size: float) -> str:
    if not size or size <= 0:
        return "Unknown MB"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"

def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned if cleaned else "youtube_download"

def get_resolution_metadata(height: int) -> tuple[int, str, str]:
    if height >= 4000:
        return 4320, "8K Ultra HD", "mkv"
    elif height >= 2000:
        return 2160, "4K Ultra HD", "mkv"
    elif height >= 1400:
        return 1440, "2K QHD", "mkv"
    elif height >= 1000:
        return 1080, "1080p Full HD", "mp4"
    elif height >= 700:
        return 720, "720p HD", "mp4"
    elif height >= 460:
        return 480, "480p SD", "mp4"
    elif height >= 340:
        return 360, "360p Low", "mp4"
    elif height >= 220:
        return 240, "240p Low", "mp4"
    else:
        return 144, "144p Low", "mp4"

def extract_yt_info(url: str) -> Dict[str, Any]:
    if "youtube.com" not in url and "youtu.be" not in url:
        raise ValueError("Please enter a valid YouTube video URL.")

    # Bot protection bypass added here
    opts = {
        'quiet': True, 
        'no_warnings': True, 
        'skip_download': True,
        'extractor_args': {'youtube': {'player_client': ['android', 'web']}}
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if 'entries' in info:
            info = info['entries'][0]

        formats = info.get('formats', [])
        unique_standards = {}

        for f in formats:
            vcodec = f.get('vcodec', 'none')
            height = f.get('height')
            fps = f.get('fps', 0)

            if vcodec != 'none' and height and height > 0:
                std_height, std_label, fmt_ext = get_resolution_metadata(height)
                v_size = f.get('filesize') or f.get('filesize_approx', 0)

                if std_height not in unique_standards or v_size > unique_standards[std_height]["filesize"]:
                    unique_standards[std_height] = {
                        "height": std_height,
                        "label": std_label,
                        "ext": fmt_ext,
                        "fps": fps,
                        "filesize": v_size
                    }

        formats_list = []
        for std_h in sorted(unique_standards.keys(), reverse=True):
            item = unique_standards[std_h]
            formats_list.append({
                "height": item["height"],
                "label": f"{item['label']} ({item['ext'].upper()})",
                "ext": item["ext"]
            })

        return {
            "title": info.get("title", "YouTube Video"),
            "uploader": info.get("uploader") or info.get("channel") or "YouTube Creator",
            "thumbnail": info.get("thumbnail") or (info.get("thumbnails", [{}])[-1].get("url", "")),
            "duration": info.get("duration", 0),
            "formats": formats_list
        }

@app.get("/api/info")
async def get_info(url: str):
    url = url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="Missing URL parameter.")
    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(executor, extract_yt_info, url)
        return JSONResponse(data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch metadata: {clean_ansi_text(str(e))}")

@app.get("/api/progress/{task_id}")
async def stream_progress(task_id: str):
    async def event_generator():
        max_iterations = 3000
        iterations = 0
        while iterations < max_iterations:
            iterations += 1
            with store_lock:
                if cancel_store.get(task_id):
                    yield f"data: {json.dumps({'status': 'cancelled', 'percent': 0})}\n\n"
                    break
                data = progress_store.get(task_id)
            if data:
                yield f"data: {json.dumps(data)}\n\n"
                if data.get("status") in ["completed", "error", "cancelled"]:
                    break
            else:
                yield f"data: {json.dumps({'status': 'pending', 'percent': 0})}\n\n"
            await asyncio.sleep(0.4)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/api/cancel/{task_id}")
async def cancel_task(task_id: str):
    with store_lock:
        cancel_store[task_id] = True
        current_p = progress_store.get(task_id, {})
        progress_store[task_id] = {
            "status": "cancelled",
            "percent": current_p.get("percent", 0),
            "downloaded": current_p.get("downloaded", "0 MB"),
            "total": current_p.get("total", "0 MB"),
            "message": "Download stopped by user"
        }
    return JSONResponse(status_code=200, content={"status": "cancelled"})

def sync_download_process(url: str, mode: str, quality: str, task_id: str, out_file_template: Path) -> Path:
    with store_lock:
        cancel_store[task_id] = False

    def progress_hook(d):
        if cancel_store.get(task_id):
            raise RuntimeError("Download stopped by user.")

        if d['status'] == 'downloading':
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            speed = d.get('speed', 0)
            eta = d.get('eta', 0)
            percent = (downloaded / total * 100) if total > 0 else 0

            with store_lock:
                progress_store[task_id] = {
                    "status": "downloading",
                    "percent": round(percent, 1),
                    "downloaded": format_bytes(downloaded),
                    "total": format_bytes(total),
                    "speed": f"{format_bytes(speed)}/s" if speed else "Processing...",
                    "eta": f"{eta}s" if eta else "--"
                }
        elif d['status'] == 'finished':
            with store_lock:
                progress_store[task_id] = {
                    "status": "processing",
                    "percent": 99.0,
                    "message": "Converting & Embedding Thumbnail..."
                }

    # Bot protection bypass added here
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'outtmpl': str(out_file_template),
        'progress_hooks': [progress_hook],
        'socket_timeout': 60,
        'continue_dl': True,
        'windowsfilenames': True,
        'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
    }

    if mode == "audio":
        ydl_opts.update({
            'format': 'bestaudio/best',
            'writethumbnail': True,
        })
        
        if quality == "wav":
            ydl_opts.update({
                'postprocessors': [
                    {'key': 'FFmpegExtractAudio', 'preferredcodec': 'wav'},
                    {'key': 'EmbedThumbnail'},
                    {'key': 'FFmpegMetadata'}
                ],
            })
        elif quality == "flac":
            ydl_opts.update({
                'postprocessors': [
                    {'key': 'FFmpegExtractAudio', 'preferredcodec': 'flac'},
                    {'key': 'EmbedThumbnail'},
                    {'key': 'FFmpegMetadata'}
                ],
            })
        elif quality == "m4a":
            ydl_opts.update({
                'format': 'bestaudio[ext=m4a]/bestaudio/best',
                'postprocessors': [
                    {'key': 'FFmpegExtractAudio', 'preferredcodec': 'm4a'},
                    {'key': 'EmbedThumbnail'},
                    {'key': 'FFmpegMetadata'}
                ],
            })
        else:
            mp3_quality = quality if quality in ["320", "256", "192", "128"] else "320"
            ydl_opts.update({
                'postprocessors': [
                    {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': mp3_quality},
                    {'key': 'EmbedThumbnail'},
                    {'key': 'FFmpegMetadata'}
                ],
            })
    else:
        target_height = int(quality) if quality.isdigit() else 4320
        is_mp4_target = target_height <= 1080
        ydl_opts.update({
            'format': f'bestvideo[height<={target_height}]+bestaudio/best[height<={target_height}]/best',
            'merge_output_format': 'mp4' if is_mp4_target else 'mkv',
        })

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:
        if cancel_store.get(task_id):
            raise RuntimeError("Download stopped by user.")
        raise exc

    matching_files = [
        f for f in TEMP_DIR.glob(f"yt_{task_id}.*") 
        if f.is_file() and not f.name.endswith((".part", ".ytdl", ".jpg", ".webp", ".png"))
    ]

    if not matching_files:
        raise RuntimeError("Download completed but converted file could not be found.")

    final_file = matching_files[0]
    formatted_final_size = format_bytes(final_file.stat().st_size)
    raw_title = info.get("title", "youtube_download")
    safe_title = sanitize_filename(raw_title)

    with store_lock:
        progress_store[task_id] = {
            "status": "completed",
            "percent": 100.0,
            "downloaded": formatted_final_size,
            "total": formatted_final_size,
            "filename": f"{safe_title[:60]}{final_file.suffix}"
        }

    return final_file

@app.get("/api/download")
async def download_media(url: str, mode: str = "video", quality: str = "4320", task_id: str = None, background_tasks: BackgroundTasks = None):
    url = url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="Missing YouTube video URL.")

    if not task_id:
        task_id = str(uuid.uuid4())

    temp_template = TEMP_DIR / f"yt_{task_id}.%(ext)s"

    try:
        loop = asyncio.get_running_loop()
        file_path = await loop.run_in_executor(
            executor, sync_download_process, url, mode, quality, task_id, temp_template
        )

        with store_lock:
            if cancel_store.get(task_id):
                return JSONResponse(status_code=200, content={"status": "cancelled"})
            completed_info = progress_store.get(task_id, {})

        download_filename = completed_info.get("filename", file_path.name)
        encoded_filename = urllib.parse.quote(download_filename)

        background_tasks.add_task(delayed_cleanup_task, file_path, task_id, 600)

        return FileResponse(
            path=file_path,
            media_type="application/octet-stream",
            headers={
                "Content-Length": str(file_path.stat().st_size),
                "Content-Disposition": f"attachment; filename=\"{download_filename}\"; filename*=UTF-8''{encoded_filename}"
            }
        )
    except Exception as e:
        clean_err = clean_ansi_text(str(e))
        with store_lock:
            if cancel_store.get(task_id):
                return JSONResponse(status_code=200, content={"status": "cancelled", "message": "Download paused by user."})
            progress_store[task_id] = {"status": "error", "error": clean_err}
        raise HTTPException(status_code=400, detail=f"Download Error: {clean_err}")

if getattr(sys, 'frozen', False):
    static_dir = Path(sys._MEIPASS)
else:
    static_dir = Path(__file__).parent

app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)