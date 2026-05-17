import asyncio
import html
import re
import time
import traceback
import inspect
from urllib.parse import quote_plus

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update, WebAppInfo
try:
    from telegram import CopyTextButton
except Exception:
    CopyTextButton = None
from telegram.ext import ContextTypes

from config import ADMIN_IDS, BOT_USERNAME, MINIAPP_SHORT_NAME, OFFLINE_REFERRAL_REQUIRED_CLICKS
from core.video_download_queue import VideoDownloadJob, enqueue_video_download
from handlers.offline_paywall import answer_subscription_check
from services.animefire_client import (
    get_anime_details,
    get_episodes,
    get_episode_player,
    get_random_anime_by_genre,
)
from services.metrics import (
    log_event,
    mark_user_seen,
    is_episode_watched,
    mark_episode_watched,
    unmark_episode_watched,
)
from services.referral_db import referral_distinct_clicks

EPISODES_PER_PAGE = 15
SEARCH_RESULTS_PER_PAGE = 8

CALLBACK_COOLDOWN = 1.0
QUALITY_COOLDOWN = 0.8

ANIME_CACHE_TTL = 60 * 30
EPISODES_CACHE_TTL = 60 * 10
PLAYER_CACHE_TTL = 60 * 3
RECOMMEND_CACHE_TTL = 60 * 5

GLOBAL_FETCH_SEMAPHORE = asyncio.Semaphore(40)

SEARCH_BANNER_URL = "https://photo.chelpbot.me/AgACAgEAAxkBaL-UMWnDPUdoNCaz4ZUFvzeOHSVXh0oRAALTC2sbdnEYRrjsVpeCeT08AQADAgADeQADOgQ/photo.jpg"
MINIAPP_URL = "https://rough-double-remarkable-north.trycloudflare.com/app"

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

_GLOBAL_ANIME_CACHE = {}
_GLOBAL_EPISODES_CACHE = {}
_GLOBAL_PLAYER_CACHE = {}
_GLOBAL_RECOMMEND_CACHE = {}

_INFLIGHT_ANIME = {}
_INFLIGHT_EPISODES = {}
_INFLIGHT_PLAYER = {}
_INFLIGHT_RECOMMEND = {}

_USER_CALLBACK_LOCKS = {}
_MESSAGE_EDIT_LOCKS = {}
_MESSAGE_INFLIGHT_ACTIONS = {}


def _now() -> float:
    return time.monotonic()


def _cache_get(cache: dict, key: str, ttl: int):
    item = cache.get(key)
    if not item:
        return None

    if _now() - item["time"] > ttl:
        cache.pop(key, None)
        return None

    return item["data"]


def _cache_set(cache: dict, key: str, data):
    cache[key] = {
        "time": _now(),
        "data": data,
    }


async def _dedup_fetch(cache: dict, inflight: dict, key: str, ttl: int, coro_factory):
    cached = _cache_get(cache, key, ttl)
    if cached is not None:
        return cached

    task = inflight.get(key)
    if task:
        return await task

    async def _runner():
        async with GLOBAL_FETCH_SEMAPHORE:
            return await coro_factory()

    task = asyncio.create_task(_runner())
    inflight[key] = task

    try:
        data = await task
        _cache_set(cache, key, data)
        return data
    finally:
        inflight.pop(key, None)


async def _safe_answer_query(query, text: str | None = None, show_alert: bool = False):
    try:
        if text is None:
            await query.answer()
        else:
            await query.answer(text, show_alert=show_alert)
    except Exception:
        pass


def _offline_referral_unlocked(user_id: int) -> tuple[bool, int, int]:
    required = max(1, int(OFFLINE_REFERRAL_REQUIRED_CLICKS or 3))
    current = referral_distinct_clicks(user_id)
    return current >= required, current, required


def _offline_referral_url(user_id: int) -> str:
    username = (BOT_USERNAME or "AnimesBaltigo_Bot").strip().lstrip("@")
    return f"https://t.me/{username}?start=ref_{user_id}"


def _copy_text_button(label: str, text: str) -> InlineKeyboardButton:
    payload = str(text or "")[:256]
    if CopyTextButton is not None:
        return InlineKeyboardButton(label, copy_text=CopyTextButton(text=payload))
    return InlineKeyboardButton(label, api_kwargs={"copy_text": {"text": payload}})


async def _send_offline_referral_gate(query, user) -> None:
    ok, current, required = _offline_referral_unlocked(user.id)
    if ok:
        return
    missing = max(0, required - current)
    link = _offline_referral_url(user.id)
    share_url = (
        "https://t.me/share/url?"
        f"url={quote_plus(link)}"
        "&text=" + quote_plus("Vem usar esse bot de animes comigo:")
    )
    text = (
        "<b>Baixar offline bloqueado</b>\n\n"
        f"Indique o bot para <b>{required}</b> pessoas diferentes para liberar essa funcao.\n\n"
        f"<b>Seu progresso:</b> <code>{current}/{required}</code>\n"
        f"<b>Faltam:</b> <code>{missing}</code>\n\n"
        "<b>Seu link:</b>\n"
        f"<code>{html.escape(link)}</code>"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Compartilhar meu link", url=share_url)],
        [_copy_text_button("Copiar meu link", link)],
        [InlineKeyboardButton("Abrir meu link", url=link)],
    ])
    await query.answer(f"Faltam {missing} indicacao(oes) para liberar.", show_alert=True)
    if query.message:
        await query.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


async def _mark_user_seen_safe(user):
    try:
        result = mark_user_seen(user.id, user.username or user.first_name or "")
        if inspect.isawaitable(result):
            await result
    except Exception as e:
        print("ERRO AO MARCAR USUÁRIO ATIVO:", repr(e))


def _safe_log_event(**kwargs):
    try:
        log_event(**kwargs)
    except Exception as e:
        print("ERRO AO SALVAR MÉTRICA:", repr(e))


def _strip_html(text: str):
    return re.sub(r"<[^>]+>", "", text or "")


def _truncate_text(text: str, limit: int):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _is_hentai(anime: dict) -> bool:
    haystack = " ".join([
        str(anime.get("id") or ""),
        str(anime.get("title") or ""),
        str(anime.get("display_title") or ""),
        " ".join(str(g) for g in anime.get("genres") or []),
    ])
    return bool(re.search(r"\bhentai\b|\+18|18\+", haystack, re.I))


def _anime_main_image(anime: dict) -> str:
    if _is_hentai(anime):
        return ""
    return (
        anime.get("cover_url")
        or anime.get("media_image_url")
        or anime.get("banner_url")
        or ""
    ).strip()


def _anime_secondary_image(anime: dict) -> str:
    if _is_hentai(anime):
        return ""
    return (
        anime.get("media_image_url")
        or anime.get("cover_url")
        or anime.get("banner_url")
        or ""
    ).strip()


def _anime_text(anime: dict):
    title = html.escape((anime.get("title") or "Sem título").strip()).upper()

    description = _strip_html(anime.get("description") or "Sem descrição disponível.")
    description = _truncate_text(description, 280)
    description = html.escape(description)

    score = anime.get("score")
    status = anime.get("status")
    genres = anime.get("genres") or []
    episodes = anime.get("episodes")
    season_year = anime.get("season_year")

    info_lines = []

    if score:
        info_lines.append(f"⭐ <b>Pontuação:</b> <code>{score}</code>")

    if status:
        info_lines.append(f"📡 <b>Situação:</b> <code>{html.escape(str(status))}</code>")

    if season_year:
        info_lines.append(f"📅 <b>Lançamento:</b> <code>{season_year}</code>")

    if episodes:
        info_lines.append(f"📚 <b>Episódios:</b> <code>{episodes}</code>")

    genres_block = ""
    if genres:
        safe_genres = " • ".join(html.escape(str(g)) for g in genres[:5])
        genres_block = (
            f"\n🎭 <b>Gêneros:</b>\n"
            f"<code>{safe_genres}</code>\n"
        )

    info_block = "\n".join(info_lines)

    return (
        f"🎬 <b>{title}</b>\n\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"{info_block}"
        f"{genres_block}\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"📖 <b>Info:</b>\n"
        f"{description}"
    )


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


def _item_is_dubbed(item: dict | None) -> bool | None:
    if not item or "is_dubbed" not in item:
        return None

    value = item.get("is_dubbed")
    if value is None:
        return None

    return bool(value)


def _find_variant_by_id(item: dict | None, anime_id: str) -> dict | None:
    if not item or not anime_id:
        return None

    default_id = item.get("default_anime_id") or item.get("id")
    if default_id == anime_id:
        return item

    for variant in item.get("variants") or []:
        if variant.get("id") == anime_id:
            return variant

    return None


def _format_title_with_version(
    title: str,
    is_dubbed: bool | None = None,
    *,
    clean: bool = False,
) -> str:
    base_title = _clean_button_title(title) if clean else (title or "").strip()
    base_title = base_title or "Sem título"

    return base_title


def _resolve_is_dubbed(
    context: ContextTypes.DEFAULT_TYPE | None = None,
    anime_id: str | None = None,
    anime: dict | None = None,
    item: dict | None = None,
) -> bool | None:
    candidate = _item_is_dubbed(item)
    if candidate is not None:
        return candidate

    if context and anime_id:
        group_item = _get_group_item(context, anime_id)
        candidate = _item_is_dubbed(_find_variant_by_id(group_item, anime_id))
        if candidate is not None:
            return candidate

    if anime and anime.get("is_dubbed") is not None:
        return bool(anime.get("is_dubbed"))

    return None


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


