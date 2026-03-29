import html
from typing import Any

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from utils.gatekeeper import ensure_channel_membership

TRACE_MOE_API = "https://api.trace.moe/search"
TRACE_MOE_ME_API = "https://api.trace.moe/me"

_HTTP_TIMEOUT = httpx.Timeout(45.0, connect=10.0, read=45.0, write=45.0)
_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}

# Similaridade abaixo de 90% tende a ser resultado ruim segundo a doc.
MIN_SIMILARITY = 0.90


def _seconds_to_hhmmss(seconds: float | int | None) -> str:
    try:
        total = int(float(seconds or 0))
    except Exception:
        total = 0

    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60

    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _pick_title(anilist_data: Any, fallback_filename: str = "") -> str:
    if isinstance(anilist_data, dict):
        title = anilist_data.get("title") or {}
        picked = (
            title.get("english")
            or title.get("romaji")
            or title.get("native")
            or ""
        ).strip()
        if picked:
            return picked

    fallback_filename = (fallback_filename or "").strip()
    return fallback_filename or "Anime desconhecido"


def _build_caption(item: dict) -> str:
    anilist_data = item.get("anilist")
    filename = str(item.get("filename") or "").strip()
    title = html.escape(_pick_title(anilist_data, filename))

    episode = item.get("episode")
    episode_text = str(episode) if episode not in (None, "", "null") else "N/A"

    similarity_raw = float(item.get("similarity") or 0.0)
    similarity_percent = round(similarity_raw * 100, 2)

    at_text = _seconds_to_hhmmss(item.get("at"))
    from_text = _seconds_to_hhmmss(item.get("from"))
    to_text = _seconds_to_hhmmss(item.get("to"))

    adult_text = "Sim" if isinstance(anilist_data, dict) and anilist_data.get("isAdult") else "Não"

    lines = [
        f"🎬 <b>{title}</b>",
        "",
        f"📺 <b>Episódio:</b> <code>{html.escape(episode_text)}</code>",
        f"⏱️ <b>Momento:</b> <code>{html.escape(at_text)}</code>",
        f"🎞️ <b>Trecho:</b> <code>{html.escape(from_text)} - {html.escape(to_text)}</code>",
        f"🎯 <b>Similaridade:</b> <code>{similarity_percent}%</code>",
        f"🔞 <b>Adulto:</b> <code>{adult_text}</code>",
    ]

    if filename:
        lines.append(f"📁 <b>Arquivo:</b> <code>{html.escape(filename[:120])}</code>")

    return "\n".join(lines)


def _build_keyboard(item: dict) -> InlineKeyboardMarkup | None:
    buttons = []

    video_url = str(item.get("video") or "").strip()
    image_url = str(item.get("image") or "").strip()

    if video_url:
        buttons.append([InlineKeyboardButton("🎬 Ver prévia", url=video_url)])

    if image_url:
        buttons.append([InlineKeyboardButton("🖼️ Frame da cena", url=image_url)])

    anilist_data = item.get("anilist")
    if isinstance(anilist_data, dict):
        anime_id = anilist_data.get("id")
        if anime_id:
            buttons.append([
                InlineKeyboardButton(
                    "📖 Abrir no AniList",
                    url=f"https://anilist.co/anime/{anime_id}",
                )
            ])

    return InlineKeyboardMarkup(buttons) if buttons else None


async def _trace_search_bytes(file_bytes: bytes, mime_type: str | None = None) -> dict:
    headers = dict(_HTTP_HEADERS)
    if mime_type:
        headers["Content-Type"] = mime_type

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=_HTTP_HEADERS) as client:
        response = await client.post(
            f"{TRACE_MOE_API}?anilistInfo",
            files={"image": ("image", file_bytes, mime_type or "application/octet-stream")},
        )
        response.raise_for_status()
        return response.json()


async def _trace_me() -> dict:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=_HTTP_HEADERS) as client:
        response = await client.get(TRACE_MOE_ME_API)
        response.raise_for_status()
        return response.json()


async def traceme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_channel_membership(update, context):
        return

    message = update.effective_message
    if not message:
        return

    await message.reply_text(
        "🖼️ <b>Me envie uma foto do anime</b>\n\n"
        "Pode ser print, frame ou cena.\n"
        "Eu vou tentar descobrir o anime, episódio e momento da cena.",
        parse_mode="HTML",
    )


