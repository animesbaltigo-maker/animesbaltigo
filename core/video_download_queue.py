from __future__ import annotations

import asyncio
import html
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from telegram.error import TelegramError, TimedOut

from config import (
    TELETHON_UPLOAD_MAX_MB,
    VIDEO_DOWNLOAD_CACHE_DIR,
    VIDEO_DOWNLOAD_MAX_MB,
    VIDEO_DOWNLOAD_PROTECT_CONTENT,
    VIDEO_DOWNLOAD_QUEUE_LIMIT,
    VIDEO_DOWNLOAD_WORKERS,
    VIDEO_UPLOAD_MAX_MB,
)
from core.telethon_uploader import send_file_with_telethon, telethon_configured

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://animefire.io/",
    "Origin": "https://animefire.io",
}

CHUNK_SIZE = 1024 * 1024
PROGRESS_INTERVAL = 3.0
MAX_BYTES = max(1, VIDEO_DOWNLOAD_MAX_MB) * 1024 * 1024
UPLOAD_MAX_BYTES = max(1, VIDEO_UPLOAD_MAX_MB) * 1024 * 1024
TELETHON_MAX_BYTES = max(1, TELETHON_UPLOAD_MAX_MB) * 1024 * 1024


@dataclass
class VideoDownloadJob:
    chat_id: int
    anime_id: str
    episode: str
    quality: str
    title: str
    video_url: str
    caption: str


_workers: list[asyncio.Task] = []
_active_jobs: dict[str, dict] = {}


def _job_key(anime_id: str, episode: str, quality: str) -> str:
    return f"{anime_id}|{episode}|{quality}".lower()


def _safe_filename(value: str, fallback: str = "episodio") -> str:
    value = html.unescape(value or "")
    value = re.sub(r"[^\w\s.-]", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip(" .-_")
    return value[:120] or fallback


def _extension_from_url(url: str) -> str:
    path = urlparse(url or "").path.lower()
    if path.endswith(".m3u8"):
        return ".m3u8"
    if path.endswith(".webm"):
        return ".webm"
    if path.endswith(".mkv"):
        return ".mkv"
    return ".mp4"


def _is_downloadable_url(url: str) -> bool:
    value = (url or "").lower()
    return bool(value.startswith("http"))


def _is_hls_url(url: str) -> bool:
    return ".m3u8" in (url or "").lower()


def _human_size(value: int | None) -> str:
    if not value:
        return "0 MB"
    mb = value / (1024 * 1024)
    if mb < 1024:
        return f"{mb:.1f} MB"
    return f"{mb / 1024:.2f} GB"


def _raise_if_too_large_for_upload(size: int) -> None:
    if size <= UPLOAD_MAX_BYTES:
        return
    if telethon_configured() and size <= TELETHON_MAX_BYTES:
        return
    raise RuntimeError(
        "O episodio foi encontrado, mas ficou grande demais para enviar pelo Bot API oficial.\n"
        f"Tamanho: {_human_size(size)}\n"
        f"Limite configurado: {_human_size(UPLOAD_MAX_BYTES)}\n\n"
        "Configure API_ID e API_HASH para ativar o uploader Telethon igual o Baixa Aqui."
    )


async def _safe_edit(message, text: str) -> None:
    try:
        await message.edit_text(text, parse_mode="HTML")
    except Exception:
        pass


async def _progress(entry: dict, job: VideoDownloadJob, downloaded: int, total: int | None) -> None:
    total_text = _human_size(total) if total else "calculando"
    pct = int((downloaded / total) * 100) if total else 0
    text = (
        "<b>Baixando episodio</b>\n\n"
        f"<b>Anime:</b> {html.escape(job.title)}\n"
        f"<b>Episodio:</b> {html.escape(str(job.episode))}\n"
        f"<b>Qualidade:</b> {html.escape(job.quality)}\n"
        f"<b>Progresso:</b> {pct}% ({_human_size(downloaded)} / {total_text})"
    )
    for message in list(entry["status_messages"]):
        await _safe_edit(message, text)


async def _download_file(job: VideoDownloadJob, entry: dict) -> Path:
    if not _is_downloadable_url(job.video_url):
        raise RuntimeError("Esse link de video nao pode ser baixado direto.")

    cache_dir = Path(VIDEO_DOWNLOAD_CACHE_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)

    filename = _safe_filename(f"{job.title} - EP {job.episode} - {job.quality}")
    target = cache_dir / f"{filename}{'.mp4' if _is_hls_url(job.video_url) else _extension_from_url(job.video_url)}"
    temp = cache_dir / f"{target.name}.part"

    if target.exists() and target.stat().st_size > 0:
        return target

    if _is_hls_url(job.video_url):
        return await _download_hls(job, entry, target, temp)

    timeout = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=HEADERS) as client:
        async with client.stream("GET", job.video_url) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0) or None
            if total and total > MAX_BYTES:
                raise RuntimeError(f"Arquivo muito grande para enviar: {_human_size(total)}.")
            if total:
                _raise_if_too_large_for_upload(total)

            downloaded = 0
            last_progress = 0.0
            with open(temp, "wb") as file:
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > MAX_BYTES:
                        raise RuntimeError(f"Arquivo passou do limite de {_human_size(MAX_BYTES)}.")
                    file.write(chunk)

                    now = time.monotonic()
                    if now - last_progress >= PROGRESS_INTERVAL:
                        last_progress = now
                        await _progress(entry, job, downloaded, total)

    temp.replace(target)
    _raise_if_too_large_for_upload(target.stat().st_size)
    return target


