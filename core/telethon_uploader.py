from __future__ import annotations

from pathlib import Path

from config import API_HASH, API_ID, BOT_TOKEN, TELETHON_SESSION_NAME

_client = None
_enabled = False


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


async def send_file_with_telethon(
    chat_id: int,
    path: Path,
    caption: str,
    *,
    as_video: bool = True,
) -> bool:
    if not _enabled or not _client:
        ok = await start_telethon_uploader()
        if not ok:
            return False

    await _client.send_file(
        chat_id,
        str(path),
        caption=caption,
        parse_mode="html",
        force_document=not as_video,
        supports_streaming=as_video,
    )
    return True
