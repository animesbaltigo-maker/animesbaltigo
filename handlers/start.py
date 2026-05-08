import asyncio
import html
import json
import time
import re
from pathlib import Path
from urllib.parse import quote_plus

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import BOT_BRAND, BOT_USERNAME, DATA_DIR, HTTP_TIMEOUT
from services.animefire_client import get_anime_details, get_episode_player, search_anime
from services.metrics import is_episode_watched
from services.referral_db import (
    create_referral,
    init_referral_db,
    register_interaction,
    register_referral_click,
    try_qualify_referral,
    upsert_user,
)
from services.user_registry import register_user
from utils.gatekeeper import ensure_channel_membership


def _clean_anime_title(title: str) -> str:
    if not title:
        return "Sem título"
    return re.sub(r"\s*-\s*Epis[oó]dio\s*\d+", "", title, flags=re.IGNORECASE)


BANNER_URL = "https://photo.chelpbot.me/AgACAgEAAxkBZ987imm1UGdjCzV5n7FN2F6Ayew0umj2AAJkC2sbJAWhRWilm7WSjeD5AQADAgADeQADOgQ/photo.jpg"
MINIAPP_URL = "https://rough-double-remarkable-north.trycloudflare.com/miniapp/index.html"

START_COOLDOWN = 1.2
START_DEEP_LINK_TTL = 8.0
ANILIST_LINKS_JSON_PATH = Path(DATA_DIR) / "anilist_anime_links.json"
ANILIST_API_URL = "https://graphql.anilist.co"

_START_USER_LOCKS = {}
_START_INFLIGHT = {}


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


def _translate_genre(genre: str) -> str:
    genre = (genre or "").strip()
    return GENRE_PT_MAP.get(genre, genre)


def _translate_status(status: str) -> str:
    status = (status or "").strip()
    if not status or status.upper() in {"N/A", "NA", "NONE", "NULL", "-"}:
        return ""
    return STATUS_PT_MAP.get(status, status)


def _translate_rating(anime: dict) -> str:
    candidates = [
        anime.get("rating"),
        anime.get("age_rating"),
        anime.get("classification"),
        anime.get("ageClassification"),
        anime.get("ageRestriction"),
        anime.get("rated"),
    ]

    media = anime.get("media") or {}
    if isinstance(media, dict):
        candidates.extend([
            media.get("rating"),
            media.get("age_rating"),
            media.get("classification"),
            media.get("ageClassification"),
        ])

    for raw in candidates:
        if raw is None:
            continue

        if isinstance(raw, dict):
            raw = (
                raw.get("label")
                or raw.get("name")
                or raw.get("value")
                or raw.get("rating")
                or ""
            )

        raw = str(raw or "").strip()
        if not raw:
            continue

        normalized = raw.upper()
        return RATING_MAP.get(normalized, raw)

    return ""


def _format_hashtag_genres(genres: list[str]) -> str:
    if not genres:
        return ""

    translated = []
    for genre in genres:
        value = _translate_genre(str(genre)).strip()
        if value and value.lower() not in {"legendado", "dublado", "animes legendados", "animes dublados", "animeplay"}:
            translated.append(f"#{value}")
        if len(translated) >= 4:
            break

    if not translated:
        return ""

    if len(translated) <= 2:
        return ", ".join(translated)

    return ", ".join(translated[:2]) + "\n" + ", ".join(translated[2:])


def _pick_display_title(anime: dict, fallback_title: str = "Sem título") -> str:
    return (
        anime.get("title")
        or anime.get("title_romaji")
        or anime.get("title_english")
        or anime.get("name")
        or fallback_title
        or "Sem título"
    ).strip()


def _extract_alt_titles(anime: dict, fallback_item: dict | None = None) -> list[str]:
    fallback_item = fallback_item or {}

    values = (
        anime.get("alt_titles")
        or anime.get("alternative_titles")
        or anime.get("synonyms")
        or fallback_item.get("alt_titles")
        or []
    )

    clean = []
    seen = set()

    for value in values:
        if isinstance(value, dict):
            value = value.get("title") or value.get("name") or value.get("value") or ""
        value = str(value or "").strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        clean.append(value)

    return clean


