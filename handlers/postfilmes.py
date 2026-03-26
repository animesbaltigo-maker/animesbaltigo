import asyncio
import html
import json
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import RetryAfter
from telegram.ext import ContextTypes

from config import ADMIN_IDS, BOT_USERNAME, STICKER_DIVISOR
from services.animefire_client import get_anime_details
from services.filmes_client import get_all_movies


CANAL_FILMES = "@filmedeanimes"
POSTED_JSON_PATH = "data/filmes_postados.json"


def _is_admin(user_id: int | None) -> bool:
    if user_id is None:
        return False
    return user_id in ADMIN_IDS


def _ensure_parent_dir(filepath: str):
    parent = os.path.dirname(filepath)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _load_json_list(filepath: str) -> list:
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_json(filepath: str, data):
    _ensure_parent_dir(filepath)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _truncate_text(text: str, limit: int = 320) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _pick_main_title(anime: dict) -> str:
    return anime.get("title_romaji") or anime.get("title") or "Sem título"


def _pick_second_title(anime: dict) -> str:
    second = anime.get("title_english") or anime.get("title_native") or ""
    main = _pick_main_title(anime)

    if second and second.strip().lower() != main.strip().lower():
        return second
    return ""


def _format_status(status: str) -> str:
    return status or "N/A"


def _infer_audio(anime: dict) -> str:
    raw_title = (anime.get("title") or "").lower()
    raw_slug = (anime.get("id") or "").lower()

    if "dublado" in raw_title or "dublado" in raw_slug:
        return "Dublado"
    return "Legendado"


def _clean_description(description: str) -> str:
    description = (description or "").strip()

    bad_starts = [
        "este site não hospeda nenhum vídeo em seu servidor",
        "todo conteúdo é provido de terceiros não afiliados",
        "sinopse:",
    ]

    lowered = description.lower()
    for bad in bad_starts:
        if lowered.startswith(bad):
            return "Sem sinopse disponível."

    if lowered.startswith("sinopse:"):
        description = description[len("sinopse:"):].strip()

    return description or "Sem sinopse disponível."


def _normalize_genres(genres: list) -> list[str]:
    cleaned = []

    for g in genres or []:
        text = str(g or "").strip()
        if not text:
            continue

        text = text.lstrip("#").strip()
        if not text:
            continue

        cleaned.append(text)

    unique = []
    seen = set()

    for g in cleaned:
        key = g.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(g)

    return unique


def _format_genres(genres: list) -> str:
    normalized = _normalize_genres(genres)
    if not normalized:
        return "N/A"
    return ", ".join(f"#{g}" for g in normalized[:4])


def _build_caption(anime: dict) -> str:
    title_1 = html.escape(_pick_main_title(anime)).upper()
    title_2 = html.escape(_pick_second_title(anime))
    full_title = f"{title_1} | {title_2}" if title_2 else title_1

    genres_text = html.escape(_format_genres(anime.get("genres") or []))
    audio = html.escape(_infer_audio(anime))
    status = html.escape(_format_status(anime.get("status")))
    description = html.escape(_truncate_text(_clean_description(anime.get("description") or "")))

    return (
        f"🎬 <b>{full_title}</b>\n\n"
        f"<b>Gêneros:</b> <i>{genres_text}</i>\n"
        f"<b>Áudio:</b> <i>{audio}</i>\n"
        f"<b>Tipo:</b> <i>Filme</i>\n"
        f"<b>Status:</b> <i>{status}</i>\n\n"
        f"💬 <b>Sinopse:</b>\n"
        f"{description}"
    )


def _build_keyboard(anime: dict) -> InlineKeyboardMarkup:
    anime_id = anime["id"]
    anilist_url = anime.get("anilist_url") or ""

    trailer_url = ""
    trailer_id = anime.get("trailer_id") or ""
    trailer_site = (anime.get("trailer_site") or "").lower()

    if trailer_site == "youtube" and trailer_id:
        trailer_url = f"https://www.youtube.com/watch?v={trailer_id}"

    rows = [
        [
            InlineKeyboardButton(
                "▶️ Assistir agora",
                url=f"https://t.me/{BOT_USERNAME}?start=anime_{anime_id}"
            )
        ]
    ]

    second_row = []

    if trailer_url:
        second_row.append(InlineKeyboardButton("🎬 Trailer", url=trailer_url))

    if anilist_url:
        second_row.append(InlineKeyboardButton("⭐ AniList", url=anilist_url))

    if second_row:
        rows.append(second_row)

    return InlineKeyboardMarkup(rows)


