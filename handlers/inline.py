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
    genre = (genre or "").strip()
    return GENRE_PT_MAP.get(genre, genre)


def _translate_status(status: str) -> str:
    status = (status or "").strip()
    return STATUS_PT_MAP.get(status, status or "N/A")


def _translate_rating(anime: dict) -> str:
    raw = (
        anime.get("rating")
        or anime.get("age_rating")
        or anime.get("classification")
        or anime.get("ageClassification")
        or ""
    )
    raw = str(raw or "").strip().upper()
    return RATING_MAP.get(raw, raw or "N/A")


def _format_hashtag_genres(genres: list[str]) -> str:
    if not genres:
        return "N/A"

    translated = []
    for genre in genres[:4]:
        value = _translate_genre(str(genre)).strip()
        if value:
            translated.append(f"#{value}")

    if not translated:
        return "N/A"

    if len(translated) <= 2:
        return ", ".join(translated)

    return ", ".join(translated[:2]) + "\n" + ", ".join(translated[2:])


def _pick_display_title(anime: dict, fallback_title: str) -> str:
    return (
        anime.get("title")
        or anime.get("title_romaji")
        or anime.get("title_english")
        or fallback_title
        or "Sem título"
    ).strip()


def _extract_alt_titles(anime: dict, fallback_item: dict) -> list[str]:
    values = (
        anime.get("alt_titles")
        or anime.get("alternative_titles")
        or fallback_item.get("alt_titles")
        or []
    )

    clean = []
    seen = set()

    for value in values:
        value = str(value or "").strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        clean.append(value)

    return clean


def _pick_image(anime: dict) -> str:
    return (
        anime.get("cover_url")
        or anime.get("media_image_url")
        or anime.get("banner_url")
        or ""
    ).strip()


def _extract_studio(anime: dict) -> str:
    candidates = (
        anime.get("studio")
        or anime.get("studios")
        or anime.get("studio_name")
        or anime.get("producer")
        or []
    )

    if isinstance(candidates, str):
        value = candidates.strip()
        return value or "N/A"

    if isinstance(candidates, list):
        names = []
        for item in candidates:
            if isinstance(item, dict):
                name = (
                    item.get("name")
                    or item.get("studio")
                    or item.get("title")
                    or ""
                )
            else:
                name = str(item or "")
            name = name.strip()
            if name and name not in names:
                names.append(name)
        return ", ".join(names[:2]) if names else "N/A"

    return "N/A"


def _extract_type(anime: dict, item: dict, is_dubbed: bool) -> str:
    if is_dubbed:
        return "DUBLADO"

    anime_type = (
        anime.get("type")
        or anime.get("format")
        or item.get("type")
        or item.get("format")
        or ""
    )
    anime_type = str(anime_type or "").strip().upper()

    type_map = {
        "TV": "TV",
        "TV_SHORT": "TV",
        "MOVIE": "MOVIE",
        "SPECIAL": "SPECIAL",
        "OVA": "OVA",
        "ONA": "ONA",
        "MUSIC": "MUSIC",
    }

    return type_map.get(anime_type, anime_type or "ANIME")


def _build_anilist_url(anime: dict, fallback_title: str, fallback_item: dict) -> str:
    explicit = (
        anime.get("anilist_url")
        or anime.get("ani_list_url")
        or anime.get("anilist")
        or ""
    )
    if explicit:
        return str(explicit).strip()

    anilist_id = anime.get("anilist_id") or anime.get("anilistId")
    if anilist_id:
        return f"https://anilist.co/anime/{anilist_id}"

    alt_titles = _extract_alt_titles(anime, fallback_item)
    search_title = (
        anime.get("title_english")
        or anime.get("title_romaji")
        or (alt_titles[0] if alt_titles else "")
        or fallback_title
        or "anime"
    ).strip()

    return f"https://anilist.co/search/anime?search={quote_plus(search_title)}"


def _build_trailer_url(anime: dict) -> str:
    trailer = anime.get("trailer") or {}

    if isinstance(trailer, dict):
        site = str(trailer.get("site") or "").lower().strip()
        trailer_id = str(trailer.get("id") or "").strip()
        if site == "youtube" and trailer_id:
            return f"https://www.youtube.com/watch?v={trailer_id}"

    trailer_site = str(anime.get("trailer_site") or "").lower().strip()
    trailer_id = str(anime.get("trailer_id") or "").strip()
    if trailer_site == "youtube" and trailer_id:
        return f"https://www.youtube.com/watch?v={trailer_id}"

    direct = anime.get("trailer_url") or anime.get("youtube_trailer") or ""
    return str(direct).strip()


def _inline_keyboard(anime_id: str, anime: dict, fallback_title: str, fallback_item: dict) -> InlineKeyboardMarkup:
    anilist_url = _build_anilist_url(anime, fallback_title, fallback_item)
    trailer_url = _build_trailer_url(anime)

    rows = [
        [
            InlineKeyboardButton(
                text="▶️ Assistir agora",
                url=f"https://t.me/{BOT_USERNAME}?start=anime_{anime_id}",
                icon_custom_emoji_id=EMOJI_PLAY,
            )
        ]
    ]

    second_row = []

    if anilist_url:
        second_row.append(
            InlineKeyboardButton(
                text="🧾 Sinopse",
                url=anilist_url,
                icon_custom_emoji_id=EMOJI_INFO,
            )
        )

    if trailer_url:
        second_row.append(
            InlineKeyboardButton(
                text="🎬 Trailer",
                url=trailer_url,
                icon_custom_emoji_id=EMOJI_TRAILER,
            )
        )

    if second_row:
        rows.append(second_row)

    return InlineKeyboardMarkup(rows)