def _extract_studio(anime: dict) -> str:
    candidates = []

    direct_candidates = [
        anime.get("studio"),
        anime.get("studios"),
        anime.get("studio_name"),
        anime.get("producer"),
        anime.get("producers"),
        anime.get("animation_studio"),
        anime.get("animationStudio"),
    ]

    media = anime.get("media") or {}
    if isinstance(media, dict):
        direct_candidates.extend([
            media.get("studio"),
            media.get("studios"),
            media.get("studio_name"),
            media.get("producer"),
            media.get("producers"),
        ])

        studios_node = media.get("studios")
        if isinstance(studios_node, dict):
            direct_candidates.extend([
                studios_node.get("nodes"),
                studios_node.get("edges"),
                studios_node.get("items"),
            ])

    for source in direct_candidates:
        if not source:
            continue

        if isinstance(source, str):
            value = source.strip()
            if value:
                candidates.append(value)
            continue

        if isinstance(source, dict):
            for key in ("name", "studio", "title", "value", "label"):
                value = str(source.get(key) or "").strip()
                if value:
                    candidates.append(value)
                    break
            continue

        if isinstance(source, list):
            for item in source:
                if isinstance(item, dict):
                    value = (
                        item.get("name")
                        or item.get("studio")
                        or item.get("title")
                        or item.get("value")
                        or item.get("label")
                        or ""
                    )
                else:
                    value = str(item or "")
                value = value.strip()
                if value:
                    candidates.append(value)

    clean = []
    seen = set()
    for name in candidates:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        clean.append(name)

    clean = [name for name in clean if name.lower() not in {"animeplay", "n/a", "na", "-"}]
    return ", ".join(clean[:2]) if clean else ""


def _pick_portrait_image(anime: dict) -> str:
    direct_candidates = [
        anime.get("cover_url"),
        anime.get("media_image_url"),
        anime.get("poster_url"),
        anime.get("image"),
    ]
    for value in direct_candidates:
        value = str(value or "").strip()
        if value:
            return value

    cover_image = anime.get("coverImage") or anime.get("cover_image") or {}
    if isinstance(cover_image, dict):
        for key in ("extraLarge", "large", "medium"):
            value = str(cover_image.get(key) or "").strip()
            if value and value.startswith("http"):
                return value

    images = anime.get("images") or {}
    if isinstance(images, dict):
        for key in ("poster", "cover", "vertical", "thumbnail"):
            value = images.get(key)
            if isinstance(value, dict):
                for subkey in ("extraLarge", "large", "medium", "url"):
                    subval = str(value.get(subkey) or "").strip()
                    if subval and subval.startswith("http"):
                        return subval
            else:
                value = str(value or "").strip()
                if value and value.startswith("http"):
                    return value

    fallback_candidates = [
        anime.get("banner_url"),
        anime.get("bannerImage"),
    ]
    for value in fallback_candidates:
        value = str(value or "").strip()
        if value:
            return value

    return ""


def _build_anilist_url(anime: dict, fallback_title: str, fallback_item: dict | None = None) -> str:
    fallback_item = fallback_item or {}

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


def _normalize_quality(value: str) -> str:
    value = (value or "").upper().strip()

    if value in {"FULLHD", "FHD", "1080P", "HD", "720P"}:
        return "HD"

    if value in {"SD", "480P", "360P"}:
        return "SD"

    return "HD"


def _available_quality_set(player: dict) -> set:
    qualities = set()

    for q in (player.get("available_qualities") or []):
        normalized = _normalize_quality(str(q))
        if normalized:
            qualities.add(normalized)

    videos = player.get("videos") or {}
    if isinstance(videos, dict):
        for q in videos.keys():
            normalized = _normalize_quality(str(q))
            if normalized:
                qualities.add(normalized)

    current = _normalize_quality(player.get("quality", ""))
    if current:
        qualities.add(current)

    return qualities


def _build_miniapp_episode_url(anime_id: str, episode: str, quality: str) -> str:
    quality = _normalize_quality(quality)
    base = MINIAPP_URL.rstrip("/")
    return f"{base}/?anime={anime_id}&ep={episode}&q={quality}"


def _build_miniapp_anime_url(anime_id: str) -> str:
    base = MINIAPP_URL.rstrip("/")
    return f"{base}/?anime={anime_id}"


