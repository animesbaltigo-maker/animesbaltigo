import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from config import DATA_DIR, OFFLINE_DOWNLOAD_MAX_BYTES


ARCHIVE_INDEX_PATH = Path(DATA_DIR) / "episode_file_ids.json"
DOWNLOAD_DIR = Path(DATA_DIR) / "offline_downloads"
FALLBACK_CHUNK_SIZE = 8 * 1024 * 1024

_LOCKS: dict[str, asyncio.Lock] = {}


def _archive_key(anime_id: str, episode: str, quality: str) -> str:
    anime_id = str(anime_id or "").strip()
    episode = str(episode or "").strip()
    quality = str(quality or "HD").strip().upper() or "HD"
    return f"{anime_id}|{episode}|{quality}"


def _lock_for(key: str) -> asyncio.Lock:
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


def _load_index() -> dict[str, dict[str, Any]]:
    if not ARCHIVE_INDEX_PATH.exists():
        return {}

    try:
        data = json.loads(ARCHIVE_INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

    return data if isinstance(data, dict) else {}


def _save_index(data: dict[str, dict[str, Any]]) -> None:
    os.makedirs(ARCHIVE_INDEX_PATH.parent, exist_ok=True)
    tmp_path = ARCHIVE_INDEX_PATH.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp_path.replace(ARCHIVE_INDEX_PATH)


def _extract_file_id(message) -> str:
    video = getattr(message, "video", None)
    if video and getattr(video, "file_id", None):
        return str(video.file_id)

    document = getattr(message, "document", None)
    if document and getattr(document, "file_id", None):
        return str(document.file_id)

    return ""


async def _deliver_cached_archive(
    *,
    bot,
    target_chat_id: int | str,
    archive_chat_id: int | str,
    entry: dict[str, Any],
    caption: str,
) -> bool:
    archive_message_id = entry.get("archive_message_id")
    if archive_message_id:
        try:
            await bot.copy_message(
                chat_id=target_chat_id,
                from_chat_id=entry.get("archive_chat_id") or archive_chat_id,
                message_id=int(archive_message_id),
                caption=caption[:1024],
            )
            return True
        except Exception:
            pass

    file_id = str(entry.get("file_id") or "").strip()
    if not file_id:
        return False

    await bot.send_video(
        chat_id=target_chat_id,
        video=file_id,
        caption=caption[:1024],
        supports_streaming=True,
    )
    return True


def _safe_filename(anime_id: str, episode: str, quality: str) -> str:
    raw = f"{anime_id}-ep{episode}-{quality}.mp4"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-") or "episode.mp4"


async def _run_aria2(url: str, target_path: Path) -> bool:
    aria2c = shutil.which("aria2c")
    if not aria2c or urlparse(url).scheme not in {"http", "https"}:
        return False

    partial_path = target_path.with_name(f"{target_path.name}.part")
    cmd = [
        aria2c,
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--continue=true",
        "--file-allocation=none",
        "--summary-interval=1",
        "--console-log-level=error",
        "--show-console-readout=false",
        "--max-connection-per-server=8",
        "--split=8",
        "--min-split-size=1M",
        "--max-tries=3",
        "--retry-wait=3",
        "--user-agent=Mozilla/5.0",
        "--dir",
        str(partial_path.parent),
        "--out",
        partial_path.name,
        url,
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return_code = await process.wait()
    if return_code != 0 or not partial_path.exists():
        partial_path.unlink(missing_ok=True)
        return False

    if partial_path.stat().st_size > OFFLINE_DOWNLOAD_MAX_BYTES:
        partial_path.unlink(missing_ok=True)
        raise ValueError("Arquivo maior que o limite configurado.")

    partial_path.replace(target_path)
    return True


async def _download_with_httpx(url: str, target_path: Path) -> None:
    partial_path = target_path.with_name(f"{target_path.name}.part")
    timeout = httpx.Timeout(connect=30, read=120, write=30, pool=30)
    current = 0

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0)
            if total and total > OFFLINE_DOWNLOAD_MAX_BYTES:
                raise ValueError("Arquivo maior que o limite configurado.")

            try:
                with partial_path.open("wb") as file:
                    async for chunk in response.aiter_bytes(FALLBACK_CHUNK_SIZE):
                        if not chunk:
                            continue
                        current += len(chunk)
                        if current > OFFLINE_DOWNLOAD_MAX_BYTES:
                            raise ValueError("Arquivo maior que o limite configurado.")
                        file.write(chunk)
            except Exception:
                partial_path.unlink(missing_ok=True)
                raise

    partial_path.replace(target_path)


async def _download_episode_file(video_url: str, anime_id: str, episode: str, quality: str) -> Path:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="anime-offline-", dir=DOWNLOAD_DIR))
    target_path = work_dir / _safe_filename(anime_id, episode, quality)

    try:
        if not await _run_aria2(video_url, target_path):
            await _download_with_httpx(video_url, target_path)
        return target_path
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


async def send_archived_episode(
    *,
    bot,
    target_chat_id: int | str,
    archive_chat_id: int | str,
    anime_id: str,
    anime_title: str,
    episode: str,
    quality: str,
    video_url: str,
) -> bool:
    key = _archive_key(anime_id, episode, quality)
    lock = _lock_for(key)

    async with lock:
        index = _load_index()
        entry = index.get(key) or {}
        file_id = str(entry.get("file_id") or "").strip()

        caption = (
            f"{anime_title}\n"
            f"Episodio: {episode}\n"
            f"Qualidade: {quality}"
        )

        if entry and await _deliver_cached_archive(
            bot=bot,
            target_chat_id=target_chat_id,
            archive_chat_id=archive_chat_id,
            entry=entry,
            caption=caption,
        ):
            return True

        if not file_id:
            archive_caption = (
                f"{caption}\n\n"
                f"ID: {anime_id}|{episode}|{quality}"
            )
            downloaded_path = await _download_episode_file(video_url, anime_id, episode, quality)
            try:
                with downloaded_path.open("rb") as video_file:
                    archived_message = await bot.send_video(
                        chat_id=archive_chat_id,
                        video=video_file,
                        filename=downloaded_path.name,
                        caption=archive_caption[:1024],
                        supports_streaming=True,
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                        pool_timeout=120,
                    )
            finally:
                shutil.rmtree(downloaded_path.parent, ignore_errors=True)

            file_id = _extract_file_id(archived_message)
            archive_message_id = getattr(archived_message, "message_id", None)
            if not file_id and not archive_message_id:
                return False

            index[key] = {
                "file_id": file_id,
                "anime_id": str(anime_id),
                "episode": str(episode),
                "quality": str(quality),
                "anime_title": str(anime_title),
                "archive_chat_id": str(archive_chat_id),
                "archive_message_id": archive_message_id,
                "created_at": int(time.time()),
            }
            _save_index(index)

        return await _deliver_cached_archive(
            bot=bot,
            target_chat_id=target_chat_id,
            archive_chat_id=archive_chat_id,
            entry=index[key],
            caption=caption,
        )