async def _download_hls(job: VideoDownloadJob, entry: dict, target: Path, temp: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("Esse episodio veio em stream HLS. Instale ffmpeg no servidor para baixar offline.")

    await _progress(entry, job, 0, None)

    headers = (
        f"User-Agent: {HEADERS['User-Agent']}\r\n"
        f"Referer: {HEADERS['Referer']}\r\n"
        f"Origin: {HEADERS['Origin']}\r\n"
    )
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-headers",
        headers,
        "-i",
        job.video_url,
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        "-f",
        "mp4",
        str(temp),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        message = stderr.decode("utf-8", errors="ignore").strip()[-500:]
        raise RuntimeError(message or "O ffmpeg nao conseguiu baixar esse stream.")

    if not temp.exists() or temp.stat().st_size <= 0:
        raise RuntimeError("O download terminou sem gerar arquivo.")

    if temp.stat().st_size > MAX_BYTES:
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"Arquivo passou do limite de {_human_size(MAX_BYTES)}.")

    temp.replace(target)
    _raise_if_too_large_for_upload(target.stat().st_size)
    return target


async def _send_video_safe(bot, chat_id: int, path: Path, caption: str) -> bool:
    size = path.stat().st_size

    if telethon_configured():
        if size > TELETHON_MAX_BYTES:
            raise RuntimeError(
                f"Arquivo maior que o limite Telethon configurado: {_human_size(size)} > {_human_size(TELETHON_MAX_BYTES)}."
            )
        sent = await send_file_with_telethon(chat_id, path, caption, as_video=True)
        if sent:
            return True
        if size > UPLOAD_MAX_BYTES:
            raise RuntimeError(
                "Arquivo grande demais para Bot API e o uploader Telethon nao conseguiu iniciar.\n"
                "Confira API_ID, API_HASH e se telethon esta instalado."
            )

    if size > UPLOAD_MAX_BYTES:
        raise RuntimeError(
            "Arquivo grande demais para Bot API e o uploader Telethon nao esta configurado.\n"
            "Preencha API_ID e API_HASH no .env."
        )

    _raise_if_too_large_for_upload(size)

    try:
        with open(path, "rb") as file:
            await bot.send_video(
                chat_id=chat_id,
                video=file,
                filename=path.name,
                caption=caption,
                parse_mode="HTML",
                supports_streaming=True,
                protect_content=VIDEO_DOWNLOAD_PROTECT_CONTENT,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
                pool_timeout=30,
            )
        return True
    except TimedOut:
        try:
            await bot.send_message(chat_id, "O envio demorou mais que o esperado. Confere se o video ja chegou.")
        except Exception:
            pass
        return True
    except TelegramError as error:
        if "request entity too large" in str(error).lower():
            raise RuntimeError(
                "O Telegram recusou o upload porque o arquivo e grande demais para o Bot API oficial.\n"
                f"Tamanho: {_human_size(path.stat().st_size)}\n"
                "Use Telegram Bot API local ou Telethon para episodios grandes."
            ) from error
        with open(path, "rb") as file:
            await bot.send_document(
                chat_id=chat_id,
                document=file,
                filename=path.name,
                caption=caption,
                parse_mode="HTML",
                protect_content=VIDEO_DOWNLOAD_PROTECT_CONTENT,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
                pool_timeout=30,
            )
        return True