def _player_keyboard(
    anime_id: str,
    episode: str,
    detected_video: str,
    prev_episode,
    next_episode,
    selected_quality: str,
    user_id: int | str,
    available_qualities: set | None = None,
):
    selected_quality = _normalize_quality(selected_quality)
    available_qualities = available_qualities or set()

    hd_label = "HD"
    sd_label = "SD"

    if available_qualities:
        if "HD" not in available_qualities:
            hd_label = "HD 🚫"
        if "SD" not in available_qualities:
            sd_label = "SD 🚫"

    if selected_quality == "HD":
        hd_label = f"{hd_label} 🔘"
    else:
        sd_label = f"{sd_label} 🔘"

    watched = is_episode_watched(user_id, anime_id, episode)

    watch_toggle_button = InlineKeyboardButton(
        "❌ Desmarcar como visto" if watched else "✅ Marcar como visto",
        callback_data=f"unvw|{anime_id}|{episode}" if watched else f"vw|{anime_id}|{episode}",
    )

    miniapp_episode_url = _build_miniapp_episode_url(
        anime_id=anime_id,
        episode=str(episode),
        quality=selected_quality,
    )

    rows = [
        [
            InlineKeyboardButton(
                "▶️ Assistir",
                web_app=WebAppInfo(url=miniapp_episode_url),
            )
        ],
        [InlineKeyboardButton("\U0001f4e5 Baixar offline", callback_data=f"dl|{anime_id}|{episode}")],
        [watch_toggle_button],
        [
            InlineKeyboardButton(hd_label, callback_data=f"ql|{anime_id}|{episode}|HD"),
            InlineKeyboardButton(sd_label, callback_data=f"ql|{anime_id}|{episode}|SD"),
        ],
    ]

    nav = []
    if prev_episode:
        nav.append(
            InlineKeyboardButton(
                "⏮ Anterior",
                callback_data=f"ep|{anime_id}|{prev_episode}",
            )
        )
    if next_episode:
        nav.append(
            InlineKeyboardButton(
                "Próximo ⏭",
                callback_data=f"ep|{anime_id}|{next_episode}",
            )
        )
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton("📋 Lista de episódios", callback_data=f"eps|{anime_id}|0")
    ])

    return InlineKeyboardMarkup(rows)


def _anime_text(anime: dict, fallback_title: str = "Sem título") -> str:
    title = html.escape(_pick_display_title(anime, fallback_title))
    image_url = _pick_portrait_image(anime)

    genres = anime.get("genres") or []
    genres_text = _format_hashtag_genres(genres)

    year = anime.get("season_year") or anime.get("year") or ""
    status = _translate_status(str(anime.get("status") or ""))
    episodes = anime.get("episodes") or ""
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

    for bad in (
        "<b>GÃªnero:</b> <i></i>\n",
        "<b>Ano:</b> <i></i>\n",
        "<b>Status:</b> <i></i>\n",
        "<b>Total EpisÃ³dios:</b> <i></i>\n",
        "<b>Studio:</b> <i></i>\n",
        "<b>ClassificaÃ§Ã£o:</b> <i></i>\n",
        "<b>GÃªnero:</b> <i>N/A</i>\n",
        "<b>Status:</b> <i>N/A</i>\n",
        "<b>Total EpisÃ³dios:</b> <i>N/A</i>\n",
        "<b>Studio:</b> <i>N/A</i>\n",
        "<b>ClassificaÃ§Ã£o:</b> <i>N/A</i>\n",
    ):
        text = text.replace(bad, "")

    return text


def _single_anime_keyboard(
    anime_id: str,
    anime: dict,
    fallback_title: str,
    fallback_item: dict | None = None,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                "📺 Ver episódios",
                 web_app=WebAppInfo(url=_build_miniapp_anime_url(anime_id))
            )
        ]
    ]

    rows.append([
        InlineKeyboardButton("\U0001f4e5 Baixar offline", callback_data=f"off|{anime_id}")
    ])

    second_row = []
    anilist_url = _build_anilist_url(anime, fallback_title, fallback_item or {})
    trailer_url = _build_trailer_url(anime)

    if anilist_url:
        second_row.append(InlineKeyboardButton("🧾 Sinopse", url=anilist_url))

    if trailer_url:
        second_row.append(InlineKeyboardButton("🎬 Trailer", url=trailer_url))

    if second_row:
        rows.append(second_row)

    return InlineKeyboardMarkup(rows)