def _anime_text(
    anime: dict,
    fallback_title: str = "Sem título",
    is_dubbed: bool | None = None,
):
    display_title = _format_title_with_version(
        _pick_display_title(anime, fallback_title),
        is_dubbed,
    )
    safe_title = html.escape(display_title)
    image_url = _anime_main_image(anime)

    genres_text = _format_hashtag_genres(anime.get("genres") or [])
    year = anime.get("season_year") or anime.get("year") or ""
    status = _translate_status(str(anime.get("status") or ""))
    episodes = anime.get("episodes") or ""
    rating = _translate_rating(anime)
    studio = _extract_studio(anime)

    if image_url:
        title_line = f'<b><a href="{html.escape(image_url, quote=True)}">🎬</a> {safe_title}</b>'
    else:
        title_line = f"<b>🎬 {safe_title}</b>"

    text = (
        f"{title_line}\n\n"
        f"<b>Gênero:</b> <i>{html.escape(genres_text)}</i>\n"
        f"<b>Ano:</b> <i>{html.escape(str(year))}</i>\n"
        f"<b>Status:</b> <i>{html.escape(str(status))}</i>\n"
        f"<b>Total Episódios:</b> <i>{html.escape(str(episodes))}</i>\n"
        f"<b>Studio:</b> <i>{html.escape(str(studio))}</i>\n\n"
        f"🔥 <i>Assista direto pelo bot, do jeito mais simples e completo.</i>"
    )

    if image_url:
        text += f'<a href="{html.escape(image_url, quote=True)}">\u200b</a>'

    for bad in (
        "<b>GÃªnero:</b> <i></i>\n",
        "<b>Status:</b> <i></i>\n",
        "<b>Total EpisÃ³dios:</b> <i></i>\n",
        "<b>Studio:</b> <i></i>\n",
        "<b>ClassificaÃ§Ã£o:</b> <i></i>\n",
        "<b>Ano:</b> <i></i>\n",
        "<b>Status:</b> <i>N/A</i>\n",
        "<b>Total EpisÃ³dios:</b> <i>N/A</i>\n",
        "<b>Studio:</b> <i>N/A</i>\n",
        "<b>ClassificaÃ§Ã£o:</b> <i>N/A</i>\n",
        "<b>GÃªnero:</b> <i>N/A</i>\n",
    ):
        text = text.replace(bad, "")

    return text