async def _process_job(app, job: VideoDownloadJob) -> None:
    key = _job_key(job.anime_id, job.episode, job.quality)
    entry = _active_jobs.get(key)
    if not entry:
        return

    try:
        await _progress(entry, job, 0, None)
        path = await _download_file(job, entry)

        for message in list(entry["status_messages"]):
            await _safe_edit(
                message,
                (
                    "<b>Enviando episodio</b>\n\n"
                    f"<b>Anime:</b> {html.escape(job.title)}\n"
                    f"<b>Episodio:</b> {html.escape(str(job.episode))}\n"
                    f"<b>Tamanho:</b> {_human_size(path.stat().st_size)}"
                ),
            )

        for waiter in entry["waiters"]:
            await _send_video_safe(app.bot, waiter["chat_id"], path, waiter["caption"])

        for message in list(entry["status_messages"]):
            await _safe_edit(
                message,
                (
                    "<b>Episodio enviado</b>\n\n"
                    f"<b>Anime:</b> {html.escape(job.title)}\n"
                    f"<b>Episodio:</b> {html.escape(str(job.episode))}"
                ),
            )
    except Exception as error:
        for message in list(entry["status_messages"]):
            await _safe_edit(message, f"<b>Falha ao baixar episodio:</b>\n<code>{html.escape(str(error))}</code>")
    finally:
        _active_jobs.pop(key, None)


async def _worker(app, queue: asyncio.Queue) -> None:
    while True:
        job = await queue.get()
        try:
            if job is None:
                return
            await _process_job(app, job)
        finally:
            queue.task_done()


async def enqueue_video_download(app, job: VideoDownloadJob) -> int:
    queue = app.bot_data["video_download_queue"]
    key = _job_key(job.anime_id, job.episode, job.quality)

    if key in _active_jobs:
        entry = _active_jobs[key]
        entry["waiters"].append({"chat_id": job.chat_id, "caption": job.caption})
        status = await app.bot.send_message(
            job.chat_id,
            (
                "<b>Pedido recebido</b>\n\n"
                f"<b>Anime:</b> {html.escape(job.title)}\n"
                f"<b>Episodio:</b> {html.escape(str(job.episode))}\n"
                "Status: <b>ja esta sendo preparado</b>"
            ),
            parse_mode="HTML",
        )
        entry["status_messages"].append(status)
        return queue.qsize()

    status = await app.bot.send_message(
        job.chat_id,
        (
            "<b>Pedido recebido</b>\n\n"
            f"<b>Anime:</b> {html.escape(job.title)}\n"
            f"<b>Episodio:</b> {html.escape(str(job.episode))}\n"
            "Status: <b>na fila</b>"
        ),
        parse_mode="HTML",
    )

    _active_jobs[key] = {
        "waiters": [{"chat_id": job.chat_id, "caption": job.caption}],
        "status_messages": [status],
    }
    await queue.put(job)
    return queue.qsize()


async def start_video_download_workers(app) -> None:
    if app.bot_data.get("video_download_workers_started"):
        return

    app.bot_data["video_download_queue"] = asyncio.Queue(maxsize=VIDEO_DOWNLOAD_QUEUE_LIMIT)
    worker_count = max(1, VIDEO_DOWNLOAD_WORKERS)
    for _ in range(worker_count):
        _workers.append(asyncio.create_task(_worker(app, app.bot_data["video_download_queue"])))

    app.bot_data["video_download_workers_started"] = True


async def stop_video_download_workers(app) -> None:
    queue = app.bot_data.get("video_download_queue")
    if queue is None:
        return
    for _ in _workers:
        await queue.put(None)
    await asyncio.gather(*_workers, return_exceptions=True)
    _workers.clear()
    app.bot_data["video_download_workers_started"] = False