def _variant_keyboard(
    group_item: dict,
    anime: dict,
    fallback_title: str = "Sem título",
) -> InlineKeyboardMarkup:
    rows = []

    seasons = group_item.get("seasons") or []
    if len(seasons) > 1:
        for season in seasons:
            season_number = int(season.get("season") or 1)
            season_variants = season.get("variants") or []
            sub_variant = next((v for v in season_variants if not v.get("is_dubbed")), None)
            dub_variant = next((v for v in season_variants if v.get("is_dubbed")), None)

            if sub_variant and dub_variant:
                rows.append([
                    InlineKeyboardButton(
                        f"T{season_number:02d} 🇯🇵",
                        web_app=WebAppInfo(url=_build_miniapp_anime_url(sub_variant["id"]))
                    ),
                    InlineKeyboardButton(
                        f"T{season_number:02d} 🇧🇷",
                        web_app=WebAppInfo(url=_build_miniapp_anime_url(dub_variant["id"]))
                    ),
                ])
            else:
                variant = sub_variant or dub_variant or (season_variants[0] if season_variants else None)
                if variant and variant.get("id"):
                    label = f"📚 Temporada {season_number:02d}"
                    if variant.get("is_dubbed"):
                        label += " 🇧🇷"
                    rows.append([
                        InlineKeyboardButton(
                            label,
                            web_app=WebAppInfo(url=_build_miniapp_anime_url(variant["id"]))
                        )
                    ])

        default_id = group_item.get("default_anime_id") or group_item.get("id")
        if default_id:
            rows.append([
                InlineKeyboardButton("\U0001f4e5 Baixar offline", callback_data=f"off|{default_id}")
            ])

        second_row = []
        anilist_url = _build_anilist_url(anime, fallback_title, group_item)
        trailer_url = _build_trailer_url(anime)

        if anilist_url:
            second_row.append(InlineKeyboardButton("🧾 Sinopse", url=anilist_url))

        if trailer_url:
            second_row.append(InlineKeyboardButton("🎬 Trailer", url=trailer_url))

        if second_row:
            rows.append(second_row)

        return InlineKeyboardMarkup(rows)

    variants = group_item.get("variants") or []
    sub_variant = next((v for v in variants if not v.get("is_dubbed")), None)
    dub_variant = next((v for v in variants if v.get("is_dubbed")), None)

    if sub_variant:
        rows.append([
            InlineKeyboardButton(
                "🇯🇵 Legendado",
                web_app=WebAppInfo(url=_build_miniapp_anime_url(sub_variant["id"]))
            )
        ])

    if dub_variant:
        rows.append([
            InlineKeyboardButton(
                "🇧🇷 Dublado",
                web_app=WebAppInfo(url=_build_miniapp_anime_url(dub_variant["id"]))
            )
        ])

    default_id = group_item.get("default_anime_id") or group_item.get("id")
    if default_id:
        rows.append([
            InlineKeyboardButton("\U0001f4e5 Baixar offline", callback_data=f"off|{default_id}")
        ])

    second_row = []
    anilist_url = _build_anilist_url(anime, fallback_title, group_item)
    trailer_url = _build_trailer_url(anime)

    if anilist_url:
        second_row.append(InlineKeyboardButton("🧾 Sinopse", url=anilist_url))

    if trailer_url:
        second_row.append(InlineKeyboardButton("🎬 Trailer", url=trailer_url))

    if second_row:
        rows.append(second_row)

    if not rows:
        default_id = group_item.get("default_anime_id") or group_item.get("id")
        rows.append([
            InlineKeyboardButton(
                "📺 Ver episódios",
                 web_app=WebAppInfo(url=_build_miniapp_anime_url(default_id))
            )
        ])

    return InlineKeyboardMarkup(rows)


def _safe_user_lock(user_id: int) -> asyncio.Lock:
    lock = _START_USER_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _START_USER_LOCKS[user_id] = lock
    return lock


