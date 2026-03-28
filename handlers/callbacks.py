import asyncio
import html
import re
import time
import traceback

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.ext import ContextTypes

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

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

EPISODES_PER_PAGE = 15
SEARCH_RESULTS_PER_PAGE = 8

CALLBACK_COOLDOWN = 0.8        # reduzido: 1.0 → 0.8s para melhor UX
QUALITY_COOLDOWN = 0.5         # reduzido: 0.8 → 0.5s

ANIME_CACHE_TTL = 60 * 30
EPISODES_CACHE_TTL = 60 * 10
PLAYER_CACHE_TTL = 60 * 10
RECOMMEND_CACHE_TTL = 60 * 5

GLOBAL_FETCH_SEMAPHORE = asyncio.Semaphore(60)  # aumentado: 40 → 60

SEARCH_BANNER_URL = (
    "https://photo.chelpbot.me/AgACAgEAAxkBaL-UMWnDPUdoNCaz4ZUFvzeOHSVXh0oRAALTC2sbdnEYRrjsVpeCeT08AQADAgADeQADOgQ/photo.jpg"
)

# ---------------------------------------------------------------------------
# Regex pré-compilados (evita recompilação a cada chamada)
# ---------------------------------------------------------------------------

_RE_HTML_TAGS = re.compile(r"<[^>]+>")
_RE_SCORE = re.compile(r"\b\d+\.\d+\b")
_RE_CLASS = re.compile(r"\bA(?:10|12|14|16|18|L)\b", re.IGNORECASE)
_RE_LIVRE = re.compile(r"\bLIVRE\b", re.IGNORECASE)
_RE_NA = re.compile(r"\bN/?A\b", re.IGNORECASE)
_RE_EMPTY_PARENS = re.compile(r"\(\s*\)")
_RE_SPACES = re.compile(r"\s{2,}")

# ---------------------------------------------------------------------------
# Caches globais com tamanho máximo (evita leak de memória)
# ---------------------------------------------------------------------------

_MAX_CACHE_SIZE = 512

_GLOBAL_ANIME_CACHE: dict = {}
_GLOBAL_EPISODES_CACHE: dict = {}
_GLOBAL_PLAYER_CACHE: dict = {}
_GLOBAL_RECOMMEND_CACHE: dict = {}

_INFLIGHT_ANIME: dict = {}
_INFLIGHT_EPISODES: dict = {}
_INFLIGHT_PLAYER: dict = {}
_INFLIGHT_RECOMMEND: dict = {}

# ---------------------------------------------------------------------------
# Locks por usuário e por mensagem
# ---------------------------------------------------------------------------

_USER_CALLBACK_LOCKS: dict[int, asyncio.Lock] = {}
_MESSAGE_EDIT_LOCKS: dict[str, asyncio.Lock] = {}
_MESSAGE_INFLIGHT_ACTIONS: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Helpers de tempo e cache
# ---------------------------------------------------------------------------

def _now() -> float:
    return time.monotonic()


def _cache_get(cache: dict, key: str, ttl: int):
    item = cache.get(key)
    if item is None:
        return None
    if _now() - item["time"] > ttl:
        del cache[key]
        return None
    return item["data"]


