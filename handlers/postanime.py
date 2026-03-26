import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import ADMIN_IDS, BOT_USERNAME, CANAL_POSTAGEM, STICKER_DIVISOR
from services.animefire_client import get_anime_details, search_anime


def _truncate_text(text: str, limit: int = 320) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _is_admin(user_id: int | None) -> bool:
    if user_id is None:
        return False
    return user_id in ADMIN_IDS


def _pick_main_title(anime: dict) -> str:
    return (
        anime.get("title_romaji")
        or anime.get("title")
        or "Sem título"
    )


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


def _build_caption(anime: dict) -> str:
    title_1 = html.escape(_pick_main_title(anime)).upper()
    title_2 = html.escape(_pick_second_title(anime))

    full_title = f"{title_1} | {title_2}" if title_2 else title_1

    genres = anime.get("genres") or []
    genres_text = ", ".join(f"#{g}" for g in genres[:4]) if genres else "N/A"
    genres_text = html.escape(genres_text)

    audio = html.escape(_infer_audio(anime))
    episodes = html.escape(str(anime.get("episodes") or "?"))
    status = html.escape(_format_status(anime.get("status")))
    description = _clean_description(anime.get("description") or "")
    description = html.escape(_truncate_text(description))

    return (
        f"🎬 <b>{full_title}</b>\n\n"
        f"<b>Gêneros:</b> <i>{genres_text}</i>\n"
        f"<b>Episódios:</b> <i>{episodes}</i>\n"
        f"<b>Status:</b> <i>{status}</i>\n\n"
        f"💬 {description}"
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
        second_row.append(
            InlineKeyboardButton("🎬 Trailer", url=trailer_url)
        )

    if anilist_url:
        second_row.append(
            InlineKeyboardButton("⭐ AniList", url=anilist_url)
        )

    if second_row:
        rows.append(second_row)

    return InlineKeyboardMarkup(rows)


async def postanime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None

    if not _is_admin(user_id):
        await update.effective_message.reply_text(
            "❌ <b>Você não tem permissão para usar este comando.</b>",
            parse_mode="HTML",
        )
        return

    if not context.args:
        await update.effective_message.reply_text(
            "❌ <b>Faltou o nome do anime.</b>\n\n"
            "Use assim:\n"
            "<code>/postanime nome do anime</code>\n\n"
            "📌 <b>Exemplo:</b>\n"
            "<code>/postanime one piece</code>",
            parse_mode="HTML",
        )
        return

    query = " ".join(context.args).strip()

    msg = await update.effective_message.reply_text(
        "📤 <b>Montando postagem...</b>",
        parse_mode="HTML",
    )

    try:
        results = await search_anime(query)

        if not results:
            await msg.edit_text(
                "❌ <b>Não encontrei esse anime.</b>",
                parse_mode="HTML",
            )
            return

        anime_id = results[0]["id"]
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
            await context.bot.send_photo(
                chat_id=CANAL_POSTAGEM,
                photo=photo,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await context.bot.send_message(
                chat_id=CANAL_POSTAGEM,
                text=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

        await context.bot.send_sticker(
            chat_id=CANAL_POSTAGEM,
            sticker=STICKER_DIVISOR,
        )

        await msg.edit_text(
            f"✅ <b>Anime postado no canal.</b>\n\n"
            f"<code>{anime.get('title', anime_id)}</code>",
            parse_mode="HTML",
        )

    except Exception as e:
        print("ERRO POSTANIME:", repr(e))
        await msg.edit_text(
            "❌ <b>Não consegui postar esse anime.</b>",
            parse_mode="HTML",
        )