def _now() -> float:
    return time.monotonic()


def _deep_link_key(user_id: int, payload: str) -> str:
    return f"{user_id}:{payload}"


def _is_inflight(user_id: int, payload: str) -> bool:
    key = _deep_link_key(user_id, payload)
    item = _START_INFLIGHT.get(key)
    if not item:
        return False

    if _now() - item > START_DEEP_LINK_TTL:
        _START_INFLIGHT.pop(key, None)
        return False

    return True


def _set_inflight(user_id: int, payload: str):
    _START_INFLIGHT[_deep_link_key(user_id, payload)] = _now()


def _clear_inflight(user_id: int, payload: str):
    _START_INFLIGHT.pop(_deep_link_key(user_id, payload), None)


def _start_last_key(user_id: int) -> str:
    return f"start_last:{user_id}"


def _start_last_payload_key(user_id: int) -> str:
    return f"start_last_payload:{user_id}"


def _is_start_cooldown(context: ContextTypes.DEFAULT_TYPE, user_id: int, payload: str) -> bool:
    now = _now()

    last_ts = context.user_data.get(_start_last_key(user_id), 0.0)
    last_payload = context.user_data.get(_start_last_payload_key(user_id), "")

    if payload and payload == last_payload and (now - last_ts) < START_COOLDOWN:
        return True

    context.user_data[_start_last_key(user_id)] = now
    context.user_data[_start_last_payload_key(user_id)] = payload
    return False


async def _safe_delete_message(msg):
    if not msg:
        return
    try:
        await msg.delete()
    except TelegramError:
        pass
    except Exception:
        pass


async def _resolve_group_from_anime_id(anime_id: str):
    anime = await asyncio.wait_for(get_anime_details(anime_id), timeout=20)
    title = anime.get("title") or anime_id.replace("-", " ").title()

    results = await asyncio.wait_for(search_anime(title), timeout=20)

    for item in results:
        if (item.get("default_anime_id") or item.get("id")) == anime_id:
            return anime, _with_season_groups(item, results)

        for variant in (item.get("variants") or []):
            if variant.get("id") == anime_id:
                return anime, _with_season_groups(item, results)

    fallback_item = {
        "id": anime_id,
        "default_anime_id": anime_id,
        "title": title,
        "variants": [{
            "id": anime_id,
            "title": title,
            "is_dubbed": False,
        }],
        "has_dubbed": False,
        "has_subbed": True,
    }

    return anime, fallback_item


def _season_number_from_text(value: str) -> int:
    text = str(value or "").lower()
    match = re.search(r"\b(?:season|temporada)\s*(\d+)\b", text)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(\d+)(?:st|nd|rd|th)\s*(?:season|temporada)\b", text)
    if match:
        return int(match.group(1))
    match = re.search(r"-(\d+)(?:st|nd|rd|th)?-season\b", text)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(\d+)\s*[:\-–—]?\s*(?:arise|part|cour|final|science|shadow)\b", text)
    if match:
        return int(match.group(1))
    match = re.search(r"-(\d+)$", text)
    if match:
        return int(match.group(1))
    return 1