def _cache_set(cache: dict, key: str, data, max_size: int = _MAX_CACHE_SIZE):
    # Evicção simples: se exceder o limite, remove entradas mais antigas
    if len(cache) >= max_size:
        # Remove os 10% mais antigos
        evict_count = max(1, max_size // 10)
        oldest = sorted(cache.items(), key=lambda kv: kv[1]["time"])[:evict_count]
        for k, _ in oldest:
            cache.pop(k, None)

    cache[key] = {"time": _now(), "data": data}


async def _dedup_fetch(cache: dict, inflight: dict, key: str, ttl: int, coro_factory):
    # Fast-path: sem lock, leitura direta
    cached = _cache_get(cache, key, ttl)
    if cached is not None:
        return cached

    task = inflight.get(key)
    if task is not None:
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


# ---------------------------------------------------------------------------
# Helpers Telegram
# ---------------------------------------------------------------------------

async def _safe_answer_query(query, text: str | None = None, show_alert: bool = False):
    try:
        if text is None:
            await query.answer()
        else:
            await query.answer(text, show_alert=show_alert)
    except Exception:
        pass


async def _mark_user_seen_bg(user):
    """Fire-and-forget: registra usuário ativo sem bloquear o callback."""
    asyncio.create_task(_mark_user_seen_task(user))


async def _mark_user_seen_task(user):
    try:
        result = mark_user_seen(user.id, user.username or user.first_name or "")
        if asyncio.isfuture(result) or asyncio.iscoroutine(result):
            await result
    except Exception as e:
        print("ERRO AO MARCAR USUÁRIO ATIVO:", repr(e))


def _safe_log_event(**kwargs):
    try:
        log_event(**kwargs)
    except Exception as e:
        print("ERRO AO SALVAR MÉTRICA:", repr(e))


# ---------------------------------------------------------------------------
# Helpers de texto
# ---------------------------------------------------------------------------

def _strip_html(text: str) -> str:
    return _RE_HTML_TAGS.sub("", text or "")


def _truncate_text(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _anime_main_image(anime: dict) -> str:
    return (
        anime.get("cover_url")
        or anime.get("media_image_url")
        or anime.get("banner_url")
        or ""
    ).strip()


def _anime_secondary_image(anime: dict) -> str:
    return (
        anime.get("media_image_url")
        or anime.get("cover_url")
        or anime.get("banner_url")
        or ""
    ).strip()


def _anime_text(anime: dict) -> str:
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
        genres_block = f"\n🎭 <b>Gêneros:</b>\n<code>{safe_genres}</code>\n"

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


def _episode_list_text(title: str, offset: int, total: int) -> str:
    safe_title = html.escape((title or "Sem título").strip())
    current_page = (offset // EPISODES_PER_PAGE) + 1
    total_pages = max(1, ((total - 1) // EPISODES_PER_PAGE) + 1)
    return (
        f"📺 <b>{safe_title}</b>\n\n"
        f"🎞 <b>Total de episódios:</b> {total}\n"
        f"📄 <b>Página:</b> {current_page}/{total_pages}\n\n"
        f"Escolha um episódio abaixo:"
    )


# Dict lookup é O(1) e mais rápido que if/elif em cadeia
_SERVER_NAME_MAP = {
    "BLOGGER": "BLOGGER",
    "GOOGLEVIDEO": "GOOGLEVIDEO",
}

def _display_server_name(server: str) -> str:
    return _SERVER_NAME_MAP.get((server or "").upper().strip(), server or "PADRÃO")


_QUALITY_MAP = {
    "FULLHD": "HD", "FHD": "HD", "1080P": "HD",
    "HD": "HD", "720P": "HD",
    "SD": "SD", "480P": "SD", "360P": "SD",
}

def _normalize_quality(value: str) -> str:
    return _QUALITY_MAP.get((value or "").upper().strip(), "HD")


def _player_text(title: str, episode: str, server: str, total_episodes: int, quality: str) -> str:
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


def _search_text(query: str, page: int, total: int) -> str:
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
    title = _RE_SCORE.sub("", title)
    title = _RE_CLASS.sub("", title)
    title = _RE_LIVRE.sub("", title)
    title = _RE_NA.sub("", title)
    title = _RE_EMPTY_PARENS.sub("", title)
    title = _RE_SPACES.sub(" ", title).strip(" -–|•")
    return title or "Sem título"


# ---------------------------------------------------------------------------
# Teclados
# ---------------------------------------------------------------------------

def _search_keyboard(results: list, page: int, total: int, token: str) -> InlineKeyboardMarkup:
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


def _anime_group_map_key(anime_id: str) -> str:
    return f"ag:{anime_id}"


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
    for variant in (item.get("variants") or []):
        if bool(variant.get("is_dubbed")) == dubbed:
            return variant
    return None


def _variant_keyboard(item: dict, back_callback: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    sub_variant = _pick_variant(item, dubbed=False)
    dub_variant = _pick_variant(item, dubbed=True)

    if sub_variant:
        rows.append([InlineKeyboardButton("🇯🇵 Legendado", callback_data=f"var|{sub_variant['id']}")])
    if dub_variant:
        rows.append([InlineKeyboardButton("🇧🇷 Dublado", callback_data=f"var|{dub_variant['id']}")])

    if not rows:
        default_id = item.get("default_anime_id") or item.get("id")
        rows.append([InlineKeyboardButton("📺 Ver episódios", callback_data=f"eps|{default_id}|0")])

    if back_callback:
        rows.append([InlineKeyboardButton("🔙 Voltar", callback_data=back_callback)])

    return InlineKeyboardMarkup(rows)


def _episodes_keyboard(anime_id: str, offset: int, items: list, total: int) -> InlineKeyboardMarkup:
    rows = []
    current = []
    for item in items:
        ep = str(item.get("episode", "?"))
        current.append(InlineKeyboardButton(ep, callback_data=f"ep|{anime_id}|{ep}"))
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
        nav_row_1.append(InlineKeyboardButton("⏪ Primeira", callback_data=f"eps|{anime_id}|0"))
        prev_offset = max(0, offset - EPISODES_PER_PAGE)
        nav_row_1.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"eps|{anime_id}|{prev_offset}"))

    if current_page < total_pages:
        next_offset = offset + EPISODES_PER_PAGE
        nav_row_2.append(InlineKeyboardButton("Próxima ➡️", callback_data=f"eps|{anime_id}|{next_offset}"))
        nav_row_2.append(InlineKeyboardButton("Última ⏩", callback_data=f"eps|{anime_id}|{last_offset}"))

    if nav_row_1:
        rows.append(nav_row_1)
    if nav_row_2:
        rows.append(nav_row_2)

    rows.append([InlineKeyboardButton("🔙 Voltar", callback_data=f"anime|{anime_id}")])
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


_GENRE_LABELS = {
    "acao": "⚔️ Ação",
    "romance": "💖 Romance",
    "comedia": "😂 Comédia",
    "terror": "😱 Terror",
    "misterio": "🧠 Mistério",
    "fantasia": "🪄 Fantasia",
    "esportes": "🏐 Esportes",
    "drama": "😭 Drama",
}


def _recommend_text(anime: dict, genre_key: str) -> str:
    title = html.escape((anime.get("title") or "Sem título").strip())
    score = anime.get("score")
    episodes = anime.get("episodes")
    genres = anime.get("genres") or []
    description = _strip_html(anime.get("description") or "Sem descrição disponível.")
    description = _truncate_text(description, 420)
    description = html.escape(description)
    label = html.escape(_GENRE_LABELS.get(genre_key, "🎲 Recomendação"))

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
    parts.extend(["", "📖 <b>Sinopse</b>", description])

    return "\n".join(parts)


def _recommend_result_keyboard(anime_id: str, genre_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📺 Ver episódios", callback_data=f"eps|{anime_id}|0")],
        [
            InlineKeyboardButton("🎭 Trocar gênero", callback_data="rec|menu"),
            InlineKeyboardButton("🎲 Tentar de novo", callback_data=f"rec|try|{genre_key}"),
        ],
    ])


# ---------------------------------------------------------------------------
# Qualidade
# ---------------------------------------------------------------------------

def _quality_key(anime_id: str, episode: str) -> str:
    return f"q:{anime_id}:{episode}"


def _last_quality_switch_key(anime_id: str, episode: str) -> str:
    return f"qs:{anime_id}:{episode}"


def _get_selected_quality(context: ContextTypes.DEFAULT_TYPE, anime_id: str, episode: str) -> str:
    return _normalize_quality(context.user_data.get(_quality_key(anime_id, episode), "HD"))


def _set_selected_quality(context: ContextTypes.DEFAULT_TYPE, anime_id: str, episode: str, quality: str):
    context.user_data[_quality_key(anime_id, episode)] = _normalize_quality(quality)


def _available_quality_set(player: dict) -> set:
    qualities: set[str] = set()
    for q in (player.get("available_qualities") or []):
        n = _normalize_quality(str(q))
        if n:
            qualities.add(n)
    if not qualities:
        videos = player.get("videos") or {}
        if isinstance(videos, dict):
            for q in videos:
                n = _normalize_quality(str(q))
                if n:
                    qualities.add(n)
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
) -> InlineKeyboardMarkup:
    selected_quality = _normalize_quality(selected_quality)
    available_qualities = available_qualities or set()

    hd_label = "HD" + (" 🚫" if available_qualities and "HD" not in available_qualities else "")
    sd_label = "SD" + (" 🚫" if available_qualities and "SD" not in available_qualities else "")

    if selected_quality == "HD":
        hd_label += " 🔘"
    else:
        sd_label += " 🔘"

    watched = is_episode_watched(user_id, anime_id, episode)
    watch_toggle_button = InlineKeyboardButton(
        "❌ Desmarcar como visto" if watched else "✅ Marcar como visto",
        callback_data=f"unvw|{anime_id}|{episode}" if watched else f"vw|{anime_id}|{episode}",
    )

    rows = [
        [InlineKeyboardButton("▶️ Assistir", url=detected_video or "https://t.me")],
        [watch_toggle_button],
        [
            InlineKeyboardButton(hd_label, callback_data=f"ql|{anime_id}|{episode}|HD"),
            InlineKeyboardButton(sd_label, callback_data=f"ql|{anime_id}|{episode}|SD"),
        ],
    ]

    nav = []
    if prev_episode:
        nav.append(InlineKeyboardButton("⏮ Anterior", callback_data=f"ep|{anime_id}|{prev_episode}"))
    if next_episode:
        nav.append(InlineKeyboardButton("Próximo ⏭", callback_data=f"ep|{anime_id}|{next_episode}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("📋 Lista de episódios", callback_data=f"eps|{anime_id}|0")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Cache de anime por usuário
# ---------------------------------------------------------------------------

def _anime_cache_key(anime_id: str) -> str:
    return f"ac:{anime_id}"


async def _get_cached_anime(context: ContextTypes.DEFAULT_TYPE, anime_id: str) -> dict:
    key = _anime_cache_key(anime_id)
    anime = context.user_data.get(key)
    if anime:
        return anime

    async def _fetch():
        return await asyncio.wait_for(get_anime_details(anime_id), timeout=20)

    anime = await _dedup_fetch(_GLOBAL_ANIME_CACHE, _INFLIGHT_ANIME, anime_id, ANIME_CACHE_TTL, _fetch)
    context.user_data[key] = anime
    return anime


async def _get_cached_episodes(anime_id: str, offset: int, limit: int):
    """
    Cache por anime_id apenas — o fatiamento é feito in-memory.
    Evita fragmentação de cache por (anime_id, offset, limit).
    """
    async def _fetch():
        return await asyncio.wait_for(get_episodes(anime_id, 0, 9999), timeout=20)

    payload = await _dedup_fetch(
        _GLOBAL_EPISODES_CACHE, _INFLIGHT_EPISODES, anime_id, EPISODES_CACHE_TTL, _fetch
    )

    # Fatia in-memory sem novo request
    all_items = payload.get("all_items") or payload.get("items", [])
    total = payload.get("total", len(all_items))
    page = all_items[offset: offset + limit]

    return {
        "items": page,
        "total": total,
        "by_episode": payload.get("by_episode", {}),
        "all_items": all_items,
    }


async def _get_cached_player(anime_id: str, episode: str, quality: str):
    key = f"{anime_id}|{episode}|{quality}"

    async def _fetch():
        return await asyncio.wait_for(get_episode_player(anime_id, episode, quality), timeout=25)

    return await _dedup_fetch(_GLOBAL_PLAYER_CACHE, _INFLIGHT_PLAYER, key, PLAYER_CACHE_TTL, _fetch)


async def _get_cached_recommendation(genre_key: str):
    async def _fetch():
        return await asyncio.wait_for(get_random_anime_by_genre(genre_key), timeout=20)

    return await _dedup_fetch(
        _GLOBAL_RECOMMEND_CACHE, _INFLIGHT_RECOMMEND, genre_key, RECOMMEND_CACHE_TTL, _fetch
    )


# ---------------------------------------------------------------------------
# Edição de mensagem (Telegram)
# ---------------------------------------------------------------------------

async def _safe_edit_text(query, text: str, reply_markup=None) -> bool:
    try:
        await query.edit_message_text(
            text=text, parse_mode="HTML", reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        return True
    except Exception as e:
        if "message is not modified" in str(e).lower():
            try:
                await query.edit_message_reply_markup(reply_markup=reply_markup)
            except Exception:
                pass
            return True
        return False


async def _safe_edit_caption(query, caption: str, reply_markup=None) -> bool:
    try:
        await query.edit_message_caption(
            caption=caption, parse_mode="HTML", reply_markup=reply_markup,
        )
        return True
    except Exception as e:
        if "message is not modified" in str(e).lower():
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
) -> bool:
    if not photo_url:
        return await _safe_edit_text(query, caption, reply_markup=reply_markup)

    # Quando a foto já está na mensagem, editar só a legenda é mais rápido
    # e evita o erro 400 de "media not modified" do Telegram
    if caption_only:
        return await _safe_edit_caption(query, caption, reply_markup=reply_markup)

    try:
        await query.edit_message_media(
            media=InputMediaPhoto(media=photo_url, caption=caption, parse_mode="HTML"),
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
        # Fallback: tenta editar só a legenda
        return await _safe_edit_caption(query, caption, reply_markup=reply_markup)


# ---------------------------------------------------------------------------
# Renderizadores de tela
# ---------------------------------------------------------------------------

async def _render_grouped_anime(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    item: dict,
    back_callback: str | None = None,
    caption_only: bool = False,
) -> bool:
    _remember_group_item(context, item)
    anime_id = item.get("default_anime_id") or item.get("id")
    anime = await _get_cached_anime(context, anime_id)
    text = _anime_text(anime)
    keyboard = _variant_keyboard(item, back_callback=back_callback)
    image_url = _anime_main_image(anime)

    if image_url:
        return await _safe_edit_photo(query, image_url, text, keyboard, caption_only=caption_only)
    return await _safe_edit_text(query, text, keyboard)


async def _render_episode_player(
    query,
    context,
    anime_id: str,
    episode: str,
    caption_only: bool = True,
) -> dict:
    # Paraleliza busca de anime e player para reduzir latência
    anime_task = asyncio.create_task(_get_cached_anime(context, anime_id))
    selected_quality = _get_selected_quality(context, anime_id, episode)
    player_task = asyncio.create_task(_get_cached_player(anime_id, episode, selected_quality))

    anime, player = await asyncio.gather(anime_task, player_task)

    detected_video = (player.get("video") or "").strip()
    server = player.get("server", "")
    total_episodes = player.get("total_episodes", 0)
    resolved_quality = _normalize_quality(player.get("quality", selected_quality))
    prev_episode = player.get("prev_episode")
    next_episode = player.get("next_episode")
    available_qualities = _available_quality_set(player)

    _set_selected_quality(context, anime_id, episode, resolved_quality)

    text = _player_text(anime.get("title", "Sem título"), episode, server, total_episodes, resolved_quality)
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
    if image_url:
        ok = await _safe_edit_photo(query, image_url, text, keyboard, caption_only=caption_only)
    else:
        ok = await _safe_edit_text(query, text, keyboard)

    if not ok:
        await _safe_answer_query(query, "⚠️ Não consegui atualizar essa mensagem. Abra novamente.", show_alert=False)

    return {
        "resolved_quality": resolved_quality,
        "available_qualities": available_qualities,
    }


# ---------------------------------------------------------------------------
# Controle de concorrência por callback
# ---------------------------------------------------------------------------

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
    prefixes = ("ep|", "eps|", "anime|", "sp|", "sa|", "ql|", "rec|", "var|", "vw|", "unvw|")
    for prefix in prefixes:
        if data.startswith(prefix):
            return data
    return data


# ---------------------------------------------------------------------------
# Cooldowns
# ---------------------------------------------------------------------------

_CALLBACK_LAST_TS: dict[int, float] = {}
_CALLBACK_LAST_DATA: dict[int, str] = {}


def _check_callback_cooldown(user_id: int, data: str) -> bool:
    """
    Retorna True se está em cooldown (deve rejeitar).
    Usa dicts globais (mais rápido que context.user_data para rate limiting).
    """
    now = _now()
    last_ts = _CALLBACK_LAST_TS.get(user_id, 0.0)
    last_data = _CALLBACK_LAST_DATA.get(user_id, "")

    if now - last_ts < CALLBACK_COOLDOWN and last_data == data:
        return True

    _CALLBACK_LAST_TS[user_id] = now
    _CALLBACK_LAST_DATA[user_id] = data
    return False


_QUALITY_SWITCH_LAST: dict[str, float] = {}


def _can_switch_quality_now(anime_id: str, episode: str) -> bool:
    key = f"{anime_id}:{episode}"
    now = _now()
    last = _QUALITY_SWITCH_LAST.get(key, 0.0)
    if now - last < QUALITY_COOLDOWN:
        return False
    _QUALITY_SWITCH_LAST[key] = now
    return True


# ---------------------------------------------------------------------------
# Handler principal de callbacks
# ---------------------------------------------------------------------------

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    if not query or not user:
        return

    # Fire-and-forget: não bloqueia o callback
    await _mark_user_seen_bg(user)

    data = query.data or ""
    print("CALLBACK DATA:", data)

    if data == "noop_loading":
        await _safe_answer_query(query, "⏳ Aguarde...", show_alert=False)
        return

    # Cooldown: verificação rápida sem lock
    if _check_callback_cooldown(user.id, data):
        await _safe_answer_query(query, "⚠️ Não aperte várias vezes seguidas.", show_alert=False)
        return

    message = query.message
    msg_lock = _message_lock(message.chat.id, message.message_id) if message else asyncio.Lock()

    # Verificação rápida de inflight ANTES de adquirir locks (evita round-trip desnecessário)
    if message:
        current_action = _action_signature(data)
        if _get_inflight_action(message.chat.id, message.message_id) == current_action:
            await _safe_answer_query(query, "⏳ Essa ação já está sendo processada...", show_alert=False)
            return

    # Responde imediatamente ao Telegram para evitar timeout da query
    await _safe_answer_query(query, "⏳ Carregando...", show_alert=False)

    # user_lock agora cobre apenas o bloco de escrita/registro de inflight,
    # não a execução completa do handler — reduz serialização por usuário
    user_lock = _user_lock(user.id)

    try:
        async with msg_lock:
            if message:
                current_action = _action_signature(data)
                if _get_inflight_action(message.chat.id, message.message_id) == current_action:
                    return
                _set_inflight_action(message.chat.id, message.message_id, current_action)

            # Despacha para o handler específico
            await _dispatch(query, context, user, data, message)

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


async def _dispatch(query, context, user, data: str, message):
    """
    Despacha o callback para o handler correto.
    Separado do `callbacks` para reduzir indentação e facilitar manutenção.
    """

    # --- qualidade ---
    if data.startswith("ql|"):
        await _handle_quality(query, context, user, data)
        return

    # --- marcar visto ---
    if data.startswith("vw|"):
        await _handle_mark_watched(query, context, user, data)
        return

    # --- desmarcar visto ---
    if data.startswith("unvw|"):
        await _handle_unmark_watched(query, context, user, data)
        return

    # --- watch link (legado) ---
    if data.startswith("watch|"):
        await _handle_watch_link(query, context, user, data)
        return

    # --- menu de recomendação ---
    if data == "rec|menu":
        await _handle_rec_menu(query)
        return

    # --- gênero ou nova tentativa de recomendação ---
    if data.startswith("rec|genre|") or data.startswith("rec|try|"):
        await _handle_rec_genre(query, data)
        return

    # --- paginação de busca ---
    if data.startswith("sp|"):
        await _handle_search_page(query, context, data)
        return

    # --- selecionar resultado de busca ---
    if data.startswith("sa|"):
        await _handle_search_select(query, context, user, data)
        return

    # --- voltar para anime ---
    if data.startswith("anime|"):
        await _handle_anime(query, context, user, data)
        return

    # --- variante (leg/dub) ---
    if data.startswith("var|"):
        await _handle_variant(query, context, user, data)
        return

    # --- lista de episódios ---
    if data.startswith("eps|"):
        await _handle_episodes(query, context, data)
        return

    # --- episódio específico ---
    if data.startswith("ep|"):
        await _handle_episode(query, context, user, data)
        return


# ---------------------------------------------------------------------------
# Handlers individuais
# ---------------------------------------------------------------------------

async def _handle_quality(query, context, user, data: str):
    parts = data.split("|", 3)
    if len(parts) != 4:
        return
    _, anime_id, episode, requested_quality = parts
    requested_quality = _normalize_quality(requested_quality)
    current_quality = _get_selected_quality(context, anime_id, episode)

    if not _can_switch_quality_now(anime_id, episode):
        await _safe_answer_query(query, "⏳ Aguarde um instante para trocar a qualidade.", show_alert=False)
        return

    if current_quality == requested_quality:
        return

    _set_selected_quality(context, anime_id, episode, requested_quality)
    result = await _render_episode_player(query, context, anime_id, episode, caption_only=True)

    if result["resolved_quality"] != requested_quality:
        await _safe_answer_query(
            query,
            f"⚠️ Esse episódio não tem {requested_quality} disponível.",
            show_alert=True,
        )


async def _handle_mark_watched(query, context, user, data: str):
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
    await _render_episode_player(query, context, anime_id, episode, caption_only=True)


async def _handle_unmark_watched(query, context, user, data: str):
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
    await _render_episode_player(query, context, anime_id, episode, caption_only=True)


async def _handle_watch_link(query, context, user, data: str):
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


async def _handle_rec_menu(query):
    text = _recommend_menu_text()
    keyboard = _recommend_menu_keyboard()
    has_photo = bool(getattr(query.message, "photo", None))
    if has_photo:
        ok = await _safe_edit_caption(query, text, keyboard)
    else:
        ok = await _safe_edit_text(query, text, keyboard)
    if not ok:
        await _safe_answer_query(query, "⚠️ Não consegui abrir o menu agora.", show_alert=False)


async def _handle_rec_genre(query, data: str):
    parts = data.split("|")
    if len(parts) < 3:
        return
    genre_key = parts[2]
    anime = await _get_cached_recommendation(genre_key)
    anime_id = anime.get("id", "")
    text = _recommend_text(anime, genre_key)
    keyboard = _recommend_result_keyboard(anime_id, genre_key)
    image_url = _anime_main_image(anime)

    if image_url:
        ok = await _safe_edit_photo(query, image_url, text, keyboard, caption_only=False)
    else:
        ok = await _safe_edit_text(query, text, keyboard)

    if not ok:
        await _safe_answer_query(query, "⚠️ Não consegui recomendar outro anime agora.", show_alert=False)


async def _handle_search_page(query, context, data: str):
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

    ok = await _safe_edit_photo(query, SEARCH_BANNER_URL, text, keyboard, caption_only=False)
    if not ok:
        await _safe_answer_query(query, "⚠️ Não consegui atualizar a página.", show_alert=False)


async def _handle_search_select(query, context, user, data: str):
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
        query, context, item,
        back_callback=f"sp|{token}|{current_page}",
        caption_only=False,
    )
    if not ok:
        await _safe_answer_query(query, "⚠️ Não consegui abrir esse anime.", show_alert=False)


async def _handle_anime(query, context, user, data: str):
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
        ok = await _render_grouped_anime(query, context, grouped_item, caption_only=False)
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
    if image_url:
        ok = await _safe_edit_photo(query, image_url, text, keyboard, caption_only=False)
    else:
        ok = await _safe_edit_text(query, text, keyboard)
    if not ok:
        await _safe_answer_query(query, "⚠️ Não consegui voltar para esse anime.", show_alert=False)


async def _handle_variant(query, context, user, data: str):
    anime_id = data.split("|", 1)[1]
    grouped_item = _get_group_item(context, anime_id)
    if grouped_item:
        _remember_group_item(context, grouped_item)

    # Paraleliza busca de anime e episódios
    anime_task = asyncio.create_task(_get_cached_anime(context, anime_id))
    episodes_task = asyncio.create_task(_get_cached_episodes(anime_id, 0, EPISODES_PER_PAGE))
    anime, payload = await asyncio.gather(anime_task, episodes_task)

    _safe_log_event(
        event_type="variant_open",
        user_id=user.id,
        username=user.username or user.first_name or "",
        anime_id=anime_id,
        anime_title=anime.get("title", "Sem título"),
    )

    items = payload.get("items", [])
    total = payload.get("total", 0)
    text = _episode_list_text(anime.get("title", "Sem título"), 0, total)
    keyboard = _episodes_keyboard(anime_id=anime_id, offset=0, items=items, total=total)
    image_url = _anime_secondary_image(anime)

    if image_url:
        ok = await _safe_edit_photo(query, image_url, text, keyboard, caption_only=False)
    else:
        ok = await _safe_edit_text(query, text, keyboard)
    if not ok:
        await _safe_answer_query(query, "⚠️ Não consegui abrir essa versão.", show_alert=False)


async def _handle_episodes(query, context, data: str):
    _, anime_id, offset_str = data.split("|", 2)
    offset = int(offset_str)

    # Paraleliza busca de anime e episódios
    anime_task = asyncio.create_task(_get_cached_anime(context, anime_id))
    episodes_task = asyncio.create_task(_get_cached_episodes(anime_id, offset, EPISODES_PER_PAGE))
    anime, payload = await asyncio.gather(anime_task, episodes_task)

    items = payload.get("items", [])
    total = payload.get("total", 0)
    text = _episode_list_text(anime.get("title", "Sem título"), offset, total)
    keyboard = _episodes_keyboard(anime_id=anime_id, offset=offset, items=items, total=total)
    image_url = _anime_secondary_image(anime)

    if image_url:
        ok = await _safe_edit_photo(query, image_url, text, keyboard, caption_only=False)
    else:
        ok = await _safe_edit_text(query, text, keyboard)
    if not ok:
        await _safe_answer_query(query, "⚠️ Não consegui abrir os episódios.", show_alert=False)


async def _handle_episode(query, context, user, data: str):
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

    await _render_episode_player(query, context, anime_id, episode, caption_only=False)