def _episode_list_text(title: str, offset: int, total: int):
    safe_title = html.escape((title or "Sem título").strip())
    current_page = (offset // EPISODES_PER_PAGE) + 1
    total_pages = max(1, ((total - 1) // EPISODES_PER_PAGE) + 1)

    return (
        f"📺 <b>{safe_title}</b>\n\n"
        f"🎞 <b>Total de episódios:</b> {total}\n"
        f"📄 <b>Página:</b> {current_page}/{total_pages}\n\n"
        f"Escolha um episódio abaixo:"
    )


def _season_list_text(title: str, season: int, total: int, page: int, total_pages: int, offline: bool = False) -> str:
    safe_title = html.escape((title or "Sem título").strip())
    action = "Baixar offline" if offline else "Episódios"
    footer = "Toque em um episódio para o bot baixar e enviar no Telegram." if offline else "Escolha um episódio abaixo:"
    return (
        f"📚 <b>{action}</b>\n\n"
        f"<b>{safe_title}</b>\n"
        f"<b>Temporada:</b> {season}\n"
        f"<b>Total de episódios:</b> {total}\n"
        f"<b>Página:</b> {page}/{total_pages}\n\n"
        f"{footer}"
    )


def _season_picker_text(title: str, current_season: int, seasons: list[int]) -> str:
    safe_title = html.escape((title or "Sem título").strip())
    return (
        f"📚 <b>{safe_title}</b>\n\n"
        f"<b>Temporada atual:</b> {current_season}\n"
        f"<b>Temporadas:</b> {len(seasons)}\n\n"
        "Escolha a temporada que deseja abrir."
    )


def _display_server_name(server: str) -> str:
    server = (server or "").upper().strip()

    if server == "BLOGGER":
        return "BLOGGER"

    if server == "GOOGLEVIDEO":
        return "GOOGLEVIDEO"

    return server or "PADRÃO"


def _normalize_quality(value: str) -> str:
    value = (value or "").upper().strip()

    if value in {"FULLHD", "FHD", "1080P", "HD", "720P"}:
        return "HD"

    if value in {"SD", "480P", "360P"}:
        return "SD"

    return "HD"


def _player_text(title: str, episode: str, server: str, total_episodes: int, quality: str):
    safe_title = html.escape((title or "Sem título").strip())
    safe_ep = html.escape(str(episode))
    safe_server = html.escape(_display_server_name(server))
    safe_quality = html.escape(_normalize_quality(quality))

    return (
        f"▶️ <b>{safe_title}</b>\n\n"
        f"🎞 <b>Episódio:</b> {safe_ep}\n"
        f"🎚 <b>Qualidade:</b> {safe_quality}\n"
        f"📚 <b>Total:</b> {total_episodes}\n\n"
        f"Escolha uma opção abaixo para continuar."
    )


def _search_text(query: str, page: int, total: int):
    total_pages = max(1, ((total - 1) // SEARCH_RESULTS_PER_PAGE) + 1)
    safe_query = html.escape((query or "").strip())

    return (
        f"🔎 <b>Resultado da busca</b>\n\n"
        f"━━━━━━━━━━━━━━\n\n"
        f"🎬 <b>Pesquisa:</b> {safe_query}\n"
        f"📚 <b>Resultados:</b> {total}\n"
        f"📄 <b>Página:</b> {page}/{total_pages}\n\n"
        f"Toque em uma obra abaixo para abrir os detalhes."
    )


def _clean_button_title(title: str) -> str:
    title = (title or "").strip()

    title = re.sub(r"\b\d+\.\d+\b", "", title)
    title = re.sub(r"\bA(?:10|12|14|16|18|L)\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\bLIVRE\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\bN/?A\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\(\s*\)", "", title)
    title = re.sub(r"\s{2,}", " ", title).strip(" -–|•")

    return title or "Sem título"


def _search_keyboard(results: list, page: int, total: int, token: str):
    rows = []

    start = (page - 1) * SEARCH_RESULTS_PER_PAGE
    end = start + SEARCH_RESULTS_PER_PAGE
    page_items = results[start:end]

    for idx, item in enumerate(page_items, start=start + 1):
        title = _clean_button_title(item.get("title") or "Sem título")

        if len(title) > 42:
            title = title[:39].rstrip() + "..."

        rows.append([
            InlineKeyboardButton(
                f"🎬 {idx}. {title}",
                callback_data=f"sa|{token}|{idx - 1}",
            )
        ])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"sp|{token}|{page - 1}"))

    if end < total:
        nav.append(InlineKeyboardButton("Próxima ➡️", callback_data=f"sp|{token}|{page + 1}"))

    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(rows)


def _build_search_button_title(item: dict) -> str:
    return _format_title_with_version(
        item.get("title") or "Sem título",
        _item_is_dubbed(item),
        clean=True,
    )


def _search_keyboard(results: list, page: int, total: int, token: str):
    rows = []

    start = (page - 1) * SEARCH_RESULTS_PER_PAGE
    end = start + SEARCH_RESULTS_PER_PAGE
    page_items = results[start:end]

    for idx, item in enumerate(page_items, start=start + 1):
        title = _build_search_button_title(item)

        if len(title) > 42:
            title = title[:39].rstrip() + "..."

        rows.append([
            InlineKeyboardButton(
                f"🎬 {idx}. {title}",
                callback_data=f"sa|{token}|{idx - 1}",
            )
        ])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"sp|{token}|{page - 1}"))

    if end < total:
        nav.append(InlineKeyboardButton("Próxima ➡️", callback_data=f"sp|{token}|{page + 1}"))

    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(rows)


def _anime_group_map_key(anime_id: str) -> str:
    return f"anime_group:{anime_id}"


def _remember_group_item(context: ContextTypes.DEFAULT_TYPE, item: dict):
    if not item:
        return

    variants = item.get("variants") or []
    default_id = item.get("default_anime_id") or item.get("id")

    if default_id:
        context.user_data[_anime_group_map_key(default_id)] = item

    for variant in variants:
        variant_id = variant.get("id")
        if variant_id:
            context.user_data[_anime_group_map_key(variant_id)] = item


def _get_group_item(context: ContextTypes.DEFAULT_TYPE, anime_id: str) -> dict | None:
    return context.user_data.get(_anime_group_map_key(anime_id))


def _pick_variant(item: dict, dubbed: bool):
    variants = item.get("variants") or []
    for variant in variants:
        if bool(variant.get("is_dubbed")) == dubbed:
            return variant
    return None


def _variant_keyboard(item: dict, back_callback: str | None = None) -> InlineKeyboardMarkup:
    rows = []

    sub_variant = _pick_variant(item, dubbed=False)
    dub_variant = _pick_variant(item, dubbed=True)

    if sub_variant:
        rows.append([
            InlineKeyboardButton(
                "🇯🇵 Legendado",
                callback_data=f"var|{sub_variant['id']}",
            )
        ])

    if dub_variant:
        rows.append([
            InlineKeyboardButton(
                "🇧🇷 Dublado",
                callback_data=f"var|{dub_variant['id']}",
            )
        ])

    if not rows:
        default_id = item.get("default_anime_id") or item.get("id")
        rows.append([
            InlineKeyboardButton(
                "📺 Ver episódios",
                callback_data=f"eps|{default_id}|0",
            )
        ])

    if back_callback:
        rows.append([
            InlineKeyboardButton("🔙 Voltar", callback_data=back_callback)
        ])

    return InlineKeyboardMarkup(rows)


def _single_anime_keyboard(
    anime_id: str,
    anime: dict,
    fallback_title: str,
    fallback_item: dict | None = None,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    rows = [
        [
            _miniapp_anime_button("📺 Ver episódios", anime_id)
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

    if back_callback:
        rows.append([
            InlineKeyboardButton("🔙 Voltar", callback_data=back_callback)
        ])

    return InlineKeyboardMarkup(rows)


def _variant_keyboard(
    item: dict,
    anime: dict,
    fallback_title: str = "Sem título",
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    rows = []

    sub_variant = _pick_variant(item, dubbed=False)
    dub_variant = _pick_variant(item, dubbed=True)

    if sub_variant:
        rows.append([
            _miniapp_anime_button("🇯🇵 Legendado", sub_variant["id"])
        ])

    if dub_variant:
        rows.append([
            _miniapp_anime_button("🇧🇷 Dublado", dub_variant["id"])
        ])

    default_id = item.get("default_anime_id") or item.get("id")
    if default_id:
        rows.append([
            InlineKeyboardButton("\U0001f4e5 Baixar offline", callback_data=f"off|{default_id}")
        ])

    second_row = []
    anilist_url = _build_anilist_url(anime, fallback_title, item)
    trailer_url = _build_trailer_url(anime)

    if anilist_url:
        second_row.append(InlineKeyboardButton("🧾 Sinopse", url=anilist_url))

    if trailer_url:
        second_row.append(InlineKeyboardButton("🎬 Trailer", url=trailer_url))

    if second_row:
        rows.append(second_row)

    if not rows:
        rows.append([
            _miniapp_anime_button("📺 Ver episódios", default_id)
        ])

    if back_callback:
        rows.append([
            InlineKeyboardButton("🔙 Voltar", callback_data=back_callback)
        ])

    return InlineKeyboardMarkup(rows)


def _episodes_keyboard(anime_id: str, offset: int, items: list, total: int):
    rows = []
    current = []

    for item in items:
        ep = str(item.get("episode", "?"))
        current.append(
            InlineKeyboardButton(
                ep,
                callback_data=f"ep|{anime_id}|{ep}",
            )
        )
        if len(current) == 5:
            rows.append(current)
            current = []

    if current:
        rows.append(current)

    total_pages = max(1, ((total - 1) // EPISODES_PER_PAGE) + 1)
    current_page = (offset // EPISODES_PER_PAGE) + 1
    last_offset = max(0, (total_pages - 1) * EPISODES_PER_PAGE)

    nav_row_1 = []
    nav_row_2 = []

    if current_page > 1:
        nav_row_1.append(
            InlineKeyboardButton("⏪ Primeira", callback_data=f"eps|{anime_id}|0")
        )
        prev_offset = max(0, offset - EPISODES_PER_PAGE)
        nav_row_1.append(
            InlineKeyboardButton("⬅️ Anterior", callback_data=f"eps|{anime_id}|{prev_offset}")
        )

    if current_page < total_pages:
        next_offset = offset + EPISODES_PER_PAGE
        nav_row_2.append(
            InlineKeyboardButton("Próxima ➡️", callback_data=f"eps|{anime_id}|{next_offset}")
        )
        nav_row_2.append(
            InlineKeyboardButton("Última ⏩", callback_data=f"eps|{anime_id}|{last_offset}")
        )

    if nav_row_1:
        rows.append(nav_row_1)

    if nav_row_2:
        rows.append(nav_row_2)

    rows.append([
        InlineKeyboardButton("🔙 Voltar", callback_data=f"anime|{anime_id}")
    ])

    return InlineKeyboardMarkup(rows)


def _episode_button_label(item: dict) -> str:
    label = str(item.get("label") or "").strip()
    if label:
        return label
    season = item.get("season")
    episode_number = item.get("episode_number")
    if season and episode_number and int(season) > 1:
        return f"T{int(season):02d}E{int(episode_number):02d}"
    return str(item.get("episode", "?"))


def _season_episode_value(item: dict) -> str:
    return str(item.get("episode") or item.get("episode_number") or "?")


def _seasons_from_payload(payload: dict) -> list[int]:
    seasons = payload.get("seasons") or []
    if not seasons:
        seasons = sorted({
            int(item.get("season") or 1)
            for item in (payload.get("all_items") or payload.get("items") or [])
            if str(item.get("season") or "1").isdigit()
        })
    return seasons or [1]


def _slice_season_payload(payload: dict, season: int | None, offset: int, limit: int):
    all_items = payload.get("all_items") or payload.get("items") or []
    seasons = _seasons_from_payload(payload)
    selected_season = int(season or (seasons[0] if len(seasons) > 1 else 0) or 0)

    if selected_season:
        season_items = [
            item for item in all_items
            if int(item.get("season") or selected_season) == selected_season
        ]
    else:
        season_items = list(all_items)

    total = len(season_items)
    page_items = season_items[offset: offset + limit]
    return selected_season, seasons, page_items, total


def _episodes_keyboard(anime_id: str, offset: int, items: list, total: int, season: int | None = None, seasons: list[int] | None = None):
    rows = []
    current = []
    seasons = seasons or []

    if season:
        rows.append([
            InlineKeyboardButton(
                f"📚 Temporada {int(season):02d}",
                callback_data=f"spseason|{anime_id}|{int(season)}|{offset}",
            )
        ])

    for item in items:
        ep = _season_episode_value(item)
        current.append(
            InlineKeyboardButton(
                _episode_button_label(item),
                callback_data=f"ep|{anime_id}|{ep}",
            )
        )
        if len(current) == 5:
            rows.append(current)
            current = []

    if current:
        rows.append(current)

    total_pages = max(1, ((total - 1) // EPISODES_PER_PAGE) + 1)
    current_page = (offset // EPISODES_PER_PAGE) + 1
    last_offset = max(0, (total_pages - 1) * EPISODES_PER_PAGE)

    nav_row_1 = []
    nav_row_2 = []

    if current_page > 1:
        first_callback = f"epss|{anime_id}|{int(season)}|0" if season else f"eps|{anime_id}|0"
        nav_row_1.append(InlineKeyboardButton("⏪ Primeira", callback_data=first_callback))
        prev_offset = max(0, offset - EPISODES_PER_PAGE)
        prev_callback = f"epss|{anime_id}|{int(season)}|{prev_offset}" if season else f"eps|{anime_id}|{prev_offset}"
        nav_row_1.append(InlineKeyboardButton("⬅️ Anterior", callback_data=prev_callback))

    if current_page < total_pages:
        next_offset = offset + EPISODES_PER_PAGE
        next_callback = f"epss|{anime_id}|{int(season)}|{next_offset}" if season else f"eps|{anime_id}|{next_offset}"
        last_callback = f"epss|{anime_id}|{int(season)}|{last_offset}" if season else f"eps|{anime_id}|{last_offset}"
        nav_row_2.append(InlineKeyboardButton("Próxima ➡️", callback_data=next_callback))
        nav_row_2.append(InlineKeyboardButton("Última ⏩", callback_data=last_callback))

    if nav_row_1:
        rows.append(nav_row_1)
    if nav_row_2:
        rows.append(nav_row_2)

    rows.append([InlineKeyboardButton("🔙 Voltar", callback_data=f"anime|{anime_id}")])
    return InlineKeyboardMarkup(rows)


def _season_picker_keyboard(anime_id: str, seasons: list[int], current_season: int, offset: int, offline: bool = False):
    rows = []
    current = []
    prefix = "offepss" if offline else "epss"
    for season in seasons:
        label = f"✅ T{int(season):02d}" if int(season) == int(current_season) else f"T{int(season):02d}"
        current.append(InlineKeyboardButton(label, callback_data=f"{prefix}|{anime_id}|{int(season)}|0"))
        if len(current) == 4:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append([
        InlineKeyboardButton("🔙 Voltar aos episódios", callback_data=f"{prefix}|{anime_id}|{int(current_season)}|{offset}")
    ])
    return InlineKeyboardMarkup(rows)


def _offline_menu_text(title: str) -> str:
    safe_title = html.escape((title or "Sem t\u00edtulo").strip())
    return (
        f"\U0001f4e5 <b>{safe_title}</b>\n\n"
        "Escolha a vers\u00e3o que voc\u00ea quer baixar para assistir offline."
    )


def _download_episode_list_text(title: str, offset: int, total: int) -> str:
    safe_title = html.escape((title or "Sem t\u00edtulo").strip())
    current_page = (offset // EPISODES_PER_PAGE) + 1
    total_pages = max(1, ((total - 1) // EPISODES_PER_PAGE) + 1)
    return (
        "\U0001f4e5 <b>Baixar offline</b>\n\n"
        f"<b>{safe_title}</b>\n"
        f"<b>Total de epis\u00f3dios:</b> {total}\n"
        f"<b>P\u00e1gina:</b> {current_page}/{total_pages}\n\n"
        "Toque em um epis\u00f3dio para o bot baixar e enviar no Telegram."
    )


def _offline_version_keyboard(item: dict, back_callback: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    variants = item.get("variants") or []
    sub_variant = _pick_variant(item, dubbed=False)
    dub_variant = _pick_variant(item, dubbed=True)
    default_id = item.get("default_anime_id") or item.get("id")

    if sub_variant:
        rows.append([
            InlineKeyboardButton(
                "\U0001f1ef\U0001f1f5 Baixar legendado",
                callback_data=f"offeps|{sub_variant['id']}|0",
            )
        ])

    if dub_variant:
        rows.append([
            InlineKeyboardButton(
                "\U0001f1e7\U0001f1f7 Baixar dublado",
                callback_data=f"offeps|{dub_variant['id']}|0",
            )
        ])

    if not rows and default_id:
        rows.append([
            InlineKeyboardButton("\U0001f4fa Ver epis\u00f3dios offline", callback_data=f"offeps|{default_id}|0")
        ])

    if len(variants) <= 1 and default_id and rows:
        rows = [[InlineKeyboardButton("\U0001f4fa Ver epis\u00f3dios offline", callback_data=f"offeps|{default_id}|0")]]

    if back_callback:
        rows.append([InlineKeyboardButton("\U0001f519 Voltar", callback_data=back_callback)])
    elif default_id:
        rows.append([InlineKeyboardButton("\U0001f519 Voltar", callback_data=f"anime|{default_id}")])

    return InlineKeyboardMarkup(rows)


def _download_episodes_keyboard(anime_id: str, offset: int, items: list, total: int):
    rows = []
    current = []

    for item in items:
        ep = str(item.get("episode", "?"))
        current.append(InlineKeyboardButton(ep, callback_data=f"dl|{anime_id}|{ep}"))
        if len(current) == 5:
            rows.append(current)
            current = []

    if current:
        rows.append(current)

    total_pages = max(1, ((total - 1) // EPISODES_PER_PAGE) + 1)
    current_page = (offset // EPISODES_PER_PAGE) + 1
    last_offset = max(0, (total_pages - 1) * EPISODES_PER_PAGE)

    nav_row_1 = []
    nav_row_2 = []

    if current_page > 1:
        nav_row_1.append(InlineKeyboardButton("\u23ea Primeira", callback_data=f"offeps|{anime_id}|0"))
        prev_offset = max(0, offset - EPISODES_PER_PAGE)
        nav_row_1.append(InlineKeyboardButton("\u2b05\ufe0f Anterior", callback_data=f"offeps|{anime_id}|{prev_offset}"))

    if current_page < total_pages:
        next_offset = offset + EPISODES_PER_PAGE
        nav_row_2.append(InlineKeyboardButton("Pr\u00f3xima \u27a1\ufe0f", callback_data=f"offeps|{anime_id}|{next_offset}"))
        nav_row_2.append(InlineKeyboardButton("\u00daltima \u23e9", callback_data=f"offeps|{anime_id}|{last_offset}"))

    if nav_row_1:
        rows.append(nav_row_1)
    if nav_row_2:
        rows.append(nav_row_2)

    rows.append([InlineKeyboardButton("\U0001f519 Voltar", callback_data=f"off|{anime_id}")])
    return InlineKeyboardMarkup(rows)


def _download_episodes_keyboard(anime_id: str, offset: int, items: list, total: int, season: int | None = None, seasons: list[int] | None = None):
    rows = []
    current = []
    seasons = seasons or []

    if season:
        rows.append([
            InlineKeyboardButton(
                f"📚 Temporada {int(season):02d}",
                callback_data=f"offspseason|{anime_id}|{int(season)}|{offset}",
            )
        ])

    for item in items:
        ep = _season_episode_value(item)
        current.append(InlineKeyboardButton(_episode_button_label(item), callback_data=f"dl|{anime_id}|{ep}"))
        if len(current) == 5:
            rows.append(current)
            current = []

    if current:
        rows.append(current)

    total_pages = max(1, ((total - 1) // EPISODES_PER_PAGE) + 1)
    current_page = (offset // EPISODES_PER_PAGE) + 1
    last_offset = max(0, (total_pages - 1) * EPISODES_PER_PAGE)

    nav_row_1 = []
    nav_row_2 = []

    if current_page > 1:
        first_callback = f"offepss|{anime_id}|{int(season)}|0" if season else f"offeps|{anime_id}|0"
        nav_row_1.append(InlineKeyboardButton("⏪ Primeira", callback_data=first_callback))
        prev_offset = max(0, offset - EPISODES_PER_PAGE)
        prev_callback = f"offepss|{anime_id}|{int(season)}|{prev_offset}" if season else f"offeps|{anime_id}|{prev_offset}"
        nav_row_1.append(InlineKeyboardButton("⬅️ Anterior", callback_data=prev_callback))

    if current_page < total_pages:
        next_offset = offset + EPISODES_PER_PAGE
        next_callback = f"offepss|{anime_id}|{int(season)}|{next_offset}" if season else f"offeps|{anime_id}|{next_offset}"
        last_callback = f"offepss|{anime_id}|{int(season)}|{last_offset}" if season else f"offeps|{anime_id}|{last_offset}"
        nav_row_2.append(InlineKeyboardButton("Próxima ➡️", callback_data=next_callback))
        nav_row_2.append(InlineKeyboardButton("Última ⏩", callback_data=last_callback))

    if nav_row_1:
        rows.append(nav_row_1)
    if nav_row_2:
        rows.append(nav_row_2)

    rows.append([InlineKeyboardButton("\U0001f519 Voltar", callback_data=f"off|{anime_id}")])
    return InlineKeyboardMarkup(rows)


def _recommend_menu_text() -> str:
    return (
        "🎲 <b>Recomendação aleatória por gênero</b>\n\n"
        "Escolha um gênero abaixo e eu vou sortear um anime aleatório dele."
    )


def _recommend_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("⚔️ Ação", callback_data="rec|genre|acao"),
            InlineKeyboardButton("💖 Romance", callback_data="rec|genre|romance"),
        ],
        [
            InlineKeyboardButton("😂 Comédia", callback_data="rec|genre|comedia"),
            InlineKeyboardButton("😱 Terror", callback_data="rec|genre|terror"),
        ],
        [
            InlineKeyboardButton("🧠 Mistério", callback_data="rec|genre|misterio"),
            InlineKeyboardButton("🪄 Fantasia", callback_data="rec|genre|fantasia"),
        ],
        [
            InlineKeyboardButton("🏐 Esportes", callback_data="rec|genre|esportes"),
            InlineKeyboardButton("😭 Drama", callback_data="rec|genre|drama"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def _recommend_text(anime: dict, genre_key: str) -> str:
    labels = {
        "acao": "⚔️ Ação",
        "romance": "💖 Romance",
        "comedia": "😂 Comédia",
        "terror": "😱 Terror",
        "misterio": "🧠 Mistério",
        "fantasia": "🪄 Fantasia",
        "esportes": "🏐 Esportes",
        "drama": "😭 Drama",
    }

    title = html.escape((anime.get("title") or "Sem título").strip())
    score = anime.get("score")
    episodes = anime.get("episodes")
    genres = anime.get("genres") or []
    description = _strip_html(anime.get("description") or "Sem descrição disponível.")
    description = _truncate_text(description, 420)
    description = html.escape(description)

    label = html.escape(labels.get(genre_key, "🎲 Recomendação"))

    parts = [f"{label}\n", f"🎬 <b>{title}</b>"]

    meta = []
    if score:
        meta.append(f"⭐ <b>{score}</b>")
    if episodes:
        meta.append(f"📺 <b>{episodes} episódios</b>")

    if meta:
        parts.append(" • ".join(meta))

    if genres:
        safe_genres = " • ".join(html.escape(str(g)) for g in genres[:5])
        parts.append(f"🎭 <b>Gêneros:</b> {safe_genres}")

    parts.append("")
    parts.append("📖 <b>Sinopse</b>")
    parts.append(description)

    return "\n".join(parts)


def _recommend_result_keyboard(anime_id: str, genre_key: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📺 Ver episódios", callback_data=f"eps|{anime_id}|0")],
        [
            InlineKeyboardButton("🎭 Trocar gênero", callback_data="rec|menu"),
            InlineKeyboardButton("🎲 Tentar de novo", callback_data=f"rec|try|{genre_key}"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def _quality_key(anime_id: str, episode: str) -> str:
    return f"quality:{anime_id}:{episode}"


def _last_quality_switch_key(anime_id: str, episode: str) -> str:
    return f"quality_switch_last:{anime_id}:{episode}"


def _get_selected_quality(context: ContextTypes.DEFAULT_TYPE, anime_id: str, episode: str) -> str:
    return _normalize_quality(
        context.user_data.get(_quality_key(anime_id, episode), "HD")
    )


def _set_selected_quality(
    context: ContextTypes.DEFAULT_TYPE,
    anime_id: str,
    episode: str,
    quality: str,
):
    context.user_data[_quality_key(anime_id, episode)] = _normalize_quality(quality)


def _available_quality_set(player: dict) -> set:
    qualities = set()

    for q in (player.get("available_qualities") or []):
        normalized = _normalize_quality(str(q))
        if normalized:
            qualities.add(normalized)

    if not qualities:
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

    rows = [
        [InlineKeyboardButton("▶️ Assistir", url=detected_video or "https://t.me")],
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


def _build_miniapp_episode_url(anime_id: str, episode: str, quality: str) -> str:
    quality = _normalize_quality(quality)
    base = MINIAPP_URL.rstrip("/")
    return f"{base}/?anime={anime_id}&ep={episode}&q={quality}"


def _build_miniapp_anime_url(anime_id: str) -> str:
    base = MINIAPP_URL.rstrip("/")
    return f"{base}/?anime={anime_id}"


def _build_miniapp_direct_url(start_param: str = "") -> str:
    username = BOT_USERNAME.lstrip("@")
    app_name = MINIAPP_SHORT_NAME.strip().strip("/")
    if not app_name:
        return ""
    base = f"https://t.me/{username}/{app_name}"
    params = []
    if start_param:
        params.append(f"startapp={quote_plus(start_param)}")
    params.append("mode=fullscreen")
    return f"{base}?{'&'.join(params)}"


def _build_miniapp_anime_direct_url(anime_id: str) -> str:
    return _build_miniapp_direct_url(f"anime_{anime_id}")


def _build_miniapp_episode_direct_url(anime_id: str, episode: str, quality: str) -> str:
    return _build_miniapp_direct_url(f"ep_{anime_id}__{episode}__{_normalize_quality(quality)}")


def _miniapp_anime_button(text: str, anime_id: str) -> InlineKeyboardButton:
    direct_url = _build_miniapp_anime_direct_url(anime_id)
    if direct_url:
        return InlineKeyboardButton(text, url=direct_url)
    return InlineKeyboardButton(text, web_app=WebAppInfo(url=_build_miniapp_anime_url(anime_id)))


def _miniapp_episode_button(text: str, anime_id: str, episode: str, quality: str) -> InlineKeyboardButton:
    direct_url = _build_miniapp_episode_direct_url(anime_id, episode, quality)
    if direct_url:
        return InlineKeyboardButton(text, url=direct_url)
    return InlineKeyboardButton(text, web_app=WebAppInfo(url=_build_miniapp_episode_url(anime_id, episode, quality)))


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
            _miniapp_episode_button("▶️ Assistir", anime_id, str(episode), selected_quality)
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


def _anime_cache_key(anime_id: str) -> str:
    return f"anime_cache:{anime_id}"


def _callback_last_key(user_id: int) -> str:
    return f"callback_last:{user_id}"


def _callback_data_last_key(user_id: int) -> str:
    return f"callback_data_last:{user_id}"


def _user_lock(user_id: int) -> asyncio.Lock:
    lock = _USER_CALLBACK_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _USER_CALLBACK_LOCKS[user_id] = lock
    return lock


def _message_lock(chat_id: int, message_id: int) -> asyncio.Lock:
    key = f"{chat_id}:{message_id}"
    lock = _MESSAGE_EDIT_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _MESSAGE_EDIT_LOCKS[key] = lock
    return lock


def _message_action_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


def _get_inflight_action(chat_id: int, message_id: int):
    return _MESSAGE_INFLIGHT_ACTIONS.get(_message_action_key(chat_id, message_id))


def _set_inflight_action(chat_id: int, message_id: int, action: str):
    _MESSAGE_INFLIGHT_ACTIONS[_message_action_key(chat_id, message_id)] = action


def _clear_inflight_action(chat_id: int, message_id: int):
    _MESSAGE_INFLIGHT_ACTIONS.pop(_message_action_key(chat_id, message_id), None)


def _action_signature(data: str) -> str:
    if not data:
        return ""

    prefixes = ("ep|", "eps|", "epss|", "spseason|", "anime|", "sp|", "sa|", "ql|", "rec|", "var|", "vw|", "unvw|", "offepss|", "offspseason|")
    for prefix in prefixes:
        if data.startswith(prefix):
            return data

    return data


def _loading_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏳ Carregando...", callback_data="noop_loading")]
    ])


async def _set_loading_state(query):
    try:
        await query.edit_message_reply_markup(reply_markup=_loading_keyboard())
    except Exception:
        pass


async def _check_callback_cooldown(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    data: str,
):
    now = _now()

    last_key = _callback_last_key(user_id)
    last_data_key = _callback_data_last_key(user_id)

    last_ts = context.user_data.get(last_key, 0.0)
    last_data = context.user_data.get(last_data_key, "")

    if now - last_ts < CALLBACK_COOLDOWN and last_data == data:
        return "cooldown"

    context.user_data[last_key] = now
    context.user_data[last_data_key] = data
    return "ok"


def _can_switch_quality_now(context: ContextTypes.DEFAULT_TYPE, anime_id: str, episode: str):
    key = _last_quality_switch_key(anime_id, episode)
    now = _now()
    last = context.user_data.get(key, 0.0)

    if now - last < QUALITY_COOLDOWN:
        return False

    context.user_data[key] = now
    return True


async def _get_cached_anime(context: ContextTypes.DEFAULT_TYPE, anime_id: str) -> dict:
    key = _anime_cache_key(anime_id)

    anime = context.user_data.get(key)
    if anime:
        return anime

    async def _fetch():
        return await asyncio.wait_for(get_anime_details(anime_id), timeout=20)

    anime = await _dedup_fetch(
        _GLOBAL_ANIME_CACHE,
        _INFLIGHT_ANIME,
        anime_id,
        ANIME_CACHE_TTL,
        _fetch,
    )
    context.user_data[key] = anime
    return anime


async def _get_cached_episodes(anime_id: str, offset: int, limit: int):
    key = f"{anime_id}|{offset}|{limit}"

    async def _fetch():
        return await asyncio.wait_for(get_episodes(anime_id, offset, limit), timeout=20)

    return await _dedup_fetch(
        _GLOBAL_EPISODES_CACHE,
        _INFLIGHT_EPISODES,
        key,
        EPISODES_CACHE_TTL,
        _fetch,
    )


async def _get_cached_player(anime_id: str, episode: str, quality: str):
    key = f"{anime_id}|{episode}|{quality}"

    async def _fetch():
        return await asyncio.wait_for(get_episode_player(anime_id, episode, quality), timeout=25)

    return await _dedup_fetch(
        _GLOBAL_PLAYER_CACHE,
        _INFLIGHT_PLAYER,
        key,
        PLAYER_CACHE_TTL,
        _fetch,
    )


async def _get_cached_recommendation(genre_key: str):
    key = genre_key

    async def _fetch():
        return await asyncio.wait_for(get_random_anime_by_genre(genre_key), timeout=20)

    return await _dedup_fetch(
        _GLOBAL_RECOMMEND_CACHE,
        _INFLIGHT_RECOMMEND,
        key,
        RECOMMEND_CACHE_TTL,
        _fetch,
    )


async def _safe_edit_text(query, text: str, reply_markup=None):
    try:
        await query.edit_message_text(
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        return True
    except Exception as e:
        error = str(e).lower()

        if "message is not modified" in error:
            try:
                await query.edit_message_reply_markup(reply_markup=reply_markup)
            except Exception:
                pass
            return True

        return False


async def _safe_edit_caption(query, caption: str, reply_markup=None):
    try:
        await query.edit_message_caption(
            caption=caption,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
        return True
    except Exception as e:
        error = str(e).lower()

        if "message is not modified" in error:
            try:
                await query.edit_message_reply_markup(reply_markup=reply_markup)
            except Exception:
                pass
            return True

        return False


async def _safe_edit_photo(
    query,
    photo_url: str,
    caption: str,
    reply_markup=None,
    caption_only: bool = False,
):
    if not photo_url:
        return await _safe_edit_text(query, caption, reply_markup=reply_markup)

    if caption_only:
        ok = await _safe_edit_caption(query, caption, reply_markup=reply_markup)
        if ok:
            return True

    try:
        await query.edit_message_media(
            media=InputMediaPhoto(
                media=photo_url,
                caption=caption,
                parse_mode="HTML",
            ),
            reply_markup=reply_markup,
        )
        return True
    except Exception as e:
        error = str(e).lower()

        if "message is not modified" in error:
            try:
                await query.edit_message_reply_markup(reply_markup=reply_markup)
            except Exception:
                pass
            return True

        ok = await _safe_edit_caption(query, caption, reply_markup=reply_markup)
        if ok:
            return True

        return False


async def _render_grouped_anime(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    item: dict,
    back_callback: str | None = None,
    caption_only: bool = False,
):
    _remember_group_item(context, item)

    anime_id = item.get("default_anime_id") or item.get("id")
    anime = await _get_cached_anime(context, anime_id)

    text = _anime_text(anime)
    keyboard = _variant_keyboard(item, back_callback=back_callback)
    image_url = _anime_main_image(anime)

    if image_url:
        return await _safe_edit_photo(
            query,
            image_url,
            text,
            keyboard,
            caption_only=caption_only,
        )

    return await _safe_edit_text(query, text, keyboard)


async def _render_episode_player(
    query,
    context,
    anime_id: str,
    episode: str,
    caption_only: bool = True,
):
    anime = await _get_cached_anime(context, anime_id)

    selected_quality = _get_selected_quality(context, anime_id, episode)
    player = await _get_cached_player(anime_id, episode, selected_quality)

    detected_video = (player.get("video") or "").strip()
    server = player.get("server", "")
    total_episodes = player.get("total_episodes", 0)
    resolved_quality = _normalize_quality(player.get("quality", selected_quality))
    prev_episode = player.get("prev_episode")
    next_episode = player.get("next_episode")
    available_qualities = _available_quality_set(player)

    _set_selected_quality(context, anime_id, episode, resolved_quality)

    text = _player_text(
        anime.get("title", "Sem título"),
        episode,
        server,
        total_episodes,
        resolved_quality,
    )

    keyboard = _player_keyboard(
        anime_id=anime_id,
        episode=episode,
        detected_video=detected_video,
        prev_episode=prev_episode,
        next_episode=next_episode,
        selected_quality=resolved_quality,
        user_id=query.from_user.id,
        available_qualities=available_qualities,
    )

    image_url = _anime_secondary_image(anime)
    ok = False
    if image_url:
        ok = await _safe_edit_photo(
            query,
            image_url,
            text,
            keyboard,
            caption_only=caption_only,
        )
    else:
        ok = await _safe_edit_text(query, text, keyboard)

    if not ok:
        await _safe_answer_query(query, "⚠️ Não consegui atualizar essa mensagem. Abra novamente.", show_alert=False)

    return {
        "resolved_quality": resolved_quality,
        "available_qualities": available_qualities,
    }


async def _render_grouped_anime(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    item: dict,
    back_callback: str | None = None,
    caption_only: bool = False,
):
    _remember_group_item(context, item)

    anime_id = item.get("default_anime_id") or item.get("id")
    anime = await _get_cached_anime(context, anime_id)
    fallback_title = item.get("title") or anime.get("title") or "Sem título"
    selected_item = _find_variant_by_id(item, anime_id) or item
    is_dubbed = _resolve_is_dubbed(context, anime_id, anime=anime, item=selected_item)

    text = _anime_text(anime, fallback_title=fallback_title, is_dubbed=is_dubbed)

    variants = item.get("variants") or []
    has_subbed = any(not v.get("is_dubbed") for v in variants)
    has_dubbed = any(v.get("is_dubbed") for v in variants)
    available_versions = int(has_subbed) + int(has_dubbed)

    if available_versions <= 1:
        keyboard = _single_anime_keyboard(
            anime_id=anime_id,
            anime=anime,
            fallback_title=fallback_title,
            fallback_item=item,
            back_callback=back_callback,
        )
    else:
        keyboard = _variant_keyboard(
            item,
            anime,
            fallback_title=fallback_title,
            back_callback=back_callback,
        )

    image_url = _anime_main_image(anime)

    if image_url:
        return await _safe_edit_photo(
            query,
            image_url,
            text,
            keyboard,
            caption_only=caption_only,
        )

    return await _safe_edit_text(query, text, keyboard)


async def _render_episode_player(
    query,
    context,
    anime_id: str,
    episode: str,
    caption_only: bool = True,
):
    anime = await _get_cached_anime(context, anime_id)

    selected_quality = _get_selected_quality(context, anime_id, episode)
    player = await _get_cached_player(anime_id, episode, selected_quality)

    detected_video = (player.get("video") or "").strip()
    server = player.get("server", "")
    total_episodes = player.get("total_episodes", 0)
    resolved_quality = _normalize_quality(player.get("quality", selected_quality))
    prev_episode = player.get("prev_episode")
    next_episode = player.get("next_episode")
    available_qualities = _available_quality_set(player)

    _set_selected_quality(context, anime_id, episode, resolved_quality)

    display_title = _format_title_with_version(
        _pick_display_title(anime, anime.get("title") or "Sem título"),
        _resolve_is_dubbed(context, anime_id, anime=anime),
    )

    text = _player_text(
        display_title,
        episode,
        server,
        total_episodes,
        resolved_quality,
    )

    keyboard = _player_keyboard(
        anime_id=anime_id,
        episode=episode,
        detected_video=detected_video,
        prev_episode=prev_episode,
        next_episode=next_episode,
        selected_quality=resolved_quality,
        user_id=query.from_user.id,
        available_qualities=available_qualities,
    )

    image_url = _anime_secondary_image(anime)
    ok = False
    if image_url:
        ok = await _safe_edit_photo(
            query,
            image_url,
            text,
            keyboard,
            caption_only=caption_only,
        )
    else:
        ok = await _safe_edit_text(query, text, keyboard)

    if not ok:
        await _safe_answer_query(query, "⚠️ Não consegui atualizar essa mensagem. Abra novamente.", show_alert=False)

    return {
        "resolved_quality": resolved_quality,
        "available_qualities": available_qualities,
    }


async def _render_single_anime(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    anime_id: str,
    back_callback: str | None = None,
    caption_only: bool = False,
):
    anime = await _get_cached_anime(context, anime_id)
    fallback_title = anime.get("title") or "Sem título"
    is_dubbed = _resolve_is_dubbed(context, anime_id, anime=anime)

    text = _anime_text(
        anime,
        fallback_title=fallback_title,
        is_dubbed=is_dubbed,
    )
    keyboard = _single_anime_keyboard(
        anime_id=anime_id,
        anime=anime,
        fallback_title=fallback_title,
        back_callback=back_callback,
    )

    image_url = _anime_main_image(anime)
    if image_url:
        return await _safe_edit_photo(query, image_url, text, keyboard, caption_only=caption_only)

    return await _safe_edit_text(query, text, keyboard)


async def _render_episodes_page(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    anime_id: str,
    offset: int,
    caption_only: bool = False,
):
    anime = await _get_cached_anime(context, anime_id)
    payload = await _get_cached_episodes(anime_id, offset, EPISODES_PER_PAGE)

    items = payload.get("items", [])
    total = payload.get("total", 0)

    display_title = _format_title_with_version(
        _pick_display_title(anime, anime.get("title") or "Sem título"),
        _resolve_is_dubbed(context, anime_id, anime=anime),
    )

    text = _episode_list_text(
        display_title,
        offset,
        total,
    )

    keyboard = _episodes_keyboard(
        anime_id=anime_id,
        offset=offset,
        items=items,
        total=total,
    )

    image_url = _anime_secondary_image(anime)
    if image_url:
        return await _safe_edit_photo(query, image_url, text, keyboard, caption_only=caption_only)

    return await _safe_edit_text(query, text, keyboard)


async def _render_offline_menu(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    anime_id: str,
    caption_only: bool = False,
):
    grouped_item = _get_group_item(context, anime_id)
    anime = await _get_cached_anime(context, anime_id)

    if not grouped_item:
        grouped_item = {
            "id": anime_id,
            "default_anime_id": anime_id,
            "title": anime.get("title") or anime_id,
            "variants": [{
                "id": anime_id,
                "title": anime.get("title") or anime_id,
                "is_dubbed": _resolve_is_dubbed(context, anime_id, anime=anime) is True,
            }],
        }
        _remember_group_item(context, grouped_item)

    title = _format_title_with_version(
        _pick_display_title(anime, grouped_item.get("title") or "Sem tÃ­tulo"),
        _resolve_is_dubbed(context, anime_id, anime=anime),
    )
    text = _offline_menu_text(title)
    keyboard = _offline_version_keyboard(grouped_item)

    image_url = _anime_secondary_image(anime)
    if image_url:
        return await _safe_edit_photo(query, image_url, text, keyboard, caption_only=caption_only)

    return await _safe_edit_text(query, text, keyboard)


async def _render_download_episodes_page(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    anime_id: str,
    offset: int,
    caption_only: bool = False,
):
    anime = await _get_cached_anime(context, anime_id)
    payload = await _get_cached_episodes(anime_id, offset, EPISODES_PER_PAGE)
    items = payload.get("items", [])
    total = payload.get("total", 0)

    display_title = _format_title_with_version(
        _pick_display_title(anime, anime.get("title") or "Sem tÃ­tulo"),
        _resolve_is_dubbed(context, anime_id, anime=anime),
    )
    text = _download_episode_list_text(display_title, offset, total)
    keyboard = _download_episodes_keyboard(anime_id, offset, items, total)

    image_url = _anime_secondary_image(anime)
    if image_url:
        return await _safe_edit_photo(query, image_url, text, keyboard, caption_only=caption_only)

    return await _safe_edit_text(query, text, keyboard)


async def _render_episodes_page(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    anime_id: str,
    offset: int,
    season: int | None = None,
    caption_only: bool = False,
):
    anime = await _get_cached_anime(context, anime_id)
    payload = await _get_cached_episodes(anime_id, 0, 3000)
    selected_season, seasons, items, total = _slice_season_payload(payload, season, offset, EPISODES_PER_PAGE)

    display_title = _format_title_with_version(
        _pick_display_title(anime, anime.get("title") or "Sem título"),
        _resolve_is_dubbed(context, anime_id, anime=anime),
    )

    if selected_season:
        current_page = (offset // EPISODES_PER_PAGE) + 1
        total_pages = max(1, ((total - 1) // EPISODES_PER_PAGE) + 1)
        text = _season_list_text(display_title, selected_season, total, current_page, total_pages)
    else:
        text = _episode_list_text(display_title, offset, total)

    keyboard = _episodes_keyboard(
        anime_id=anime_id,
        offset=offset,
        items=items,
        total=total,
        season=selected_season or None,
        seasons=seasons,
    )

    image_url = _anime_secondary_image(anime)
    if image_url:
        return await _safe_edit_photo(query, image_url, text, keyboard, caption_only=caption_only)

    return await _safe_edit_text(query, text, keyboard)


async def _render_download_episodes_page(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    anime_id: str,
    offset: int,
    season: int | None = None,
    caption_only: bool = False,
):
    anime = await _get_cached_anime(context, anime_id)
    payload = await _get_cached_episodes(anime_id, 0, 3000)
    selected_season, seasons, items, total = _slice_season_payload(payload, season, offset, EPISODES_PER_PAGE)

    display_title = _format_title_with_version(
        _pick_display_title(anime, anime.get("title") or "Sem título"),
        _resolve_is_dubbed(context, anime_id, anime=anime),
    )

    if selected_season:
        current_page = (offset // EPISODES_PER_PAGE) + 1
        total_pages = max(1, ((total - 1) // EPISODES_PER_PAGE) + 1)
        text = _season_list_text(display_title, selected_season, total, current_page, total_pages, offline=True)
    else:
        text = _download_episode_list_text(display_title, offset, total)

    keyboard = _download_episodes_keyboard(
        anime_id=anime_id,
        offset=offset,
        items=items,
        total=total,
        season=selected_season or None,
        seasons=seasons,
    )

    image_url = _anime_secondary_image(anime)
    if image_url:
        return await _safe_edit_photo(query, image_url, text, keyboard, caption_only=caption_only)

    return await _safe_edit_text(query, text, keyboard)


async def _render_season_picker_page(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    anime_id: str,
    season: int,
    offset: int,
    offline: bool = False,
):
    anime = await _get_cached_anime(context, anime_id)
    payload = await _get_cached_episodes(anime_id, 0, 3000)
    seasons = _seasons_from_payload(payload)
    display_title = _format_title_with_version(
        _pick_display_title(anime, anime.get("title") or "Sem título"),
        _resolve_is_dubbed(context, anime_id, anime=anime),
    )
    text = _season_picker_text(display_title, season, seasons)
    keyboard = _season_picker_keyboard(anime_id, seasons, season, offset, offline=offline)

    image_url = _anime_secondary_image(anime)
    if image_url:
        return await _safe_edit_photo(query, image_url, text, keyboard, caption_only=False)
    return await _safe_edit_text(query, text, keyboard)


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    if not query or not user:
        return

    await _mark_user_seen_safe(user)

    data = query.data or ""
    print("CALLBACK DATA:", data)

    if data == "subcheck":
        await answer_subscription_check(query, user.id)
        return

    if (
        data.startswith(("off|", "offeps|", "offepss|", "offspseason|", "dl|"))
        and user.id not in ADMIN_IDS
    ):
        unlocked, _, _ = _offline_referral_unlocked(user.id)
        if not unlocked:
            await _send_offline_referral_gate(query, user)
            return

    if data == "noop_loading":
        await _safe_answer_query(query, "⏳ Aguarde...", show_alert=False)
        return

    cooldown = await _check_callback_cooldown(context, user.id, data)
    if cooldown == "cooldown":
        await _safe_answer_query(query, "⚠️ Não aperte várias vezes seguidas.", show_alert=False)
        return

    message = query.message
    user_lock = _user_lock(user.id)

    if message:
        msg_lock = _message_lock(message.chat.id, message.message_id)

        current_action = _action_signature(data)
        inflight_action = _get_inflight_action(message.chat.id, message.message_id)

        if inflight_action == current_action:
            await _safe_answer_query(query, "⏳ Essa ação já está sendo processada...", show_alert=False)
            return
    else:
        msg_lock = asyncio.Lock()

    await _safe_answer_query(query, "⏳ Carregando...", show_alert=False)

    try:
        async with user_lock:
            async with msg_lock:
                if message:
                    current_action = _action_signature(data)
                    inflight_action = _get_inflight_action(message.chat.id, message.message_id)

                    if inflight_action == current_action:
                        await _safe_answer_query(query, "⏳ Essa ação já está sendo processada...", show_alert=False)
                        return

                    _set_inflight_action(message.chat.id, message.message_id, current_action)

                if data.startswith(("ep|", "eps|", "epss|", "spseason|", "anime|", "sp|", "sa|", "ql|", "rec|", "var|", "vw|", "unvw|", "off|", "offeps|", "offepss|", "offspseason|")):
                    await _set_loading_state(query)

                if data.startswith("ql|"):
                    parts = data.split("|", 3)
                    if len(parts) != 4:
                        return

                    _, anime_id, episode, requested_quality = parts
                    requested_quality = _normalize_quality(requested_quality)
                    current_quality = _get_selected_quality(context, anime_id, episode)

                    if not _can_switch_quality_now(context, anime_id, episode):
                        await _safe_answer_query(query, "⏳ Aguarde um instante para trocar a qualidade.", show_alert=False)
                        return

                    if current_quality == requested_quality:
                        return

                    _set_selected_quality(context, anime_id, episode, requested_quality)
                    result = await _render_episode_player(
                        query,
                        context,
                        anime_id,
                        episode,
                        caption_only=True,
                    )

                    if result["resolved_quality"] != requested_quality:
                        await _safe_answer_query(
                            query,
                            f"⚠️ Esse episódio não tem {requested_quality} disponível.",
                            show_alert=True,
                        )
                    return

                if data.startswith("vw|"):
                    _, anime_id, episode = data.split("|", 2)

                    anime = await _get_cached_anime(context, anime_id)

                    mark_episode_watched(
                        user_id=user.id,
                        anime_id=anime_id,
                        episode=episode,
                        anime_title=anime.get("title", "Sem título"),
                        username=user.username or user.first_name or "",
                    )

                    await _safe_answer_query(query, "✅ Marcado como visto.", show_alert=False)

                    await _render_episode_player(
                        query,
                        context,
                        anime_id,
                        episode,
                        caption_only=True,
                    )
                    return

                if data.startswith("unvw|"):
                    _, anime_id, episode = data.split("|", 2)

                    anime = await _get_cached_anime(context, anime_id)

                    unmark_episode_watched(
                        user_id=user.id,
                        anime_id=anime_id,
                        episode=episode,
                        anime_title=anime.get("title", "Sem título"),
                        username=user.username or user.first_name or "",
                    )

                    await _safe_answer_query(query, "↩️ Episódio desmarcado.", show_alert=False)

                    await _render_episode_player(
                        query,
                        context,
                        anime_id,
                        episode,
                        caption_only=True,
                    )
                    return

                if data.startswith("off|"):
                    anime_id = data.split("|", 1)[1]
                    ok = await _render_offline_menu(
                        query,
                        context,
                        anime_id,
                        caption_only=False,
                    )
                    if not ok:
                        await _safe_answer_query(query, "NÃ£o consegui abrir o modo offline agora.", show_alert=False)
                    return

                if data.startswith("offspseason|"):
                    _, anime_id, season_str, offset_str = data.split("|", 3)
                    ok = await _render_season_picker_page(
                        query,
                        context,
                        anime_id,
                        int(season_str),
                        int(offset_str),
                        offline=True,
                    )
                    if not ok:
                        await _safe_answer_query(query, "Não consegui abrir as temporadas offline.", show_alert=False)
                    return

                if data.startswith("offepss|"):
                    _, anime_id, season_str, offset_str = data.split("|", 3)
                    ok = await _render_download_episodes_page(
                        query,
                        context,
                        anime_id,
                        int(offset_str),
                        season=int(season_str),
                        caption_only=False,
                    )
                    if not ok:
                        await _safe_answer_query(query, "Não consegui abrir os episódios offline.", show_alert=False)
                    return

                if data.startswith("offeps|"):
                    _, anime_id, offset_str = data.split("|", 2)
                    offset = int(offset_str)
                    ok = await _render_download_episodes_page(
                        query,
                        context,
                        anime_id,
                        offset,
                        caption_only=False,
                    )
                    if not ok:
                        await _safe_answer_query(query, "NÃ£o consegui abrir os episÃ³dios offline.", show_alert=False)
                    return

                if data.startswith("dl|"):
                    _, anime_id, episode = data.split("|", 2)

                    anime = await _get_cached_anime(context, anime_id)
                    selected_quality = _get_selected_quality(context, anime_id, episode)
                    player = await _get_cached_player(anime_id, episode, selected_quality)
                    video_url = (player.get("video") or "").strip()
                    video_candidates = []
                    for label, url in (player.get("videos") or {}).items():
                        candidate_url = str(url or "").strip()
                        if candidate_url:
                            video_candidates.append({"label": _normalize_quality(label), "url": candidate_url})
                    resolved_quality = _normalize_quality(player.get("quality", selected_quality))
                    title = _format_title_with_version(
                        _pick_display_title(anime, anime.get("title") or "Sem t\u00edtulo"),
                        _resolve_is_dubbed(context, anime_id, anime=anime),
                    )

                    _safe_log_event(
                        event_type="download_click",
                        user_id=user.id,
                        username=user.username or user.first_name or "",
                        anime_id=anime_id,
                        anime_title=anime.get("title", "Sem t\u00edtulo"),
                        episode=str(episode),
                        extra=resolved_quality,
                    )

                    if not video_url:
                        await _safe_answer_query(query, "N\u00e3o encontrei o v\u00eddeo desse epis\u00f3dio.", show_alert=True)
                        return

                    caption = (
                        f"<b>{html.escape(title)}</b>\n"
                        f"<b>Epis\u00f3dio:</b> {html.escape(str(episode))}\n"
                        f"<b>Qualidade:</b> {html.escape(resolved_quality)}"
                    )

                    try:
                        await enqueue_video_download(
                            context.application,
                            VideoDownloadJob(
                                user_id=user.id,
                                chat_id=query.message.chat_id,
                                anime_id=anime_id,
                                episode=str(episode),
                                quality=resolved_quality,
                                title=title,
                                video_url=video_url,
                                caption=caption,
                                video_urls=video_candidates,
                            ),
                        )
                    except RuntimeError as error:
                        await _safe_answer_query(query, str(error), show_alert=True)
                        return

                    await _safe_answer_query(query, "Pedido de download enviado para a fila.", show_alert=False)
                    return

                if False and data.startswith("watch|"):
                    _, anime_id, episode = data.split("|", 2)

                    anime = await _get_cached_anime(context, anime_id)
                    selected_quality = _get_selected_quality(context, anime_id, episode)
                    player = await _get_cached_player(anime_id, episode, selected_quality)
                    video_url = (player.get("video") or "").strip()

                    _safe_log_event(
                        event_type="watch_click",
                        user_id=user.id,
                        username=user.username or user.first_name or "",
                        anime_id=anime_id,
                        anime_title=anime.get("title", "Sem título"),
                        episode=str(episode),
                        extra=selected_quality,
                    )

                    if not video_url:
                        await _safe_answer_query(query, "❌ Não encontrei o vídeo desse episódio.", show_alert=True)
                        return

                    try:
                        await query.message.reply_text(
                            f"▶️ <b>{html.escape(anime.get('title', 'Sem título'))}</b>\n"
                            f"🎞 <b>Episódio:</b> {html.escape(str(episode))}\n"
                            f"🎚 <b>Qualidade:</b> {html.escape(selected_quality)}\n\n"
                            f"<a href=\"{html.escape(video_url, quote=True)}\">Clique aqui para assistir</a>",
                            parse_mode="HTML",
                            disable_web_page_preview=False,
                        )
                    except Exception:
                        await _safe_answer_query(query, "⚠️ Não consegui enviar o link agora.", show_alert=True)

                    return

                if data.startswith("watch|"):
                    _, anime_id, episode = data.split("|", 2)

                    anime = await _get_cached_anime(context, anime_id)
                    selected_quality = _get_selected_quality(context, anime_id, episode)
                    player = await _get_cached_player(anime_id, episode, selected_quality)
                    video_url = (player.get("video") or "").strip()
                    miniapp_url = _build_miniapp_episode_direct_url(anime_id, episode, selected_quality)

                    _safe_log_event(
                        event_type="watch_click",
                        user_id=user.id,
                        username=user.username or user.first_name or "",
                        anime_id=anime_id,
                        anime_title=anime.get("title", "Sem título"),
                        episode=str(episode),
                        extra=selected_quality,
                    )

                    if not video_url:
                        await _safe_answer_query(query, "❌ Não encontrei o vídeo desse episódio.", show_alert=True)
                        return

                    try:
                        await query.message.reply_text(
                            f"▶️ <b>{html.escape(_format_title_with_version(anime.get('title', 'Sem título'), _resolve_is_dubbed(context, anime_id, anime=anime)))}</b>\n"
                            f"🎞 <b>Episódio:</b> {html.escape(str(episode))}\n"
                            f"🎚 <b>Qualidade:</b> {html.escape(selected_quality)}\n\n"
                            f"<a href=\"{html.escape(miniapp_url, quote=True)}\">Abrir no MiniApp</a>",
                            parse_mode="HTML",
                            disable_web_page_preview=False,
                        )
                    except Exception:
                        await _safe_answer_query(query, "⚠️ Não consegui enviar o MiniApp agora.", show_alert=True)

                    return

                if data == "rec|menu":
                    text = _recommend_menu_text()
                    keyboard = _recommend_menu_keyboard()

                    has_photo = bool(getattr(query.message, "photo", None))

                    if has_photo:
                        ok = await _safe_edit_caption(query, text, keyboard)
                    else:
                        ok = await _safe_edit_text(query, text, keyboard)

                    if not ok:
                        await _safe_answer_query(query, "⚠️ Não consegui abrir o menu agora.", show_alert=False)
                    return

                if data.startswith("rec|genre|") or data.startswith("rec|try|"):
                    parts = data.split("|")
                    if len(parts) < 3:
                        return

                    genre_key = parts[2]

                    anime = await _get_cached_recommendation(genre_key)
                    anime_id = anime.get("id", "")

                    text = _recommend_text(anime, genre_key)
                    keyboard = _recommend_result_keyboard(anime_id, genre_key)
                    image_url = _anime_main_image(anime)

                    ok = False
                    if image_url:
                        ok = await _safe_edit_photo(query, image_url, text, keyboard, caption_only=False)
                    else:
                        ok = await _safe_edit_text(query, text, keyboard)

                    if not ok:
                        await _safe_answer_query(query, "⚠️ Não consegui recomendar outro anime agora.", show_alert=False)
                    return

                if data.startswith("sp|"):
                    _, token, page_str = data.split("|", 2)
                    page = int(page_str)

                    session = context.user_data.get(f"search_session:{token}")
                    if not session:
                        await _safe_answer_query(query, "A busca expirou. Faça outra.", show_alert=True)
                        return

                    session["page"] = page

                    raw_query = session["query"]
                    results = session["results"]
                    total = len(results)

                    text = _search_text(raw_query, page, total)
                    keyboard = _search_keyboard(results, page, total, token)

                    ok = await _safe_edit_photo(
                        query,
                        SEARCH_BANNER_URL,
                        text,
                        keyboard,
                        caption_only=False,
                    )

                    if not ok:
                        await _safe_answer_query(query, "⚠️ Não consegui atualizar a página.", show_alert=False)
                    return

                if data.startswith("sa|"):
                    _, token, idx_str = data.split("|", 2)
                    idx = int(idx_str)

                    session = context.user_data.get(f"search_session:{token}")
                    if not session:
                        await _safe_answer_query(query, "A busca expirou. Faça outra.", show_alert=True)
                        return

                    results = session["results"]
                    if idx < 0 or idx >= len(results):
                        await _safe_answer_query(query, "Resultado inválido.", show_alert=True)
                        return

                    item = results[idx]
                    _remember_group_item(context, item)

                    current_page = session.get("page", 1)

                    anime_id = item.get("default_anime_id") or item.get("id")
                    anime = await _get_cached_anime(context, anime_id)

                    _safe_log_event(
                        event_type="anime_open",
                        user_id=user.id,
                        username=user.username or user.first_name or "",
                        anime_id=anime_id,
                        anime_title=anime.get("title", "Sem título"),
                    )

                    ok = await _render_grouped_anime(
                        query,
                        context,
                        item,
                        back_callback=f"sp|{token}|{current_page}",
                        caption_only=False,
                    )

                    if not ok:
                        await _safe_answer_query(query, "⚠️ Não consegui abrir esse anime.", show_alert=False)
                    return

                if data.startswith("anime|"):
                    anime_id = data.split("|", 1)[1]

                    grouped_item = _get_group_item(context, anime_id)
                    if grouped_item:
                        default_id = grouped_item.get("default_anime_id") or grouped_item.get("id")
                        anime = await _get_cached_anime(context, default_id)

                        _safe_log_event(
                            event_type="anime_open",
                            user_id=user.id,
                            username=user.username or user.first_name or "",
                            anime_id=default_id,
                            anime_title=anime.get("title", "Sem título"),
                        )

                        ok = await _render_grouped_anime(
                            query,
                            context,
                            grouped_item,
                            caption_only=False,
                        )

                        if not ok:
                            await _safe_answer_query(query, "⚠️ Não consegui voltar para essa obra.", show_alert=False)
                        return

                    anime = await _get_cached_anime(context, anime_id)

                    _safe_log_event(
                        event_type="anime_open",
                        user_id=user.id,
                        username=user.username or user.first_name or "",
                        anime_id=anime_id,
                        anime_title=anime.get("title", "Sem título"),
                    )

                    ok = await _render_single_anime(
                        query,
                        context,
                        anime_id,
                        caption_only=False,
                    )

                    if not ok:
                        await _safe_answer_query(query, "⚠️ Não consegui voltar para esse anime.", show_alert=False)
                    return

                if False and data.startswith("anime|"):
                    anime_id = data.split("|", 1)[1]

                    grouped_item = _get_group_item(context, anime_id)
                    if grouped_item:
                        default_id = grouped_item.get("default_anime_id") or grouped_item.get("id")
                        anime = await _get_cached_anime(context, default_id)

                        _safe_log_event(
                            event_type="anime_open",
                            user_id=user.id,
                            username=user.username or user.first_name or "",
                            anime_id=default_id,
                            anime_title=anime.get("title", "Sem título"),
                        )

                        ok = await _render_grouped_anime(
                            query,
                            context,
                            grouped_item,
                            caption_only=False,
                        )

                        if not ok:
                            await _safe_answer_query(query, "⚠️ Não consegui voltar para essa obra.", show_alert=False)
                        return

                    anime = await _get_cached_anime(context, anime_id)

                    _safe_log_event(
                        event_type="anime_open",
                        user_id=user.id,
                        username=user.username or user.first_name or "",
                        anime_id=anime_id,
                        anime_title=anime.get("title", "Sem título"),
                    )

                    text = _anime_text(anime)
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("📺 Ver episódios", callback_data=f"eps|{anime_id}|0")]
                    ])

                    image_url = _anime_main_image(anime)
                    ok = False
                    if image_url:
                        ok = await _safe_edit_photo(query, image_url, text, keyboard, caption_only=False)
                    else:
                        ok = await _safe_edit_text(query, text, keyboard)

                    if not ok:
                        await _safe_answer_query(query, "⚠️ Não consegui voltar para esse anime.", show_alert=False)
                    return

                if data.startswith("var|"):
                    anime_id = data.split("|", 1)[1]
                    grouped_item = _get_group_item(context, anime_id)

                    if grouped_item:
                        _remember_group_item(context, grouped_item)

                    anime = await _get_cached_anime(context, anime_id)

                    _safe_log_event(
                        event_type="variant_open",
                        user_id=user.id,
                        username=user.username or user.first_name or "",
                        anime_id=anime_id,
                        anime_title=anime.get("title", "Sem título"),
                    )

                    ok = await _render_episodes_page(
                        query,
                        context,
                        anime_id,
                        0,
                        caption_only=False,
                    )

                    if not ok:
                        await _safe_answer_query(query, "⚠️ Não consegui abrir essa versão.", show_alert=False)
                    return

                if False and data.startswith("var|"):
                    anime_id = data.split("|", 1)[1]
                    grouped_item = _get_group_item(context, anime_id)

                    if grouped_item:
                        _remember_group_item(context, grouped_item)

                    anime = await _get_cached_anime(context, anime_id)

                    _safe_log_event(
                        event_type="variant_open",
                        user_id=user.id,
                        username=user.username or user.first_name or "",
                        anime_id=anime_id,
                        anime_title=anime.get("title", "Sem título"),
                    )

                    payload = await _get_cached_episodes(anime_id, 0, EPISODES_PER_PAGE)
                    items = payload.get("items", [])
                    total = payload.get("total", 0)

                    text = _episode_list_text(
                        anime.get("title", "Sem título"),
                        0,
                        total,
                    )

                    keyboard = _episodes_keyboard(
                        anime_id=anime_id,
                        offset=0,
                        items=items,
                        total=total,
                    )

                    image_url = _anime_secondary_image(anime)
                    ok = False
                    if image_url:
                        ok = await _safe_edit_photo(query, image_url, text, keyboard, caption_only=False)
                    else:
                        ok = await _safe_edit_text(query, text, keyboard)

                    if not ok:
                        await _safe_answer_query(query, "⚠️ Não consegui abrir essa versão.", show_alert=False)
                    return

                if data.startswith("spseason|"):
                    _, anime_id, season_str, offset_str = data.split("|", 3)
                    ok = await _render_season_picker_page(
                        query,
                        context,
                        anime_id,
                        int(season_str),
                        int(offset_str),
                        offline=False,
                    )
                    if not ok:
                        await _safe_answer_query(query, "Não consegui abrir as temporadas.", show_alert=False)
                    return

                if data.startswith("epss|"):
                    _, anime_id, season_str, offset_str = data.split("|", 3)
                    ok = await _render_episodes_page(
                        query,
                        context,
                        anime_id,
                        int(offset_str),
                        season=int(season_str),
                        caption_only=False,
                    )
                    if not ok:
                        await _safe_answer_query(query, "Não consegui abrir os episódios.", show_alert=False)
                    return

                if data.startswith("eps|"):
                    _, anime_id, offset_str = data.split("|", 2)
                    offset = int(offset_str)

                    ok = await _render_episodes_page(
                        query,
                        context,
                        anime_id,
                        offset,
                        caption_only=False,
                    )

                    if not ok:
                        await _safe_answer_query(query, "⚠️ Não consegui abrir os episódios.", show_alert=False)
                    return

                if False and data.startswith("eps|"):
                    _, anime_id, offset_str = data.split("|", 2)
                    offset = int(offset_str)

                    anime = await _get_cached_anime(context, anime_id)
                    payload = await _get_cached_episodes(anime_id, offset, EPISODES_PER_PAGE)

                    items = payload.get("items", [])
                    total = payload.get("total", 0)

                    text = _episode_list_text(
                        anime.get("title", "Sem título"),
                        offset,
                        total,
                    )

                    keyboard = _episodes_keyboard(
                        anime_id=anime_id,
                        offset=offset,
                        items=items,
                        total=total,
                    )

                    image_url = _anime_secondary_image(anime)
                    ok = False
                    if image_url:
                        ok = await _safe_edit_photo(query, image_url, text, keyboard, caption_only=False)
                    else:
                        ok = await _safe_edit_text(query, text, keyboard)

                    if not ok:
                        await _safe_answer_query(query, "⚠️ Não consegui abrir os episódios.", show_alert=False)
                    return

                if data.startswith("ep|"):
                    _, anime_id, episode = data.split("|", 2)

                    if not context.user_data.get(_quality_key(anime_id, episode)):
                        _set_selected_quality(context, anime_id, episode, "HD")

                    anime = await _get_cached_anime(context, anime_id)

                    _safe_log_event(
                        event_type="episode_open",
                        user_id=user.id,
                        username=user.username or user.first_name or "",
                        anime_id=anime_id,
                        anime_title=anime.get("title", "Sem título"),
                        episode=str(episode),
                    )

                    await _render_episode_player(
                        query,
                        context,
                        anime_id,
                        episode,
                        caption_only=False,
                    )
                    return

    except asyncio.TimeoutError:
        print("ERRO NO CALLBACK: Timeout")
        await _safe_answer_query(query, "⏳ Demorou demais para carregar. Tente de novo.", show_alert=True)
    except Exception as e:
        print("ERRO NO CALLBACK:", repr(e))
        traceback.print_exc()
        await _safe_answer_query(query, "❌ Ocorreu um erro. Tente novamente.", show_alert=True)
    finally:
        if message:
            _clear_inflight_action(message.chat.id, message.message_id)
