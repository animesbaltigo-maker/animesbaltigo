from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Awaitable, Callable

from config import API_HASH, API_ID, BOT_TOKEN, TELETHON_SESSION_NAME

_client = None
_enabled = False

ProgressCallback = Callable[[int, int], Awaitable[None] | None]


def telethon_configured() -> bool:
    return bool(API_ID and API_HASH and BOT_TOKEN)


async def start_telethon_uploader() -> bool:
    global _client, _enabled
    if _enabled and _client:
        return True

    if not telethon_configured():
        return False

    try:
        from telethon import TelegramClient

        session_path = Path(TELETHON_SESSION_NAME)
        session_path.parent.mkdir(parents=True, exist_ok=True)

        _client = TelegramClient(str(session_path), API_ID, API_HASH)
        await _client.start(bot_token=BOT_TOKEN)
        _enabled = True
        return True
    except Exception as error:
        print(f"[TELETHON_UPLOAD] disabled: {error!r}")
        _client = None
        _enabled = False
        return False


async def stop_telethon_uploader() -> None:
    global _client, _enabled
    if _client:
        try:
            await _client.disconnect()
        except Exception:
            pass
    _client = None
    _enabled = False


async def _probe_video(path: Path) -> dict:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {}

    proc = await asyncio.create_subprocess_exec(
        ffprobe,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return {}

    try:
        data = json.loads(stdout.decode("utf-8", errors="ignore"))
    except Exception:
        return {}

    video_stream = None
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video":
            video_stream = stream
            break

    duration = 0
    try:
        duration = int(float((data.get("format") or {}).get("duration") or 0))
    except Exception:
        duration = 0

    return {
        "duration": duration,
        "width": int((video_stream or {}).get("width") or 0),
        "height": int((video_stream or {}).get("height") or 0),
    }


async def _make_thumbnail(path: Path) -> Path | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None

    thumb = path.with_suffix(".thumb.jpg")
    proc = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        "00:00:01.000",
        "-i",
        str(path),
        "-vframes",
        "1",
        "-vf",
        "scale='min(320,iw)':-2",
        str(thumb),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    if proc.returncode == 0 and thumb.exists() and thumb.stat().st_size > 0:
        return thumb
    thumb.unlink(missing_ok=True)
    return None


async def send_file_with_telethon(
    chat_id: int,
    path: Path,
    caption: str,
    *,
    as_video: bool = True,
    progress_callback: ProgressCallback | None = None,
    protect_content: bool = True,
) -> bool:
    if not _enabled or not _client:
        ok = await start_telethon_uploader()
        if not ok:
            return False

    attrs = None
    thumb = None

    if as_video:
        try:
            from telethon.tl.types import DocumentAttributeVideo

            meta = await _probe_video(path)
            attrs = [
                DocumentAttributeVideo(
                    duration=int(meta.get("duration") or 0),
                    w=int(meta.get("width") or 0),
                    h=int(meta.get("height") or 0),
                    supports_streaming=True,
                )
            ]
        except Exception:
            attrs = None

        thumb = await _make_thumbnail(path)

    kwargs = {
        "caption": caption,
        "parse_mode": "html",
        "force_document": not as_video,
        "supports_streaming": as_video,
        "thumb": str(thumb) if thumb else None,
        "attributes": attrs,
        "progress_callback": progress_callback,
    }
    if protect_content:
        kwargs["noforwards"] = True

    try:
        try:
            await _client.send_file(chat_id, str(path), **kwargs)
        except TypeError as error:
            if protect_content:
                raise RuntimeError(
                    "Sua versao do Telethon nao aceitou bloqueio de encaminhamento. "
                    "Atualize com: pip install -U telethon"
                ) from error
            kwargs.pop("noforwards", None)
            await _client.send_file(chat_id, str(path), **kwargs)
    finally:
        if thumb:
            thumb.unlink(missing_ok=True)

    return True