async def tracequota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_channel_membership(update, context):
        return

    message = update.effective_message
    if not message:
        return

    msg = await message.reply_text("📊 <b>Consultando limite do trace.moe...</b>", parse_mode="HTML")

    try:
        data = await _trace_me()
        quota = data.get("quota", "N/A")
        quota_used = data.get("quotaUsed", "N/A")
        concurrency = data.get("concurrency", "N/A")
        priority = data.get("priority", "N/A")
        identifier = html.escape(str(data.get("id", "N/A")))

        await msg.edit_text(
            "📊 <b>Limites trace.moe</b>\n\n"
            f"👤 <b>ID:</b> <code>{identifier}</code>\n"
            f"📦 <b>Quota:</b> <code>{quota}</code>\n"
            f"📉 <b>Usado:</b> <code>{quota_used}</code>\n"
            f"⚡ <b>Concorrência:</b> <code>{concurrency}</code>\n"
            f"🏁 <b>Prioridade:</b> <code>{priority}</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        await msg.edit_text(
            "❌ <b>Não consegui consultar o trace.moe.</b>\n"
            f"<code>{html.escape(repr(e))}</code>",
            parse_mode="HTML",
        )


async def trace_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_channel_membership(update, context):
        return

    message = update.effective_message
    if not message or not message.photo:
        return

    status = await message.reply_text(
        "🔎 <b>Analisando imagem no trace.moe...</b>",
        parse_mode="HTML",
    )

    try:
        photo = message.photo[-1]
        tg_file = await photo.get_file()

        # baixa em memória
        file_bytes = await tg_file.download_as_bytearray()
        file_size = len(file_bytes)

        # A doc informa limite de 25 MB por busca.
        if file_size > 25 * 1024 * 1024:
            await status.edit_text(
                "❌ <b>A imagem passou de 25 MB.</b>\n"
                "Envie uma imagem menor.",
                parse_mode="HTML",
            )
            return

        data = await _trace_search_bytes(bytes(file_bytes), mime_type="image/jpeg")
        error_text = str(data.get("error") or "").strip()
        results = data.get("result") or []

        if error_text:
            await status.edit_text(
                f"❌ <b>Erro do trace.moe:</b>\n<code>{html.escape(error_text)}</code>",
                parse_mode="HTML",
            )
            return

        if not results:
            await status.edit_text(
                "🚫 <b>Não encontrei resultado para essa imagem.</b>",
                parse_mode="HTML",
            )
            return

        top = results[0]
        similarity = float(top.get("similarity") or 0.0)

        if similarity < MIN_SIMILARITY:
            await status.edit_text(
                "⚠️ <b>Encontrei algo, mas a similaridade ficou baixa.</b>\n\n"
                f"🎯 <b>Similaridade:</b> <code>{round(similarity * 100, 2)}%</code>\n"
                "Esse resultado pode estar errado. Tente um frame mais limpo da cena.",
                parse_mode="HTML",
            )
            return

        caption = _build_caption(top)
        keyboard = _build_keyboard(top)
        preview_image = str(top.get("image") or "").strip()

        try:
            await status.delete()
        except Exception:
            pass

        if preview_image:
            await message.reply_photo(
                photo=preview_image,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await message.reply_text(
                caption,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

    except httpx.HTTPStatusError as e:
        code = e.response.status_code if e.response else 0

        if code == 402:
            text = (
                "⚠️ <b>Limite temporário do trace.moe atingido.</b>\n"
                "Tente novamente em instantes."
            )
        elif code == 413:
            text = (
                "❌ <b>A imagem está grande demais.</b>\n"
                "Envie uma imagem menor que 25 MB."
            )
        elif code in (503, 504):
            text = (
                "⚠️ <b>O trace.moe está sobrecarregado agora.</b>\n"
                "Tente novamente daqui a pouco."
            )
        else:
            text = (
                "❌ <b>Falha ao consultar o trace.moe.</b>\n"
                f"<code>HTTP {code}</code>"
            )

        await status.edit_text(text, parse_mode="HTML")

    except Exception as e:
        await status.edit_text(
            "❌ <b>Não consegui analisar essa imagem.</b>\n"
            f"<code>{html.escape(repr(e))}</code>",
            parse_mode="HTML",
        )