def _season_group_key(value: str) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\b(?:dublado|legendado|todos os episodios|todos os episódios|online|hd)\b", " ", text)
    text = re.sub(r"\b(?:season|temporada)\s*\d+\b", " ", text)
    text = re.sub(r"\b\d+(?:st|nd|rd|th)?\s*(?:season|temporada)\b", " ", text)
    text = re.sub(r"[-\s]+\d+(?:st|nd|rd|th)?[-\s]*(?:season|temporada).*$", " ", text)
    text = re.sub(r"\s+\d+\s+(?:arise|part|cour|final|shadow).*$", " ", text)
    text = re.sub(r"[-\s]+\d+$", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _item_season_key(item: dict) -> str:
    title = item.get("display_title") or item.get("title") or item.get("raw_title") or item.get("id") or ""
    return _season_group_key(title) or _season_group_key(item.get("id") or "")


def _season_entry_from_item(item: dict) -> dict:
    variants = item.get("variants") or []
    if not variants:
        default_id = item.get("default_anime_id") or item.get("id")
        variants = [{
            "id": default_id,
            "title": item.get("title") or item.get("display_title") or default_id,
            "is_dubbed": bool(item.get("is_dubbed")),
        }]

    title = item.get("display_title") or item.get("title") or variants[0].get("title") or ""
    season_number = max(
        _season_number_from_text(title),
        _season_number_from_text(item.get("id") or ""),
        _season_number_from_text(item.get("raw_title") or ""),
    )

    return {
        "season": season_number,
        "title": title,
        "default_anime_id": item.get("default_anime_id") or item.get("id") or variants[0].get("id"),
        "variants": variants,
    }


def _with_season_groups(item: dict, results: list[dict]) -> dict:
    if not item:
        return item

    key = _item_season_key(item)
    if not key:
        return item

    seasons_by_number = {}
    for candidate in results or []:
        if _item_season_key(candidate) != key:
            continue
        entry = _season_entry_from_item(candidate)
        season_number = int(entry.get("season") or 1)
        current = seasons_by_number.get(season_number)
        if not current:
            seasons_by_number[season_number] = entry
            continue

        by_id = {variant.get("id"): variant for variant in current.get("variants") or [] if variant.get("id")}
        for variant in entry.get("variants") or []:
            variant_id = variant.get("id")
            if variant_id and variant_id not in by_id:
                current.setdefault("variants", []).append(variant)

    if len(seasons_by_number) <= 1:
        return item

    merged = dict(item)
    merged["seasons"] = [
        seasons_by_number[number]
        for number in sorted(seasons_by_number)
    ]
    return merged


def _load_anilist_links() -> dict:
    try:
        if not ANILIST_LINKS_JSON_PATH.exists():
            return {}
        data = json.loads(ANILIST_LINKS_JSON_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def _fetch_anilist_title(anilist_id: str) -> str:
    query = """
    query ($id: Int) {
      Media(id: $id, type: ANIME) {
        title {
          romaji
          english
          native
        }
      }
    }
    """
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.post(
            ANILIST_API_URL,
            json={"query": query, "variables": {"id": int(anilist_id)}},
        )
        response.raise_for_status()
        data = response.json()

    title = ((data.get("data") or {}).get("Media") or {}).get("title") or {}
    return (
        title.get("romaji")
        or title.get("english")
        or title.get("native")
        or ""
    ).strip()


def _item_matches_anilist(item: dict, anilist_id: str) -> bool:
    candidates = [
        item.get("anilist_id"),
        item.get("anilistId"),
    ]
    for variant in item.get("variants") or []:
        candidates.extend([variant.get("anilist_id"), variant.get("anilistId")])

    return any(str(value or "") == str(anilist_id) for value in candidates)


async def _resolve_group_from_anilist_id(anilist_id: str):
    anilist_id = str(anilist_id or "").strip()
    links = _load_anilist_links()
    mapped = links.get(anilist_id) or {}

    mapped_id = mapped.get("default_anime_id") or mapped.get("id")
    if mapped_id:
        try:
            anime, group_item = await _resolve_group_from_anime_id(mapped_id)
            if group_item:
                return anime, group_item
        except Exception:
            pass

    title = await asyncio.wait_for(_fetch_anilist_title(anilist_id), timeout=20)
    if not title:
        raise RuntimeError(f"AniList sem titulo para id={anilist_id}")

    results = await asyncio.wait_for(search_anime(title), timeout=20)
    if not results:
        raise RuntimeError(f"Nenhum resultado local para AniList id={anilist_id}")

    for item in results:
        if _item_matches_anilist(item, anilist_id):
            anime_id = item.get("default_anime_id") or item.get("id")
            anime = await asyncio.wait_for(get_anime_details(anime_id), timeout=20)
            return anime, _with_season_groups(item, results)

    item = results[0]
    anime_id = item.get("default_anime_id") or item.get("id")
    anime = await asyncio.wait_for(get_anime_details(anime_id), timeout=20)
    return anime, _with_season_groups(item, results)


def _remember_group_item(context: ContextTypes.DEFAULT_TYPE, item: dict) -> None:
    default_id = item.get("default_anime_id") or item.get("id")
    if default_id:
        context.user_data[f"anime_group:{default_id}"] = item

    for variant in item.get("variants") or []:
        variant_id = variant.get("id")
        if variant_id:
            context.user_data[f"anime_group:{variant_id}"] = item

    for season in item.get("seasons") or []:
        season_default_id = season.get("default_anime_id")
        if season_default_id:
            context.user_data[f"anime_group:{season_default_id}"] = item
        for variant in season.get("variants") or []:
            variant_id = variant.get("id")
            if variant_id:
                context.user_data[f"anime_group:{variant_id}"] = item


async def start(update, context):
    init_referral_db()

    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    upsert_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
    )
    register_interaction(user.id)
    register_user(user.id)

    arg = (context.args[0] or "").strip() if context.args else ""
    referral_feedback = None

    if arg.startswith("ref_"):
        raw_ref = arg[len("ref_"):].strip()
        if raw_ref.isdigit():
            referrer_id = int(raw_ref)

            try:
                register_referral_click(referrer_id, user.id)
            except Exception as e:
                print("ERRO REGISTER REFERRAL CLICK:", repr(e))

            try:
                ok, reason = create_referral(referrer_id, user.id)
                referral_feedback = (ok, reason)
            except Exception as e:
                print("ERRO CREATE REFERRAL:", repr(e))

    is_member = await ensure_channel_membership(update, context)
    if not is_member:
        return

    try:
        try_qualify_referral(user.id, is_channel_member=True)
    except Exception as e:
        print("ERRO QUALIFY REFERRAL:", repr(e))

    if referral_feedback:
        ok, reason = referral_feedback

        if ok:
            await message.reply_text("🎁 Convite registrado com sucesso!\n\n")
        else:
            if reason == "self_referral":
                await message.reply_text("⚠️ Você não pode usar seu próprio link.")
            elif reason == "already_referred_by_other":
                await message.reply_text(
                    "⚠️ Sua conta já foi vinculada a outra indicação antes."
                )

    if arg and _is_start_cooldown(context, user.id, arg):
        await message.reply_text("⏳ Aguarde um instante antes de repetir essa ação.")
        return

    if arg and _is_inflight(user.id, arg):
        await message.reply_text("⏳ Essa solicitação já está sendo processada.")
        return

    user_lock = _safe_user_lock(user.id)

    async with user_lock:
        if arg and _is_inflight(user.id, arg):
            await message.reply_text("⏳ Essa solicitação já está sendo processada.")
            return

        if arg:
            _set_inflight(user.id, arg)

        try:
            if arg.startswith("ep_") and "__" in arg:
                raw = arg[len("ep_"):]
                anime_id, episode = raw.rsplit("__", 1)

                loading_msg = await message.reply_text(
                    "⏳ <b>Abrindo o episódio para você...</b>",
                    parse_mode="HTML",
                )

                try:
                    anime = await asyncio.wait_for(get_anime_details(anime_id), timeout=20)
                    player = await asyncio.wait_for(
                        get_episode_player(anime_id, episode, "HD"),
                        timeout=25,
                    )

                    total_episodes = player.get("total_episodes", 0)
                    quality = _normalize_quality(player.get("quality", "HD"))
                    prev_episode = player.get("prev_episode")
                    next_episode = player.get("next_episode")
                    available_qualities = _available_quality_set(player)

                    text = (
                        f"🎬 <b>{html.escape(_clean_anime_title(anime.get('title', 'Sem título')))}</b>\n\n"
                        f"▶️ <b>Episódio {html.escape(str(episode))}</b>\n"
                        f"🎚 {html.escape(quality)} • 📚 {total_episodes} eps\n\n"
                        f"<i>Escolha uma opção abaixo 👇</i>"
                    )

                    keyboard = _player_keyboard(
                        anime_id=anime_id,
                        episode=str(episode),
                        detected_video=(player.get("video") or "").strip(),
                        prev_episode=prev_episode,
                        next_episode=next_episode,
                        selected_quality=quality,
                        user_id=user.id,
                        available_qualities=available_qualities,
                    )

                    cover = _pick_portrait_image(anime) or None

                    await _safe_delete_message(loading_msg)

                    if cover:
                        await message.reply_photo(
                            photo=cover,
                            caption=text,
                            parse_mode="HTML",
                            reply_markup=keyboard,
                        )
                    else:
                        await message.reply_text(
                            text,
                            parse_mode="HTML",
                            reply_markup=keyboard,
                        )
                    return

                except asyncio.TimeoutError:
                    await _safe_delete_message(loading_msg)
                    await message.reply_text(
                        "⏳ Esse episódio demorou demais para abrir. Tente novamente em instantes."
                    )
                    return
                except Exception as e:
                    await _safe_delete_message(loading_msg)
                    print("ERRO START EPISODIO:", repr(e))
                    await message.reply_text(
                        "❌ Não foi possível abrir esse episódio agora."
                    )
                    return

            if arg.startswith("anime_al_") or arg.startswith("anime_"):
                is_anilist_deep_link = arg.startswith("anime_al_")
                anime_id = arg[len("anime_al_"):].strip() if is_anilist_deep_link else arg[len("anime_"):].strip()

                loading_msg = await message.reply_text(
                    "⏳ <b>Abrindo o anime para você...</b>",
                    parse_mode="HTML",
                )

                try:
                    if is_anilist_deep_link:
                        anime, group_item = await _resolve_group_from_anilist_id(anime_id)
                    else:
                        anime, group_item = await _resolve_group_from_anime_id(anime_id)
                    _remember_group_item(context, group_item)

                    fallback_title = group_item.get("title") or anime.get("title") or "Sem título"
                    cover = _pick_portrait_image(anime) or None
                    text = _anime_text(anime, fallback_title)

                    variants = group_item.get("variants") or []
                    sub_count = 1 if any(not v.get("is_dubbed") for v in variants) else 0
                    dub_count = 1 if any(v.get("is_dubbed") for v in variants) else 0
                    available_versions = sub_count + dub_count
                    has_season_groups = len(group_item.get("seasons") or []) > 1

                    if available_versions <= 1 and not has_season_groups:
                        default_id = group_item.get("default_anime_id") or anime_id
                        keyboard = _single_anime_keyboard(
                            anime_id=default_id,
                            anime=anime,
                            fallback_title=fallback_title,
                            fallback_item=group_item,
                        )
                    else:
                        keyboard = _variant_keyboard(
                            group_item=group_item,
                            anime=anime,
                            fallback_title=fallback_title,
                        )

                    await _safe_delete_message(loading_msg)

                    if cover:
                        await message.reply_photo(
                            photo=cover,
                            caption=text,
                            parse_mode="HTML",
                            reply_markup=keyboard,
                        )
                    else:
                        await message.reply_text(
                            text,
                            parse_mode="HTML",
                            reply_markup=keyboard,
                        )
                    return

                except asyncio.TimeoutError:
                    await _safe_delete_message(loading_msg)
                    await message.reply_text(
                        "⏳ Esse anime demorou demais para abrir. Tente novamente em instantes."
                    )
                    return
                except Exception as e:
                    await _safe_delete_message(loading_msg)
                    print("ERRO START DEEP LINK:", repr(e))
                    await message.reply_text(
                        "❌ Não foi possível abrir esse anime agora."
                    )
                    return

            first_name = html.escape(user.first_name or "otaku")
            text = (
                f"🍥 <b>Bem-vindo ao Animes Baltigo Bot, {first_name}!</b>\n\n"
                "Aqui você pode encontrar seus animes favoritos, abrir episódios rapidamente e assistir sem sair do bot.\n\n"
                "<blockquote>• 🔎 Busque qualquer anime\n"
                "• 📺 Navegue pela lista de episódios\n"
                "• 📥 Baixe para assistir offline quando disponível</blockquote>\n\n"
                "<i>Use <code>/buscar</code> para começar sua jornada.</i>"
            )

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔎 Buscar anime",
                        switch_inline_query_current_chat=""
                    )
                ],
                [
                    InlineKeyboardButton(
                        "➕ Adicionar ao grupo",
                        url=f"https://t.me/{BOT_USERNAME}?startgroup=true"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🏴‍☠️ QG Baltigo",
                        url="https://t.me/QG_BALTIGO"
                    )
                ]
            ])

            await message.reply_photo(
                photo=BANNER_URL,
                caption=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )

        finally:
            if arg:
                _clear_inflight(user.id, arg)

            await _safe_delete_message(message)
