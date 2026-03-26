import asyncio
import hashlib
import html
from urllib.parse import quote_plus

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.ext import ContextTypes

from config import BOT_USERNAME
from services.animefire_client import get_anime_details, search_anime

INLINE_LIMIT = 8
INLINE_DETAILS_TIMEOUT = 4.2
INLINE_SEARCH_TIMEOUT = 4.5
INLINE_ANSWER_CACHE = 4

GENRE_PT_MAP = {
    "Action": "Ação",
    "Adventure": "Aventura",
    "Avant Garde": "Avant Garde",
    "Award Winning": "Premiado",
    "Boys Love": "Boys Love",
    "Cars": "Carros",
    "Comedy": "Comédia",
    "Demons": "Demônios",
    "Drama": "Drama",
    "Ecchi": "Ecchi",
    "Fantasy": "Fantasia",
    "Girls Love": "Girls Love",
    "Gourmet": "Culinária",
    "Harem": "Harém",
    "Historical": "Histórico",
    "Horror": "Terror",
    "Isekai": "Isekai",
    "Josei": "Josei",
    "Kids": "Infantil",
    "Magic": "Magia",
    "Mahou Shoujo": "Garota Mágica",
    "Martial Arts": "Artes Marciais",
    "Mecha": "Mecha",
    "Military": "Militar",
    "Music": "Música",
    "Mystery": "Mistério",
    "Parody": "Paródia",
    "Psychological": "Psicológico",
    "Racing": "Corrida",
    "Romance": "Romance",
    "Samurai": "Samurai",
    "School": "Escolar",
    "Sci-Fi": "Ficção Científica",
    "Seinen": "Seinen",
    "Shoujo": "Shoujo",
    "Shounen": "Shounen",
    "Slice of Life": "Slice of Life",
    "Space": "Espacial",
    "Sports": "Esportes",
    "Super Power": "Superpoderes",
    "Supernatural": "Sobrenatural",
    "Suspense": "Suspense",
    "Thriller": "Thriller",
    "Vampire": "Vampiro",
    "Work Life": "Vida Profissional",
}

STATUS_PT_MAP = {
    "Finished Airing": "Finalizado",
    "Currently Airing": "Em lançamento",
    "Not yet aired": "Não lançado",
    "Airing": "Em lançamento",
    "Completed": "Finalizado",
    "Upcoming": "Em breve",
    "RELEASING": "Em lançamento",
    "FINISHED": "Finalizado",
    "NOT_YET_RELEASED": "Não lançado",
    "CANCELLED": "Cancelado",
    "HIATUS": "Em hiato",
}

RATING_MAP = {
    "G": "Livre",
    "PG": "10+",
    "PG-13": "13+",
    "R": "16+",
    "R+": "18+",
    "RX": "18+",
}


def _safe_result_id(anime_id: str, index: int) -> str:
    raw = f"{anime_id}:{index}".encode("utf-8")
    return hashlib.md5(raw).hexdigest()


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _translate_genre(genre: str) -> str:
    return GENRE_PT_MAP.get((genre or "").strip(), genre)


def _translate_status(status: str) -> str:
    return STATUS_PT_MAP.get((status or "").strip(), status or "N/A")


def _translate_rating(anime: dict) -> str:
    raw = str(
        anime.get("rating")
        or anime.get("age_rating")
        or anime.get("classification")
        or anime.get("ageClassification")
        or ""
    ).upper().strip()
    return RATING_MAP.get(raw, raw or "N/A")


def _format_hashtag_genres(genres):
    if not genres:
        return "N/A"

    translated = [f"#{_translate_genre(g)}" for g in genres[:4] if g]

    if len(translated) <= 2:
        return ", ".join(translated)

    return ", ".join(translated[:2]) + "\n" + ", ".join(translated[2:])


def _pick_display_title(anime, fallback):
    return (
        anime.get("title")
        or anime.get("title_romaji")
        or anime.get("title_english")
        or fallback
        or "Sem título"
    ).strip()


def _pick_image(anime):
    return (
        anime.get("cover_url")
        or anime.get("media_image_url")
        or anime.get("banner_url")
        or ""
    ).strip()


def _extract_studio(anime):
    studios = anime.get("studios") or []
    if isinstance(studios, list) and studios:
        return ", ".join(str(s).strip() for s in studios[:2])
    return anime.get("studio") or "N/A"


def _build_anilist_url(anime, fallback, item):
    return anime.get("anilist_url") or f"https://anilist.co/search/anime?search={quote_plus(fallback)}"


def _build_trailer_url(anime):
    trailer = anime.get("trailer") or {}
    if isinstance(trailer, dict):
        if trailer.get("site") == "youtube" and trailer.get("id"):
            return f"https://youtube.com/watch?v={trailer['id']}"
    return ""


def _inline_keyboard(anime_id, anime, fallback_title, item):
    anilist_url = _build_anilist_url(anime, fallback_title, item)
    trailer_url = _build_trailer_url(anime)

    rows = [
        [InlineKeyboardButton("▶️ Assistir agora", url=f"https://t.me/{BOT_USERNAME}?start=anime_{anime_id}")]
    ]

    second = []

    if anilist_url:
        second.append(InlineKeyboardButton("🧾 Sinopse", url=anilist_url))

    if trailer_url:
        second.append(InlineKeyboardButton("🎬 Trailer", url=trailer_url))

    if second:
        rows.append(second)

    return InlineKeyboardMarkup(rows)


def _inline_message_text(anime, fallback):
    title = html.escape(_pick_display_title(anime, fallback))
    image = _pick_image(anime)

    text = f"<b>🎬 {title}</b>\n\n"
    text += "🔥 <i>Assista direto pelo bot.</i>"

    if image:
        text += f'<a href="{html.escape(image)}">\u200b</a>'

    return text


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inline = update.inline_query
    if not inline:
        return

    query = (inline.query or "").strip()
    if not query:
        await inline.answer([])
        return

    items = await search_anime(query)
    if not items:
        await inline.answer([])
        return

    results = []

    for i, item in enumerate(items[:INLINE_LIMIT]):
        anime_id = str(item.get("id"))
        anime = await get_anime_details(anime_id)

        title = _pick_display_title(anime, item.get("title"))
        text = _inline_message_text(anime, item.get("title"))
        keyboard = _inline_keyboard(anime_id, anime, item.get("title"), item)

        results.append(
            InlineQueryResultArticle(
                id=_safe_result_id(anime_id, i),
                title=title,
                input_message_content=InputTextMessageContent(text, parse_mode="HTML"),
                reply_markup=keyboard,
            )
        )

    await inline.answer(results, cache_time=INLINE_ANSWER_CACHE)