async def _safe_send_photo(bot, **kwargs):
    while True:
        try:
            return await bot.send_photo(**kwargs)
        except RetryAfter as e:
            await asyncio.sleep(int(e.retry_after) + 1)


async def _safe_send_message(bot, **kwargs):
    while True:
        try:
            return await bot.send_message(**kwargs)
        except RetryAfter as e:
            await asyncio.sleep(int(e.retry_after) + 1)


async def _safe_send_sticker(bot, **kwargs):
    while True:
        try:
            return await bot.send_sticker(**kwargs)
        except RetryAfter as e:
            await asyncio.sleep(int(e.retry_after) + 1)


async def _post_one_movie(context: ContextTypes.DEFAULT_TYPE, anime_id: str) -> tuple[bool, str]:
    try:
        anime = await get_anime_details(anime_id)

        photo = (
            anime.get("media_image_url")
            or anime.get("cover_url")
            or anime.get("banner_url")
            or None
        )

        caption = _build_caption(anime)
        keyboard = _build_keyboard(anime)

        if photo:
            await _safe_send_photo(
                context.bot,
                chat_id=CANAL_FILMES,
                photo=photo,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await _safe_send_message(
                context.bot,
                chat_id=CANAL_FILMES,
                text=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

        await asyncio.sleep(1.2)

        await _safe_send_sticker(
            context.bot,
            chat_id=CANAL_FILMES,
            sticker=STICKER_DIVISOR,
        )

        await asyncio.sleep(1.5)

        return True, anime.get("title", anime_id)

    except Exception as e:
        print(f"[FILMES] erro ao postar {anime_id}: {repr(e)}")
        return False, anime_id


async def postfilmes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    message = update.effective_message

    if not _is_admin(user_id):
        await message.reply_text(
            "❌ <b>Você não tem permissão para usar este comando.</b>",
            parse_mode="HTML",
        )
        return

    limit = 0
    if context.args:
        try:
            limit = max(0, int(context.args[0]))
        except Exception:
            limit = 0

    msg = await message.reply_text(
        "🎞 <b>Coletando filmes...</b>",
        parse_mode="HTML",
    )

    try:
        movies = await get_all_movies()
        posted_ids = set(_load_json_list(POSTED_JSON_PATH))

        queue = [item for item in movies if item["id"] not in posted_ids]

        if limit > 0:
            queue = queue[:limit]

        if not queue:
            await msg.edit_text(
                "✅ <b>Nenhum filme novo para postar.</b>",
                parse_mode="HTML",
            )
            return

        success_count = 0
        fail_count = 0

        for idx, item in enumerate(queue, start=1):
            ok, title = await _post_one_movie(context, item["id"])

            if ok:
                posted_ids.add(item["id"])
                _save_json(POSTED_JSON_PATH, sorted(posted_ids))
                success_count += 1
            else:
                fail_count += 1

            try:
                await msg.edit_text(
                    f"🎞 <b>Postando filmes...</b>\n\n"
                    f"<b>Atual:</b> <code>{idx}/{len(queue)}</code>\n"
                    f"<b>Último:</b> <code>{html.escape(title)}</code>\n"
                    f"<b>Sucesso:</b> <code>{success_count}</code>\n"
                    f"<b>Falhas:</b> <code>{fail_count}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass

            await asyncio.sleep(1.0)

        await msg.edit_text(
            f"✅ <b>Filmes processados.</b>\n\n"
            f"<b>Total postados:</b> <code>{success_count}</code>\n"
            f"<b>Falhas:</b> <code>{fail_count}</code>",
            parse_mode="HTML",
        )

    except Exception as e:
        print("ERRO POSTFILMES:", repr(e))
        await msg.edit_text(
            "❌ <b>Não consegui processar os filmes.</b>",
            parse_mode="HTML",
        )