def _inline_message_text(anime: dict, fallback_title: str) -> str:
    title = html.escape(_pick_display_title(anime, fallback_title))
    image_url = _pick_image(anime)

    genres = anime.get("genres") or []
    genres_text = _format_hashtag_genres(genres)

    year = anime.get("season_year") or anime.get("year") or "N/A"
    status = _translate_status(str(anime.get("status") or ""))
    episodes = anime.get("episodes") or "N/A"
    rating = _translate_rating(anime)
    studio = _extract_studio(anime)

    if image_url:
        title_line = f'<b><a href="{html.escape(image_url, quote=True)}">🎬</a> {title}</b>'
    else:
        title_line = f"<b>🎬 {title}</b>"

    text = (
        f"{title_line}\n\n"
        f"<b>Gênero:</b> <i>{html.escape(genres_text)}</i>\n"
        f"<b>Ano:</b> <i>{html.escape(str(year))}</i>\n"
        f"<b>Status:</b> <i>{html.escape(str(status))}</i>\n"
        f"<b>Total Episódios:</b> <i>{html.escape(str(episodes))}</i>\n"
        f"<b>Studio:</b> <i>{html.escape(str(studio))}</i>\n"
        f"<b>Classificação:</b> <i>{html.escape(str(rating))}</i>\n\n"
        f"🔥 <i>Assista direto pelo bot, do jeito mais simples e completo.</i>"
    )

    if image_url:
        text += f'<a href="{html.escape(image_url, quote=True)}">\u200b</a>'

    return text


def _inline_description(anime: dict, fallback_item: dict, is_dubbed: bool) -> str:
    anime_type = _extract_type(anime, fallback_item, is_dubbed)
    studio = _extract_studio(anime)

    if studio != "N/A" and anime_type != "DUBLADO":
        return f"{anime_type} • {studio}"

    return anime_type


async def _get_details_safe(anime_id: str) -> dict:
    try:
        return await asyncio.wait_for(
            get_anime_details(anime_id),
            timeout=INLINE_DETAILS_TIMEOUT,
        )
    except Exception as e:
        print("[INLINE][DETAILS]", anime_id, repr(e))
        return {}


def _build_fallback_details(item: dict) -> dict:
    title = str(item.get("title") or "Sem título").strip()

    return {
        "id": item.get("id"),
        "title": title,
        "alt_titles": item.get("alt_titles") or [],
        "description": "",
        "cover_url": "",
        "banner_url": "",
        "media_image_url": "",
        "score": None,
        "status": "",
        "episodes": None,
        "season_year": None,
        "genres": [],
        "anilist_id": None,
        "anilist_url": "",
        "title_romaji": "",
        "title_english": "",
        "title_native": "",
        "trailer_id": "",
        "trailer_site": "",
        "type": item.get("type") or "",
        "format": item.get("format") or "",
        "studio": item.get("studio") or "",
        "studios": item.get("studios") or [],
    }


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inline = update.inline_query
    if not inline:
        return

    query = (inline.query or "").strip()

    if not query:
        try:
            await inline.answer([], cache_time=2, is_personal=True)
        except Exception:
            pass
        return

    try:
        items = await asyncio.wait_for(
            search_anime(query),
            timeout=INLINE_SEARCH_TIMEOUT,
        )
    except Exception as e:
        print("[INLINE][SEARCH]", repr(e))
        try:
            await inline.answer([], cache_time=1, is_personal=True)
        except Exception:
            pass
        return

    if not items:
        try:
            await inline.answer([], cache_time=2, is_personal=True)
        except Exception:
            pass
        return

    ordered_items = items[:INLINE_LIMIT]

    detail_values = await asyncio.gather(
        *[_get_details_safe(str(item.get("id") or "").strip()) for item in ordered_items],
        return_exceptions=False,
    )

    results = []

    for index, (item, details) in enumerate(zip(ordered_items, detail_values)):
        anime_id = str(item.get("id") or "").strip()
        fallback_title = str(item.get("title") or "Sem título").strip()

        if not anime_id:
            continue

        merged = _build_fallback_details(item)
        if isinstance(details, dict) and details:
            merged.update({k: v for k, v in details.items() if v not in (None, "", [])})

        title = _pick_display_title(merged, fallback_title)
        is_dubbed = bool(item.get("is_dubbed"))

        title_display = f"{title} [DUBLADO]" if is_dubbed else title
        description = _inline_description(merged, item, is_dubbed)
        text = _inline_message_text(merged, fallback_title)
        keyboard = _inline_keyboard(anime_id, merged, fallback_title, item)
        image_url = _pick_image(merged)

        result_id = _safe_result_id(anime_id, index)

        results.append(
            InlineQueryResultArticle(
                id=result_id,
                title=_truncate(title_display, 64),
                description=description,
                thumbnail_url=image_url if image_url else None,
                input_message_content=InputTextMessageContent(
                    text,
                    parse_mode="HTML",
                ),
                reply_markup=keyboard,
            )
        )

    try:
        await inline.answer(
            results,
            cache_time=INLINE_ANSWER_CACHE,
            is_personal=True,
        )
    except Exception as e:
        print("[INLINE][ANSWER]", repr(e))
