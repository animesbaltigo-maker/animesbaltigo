import html
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.ext import ContextTypes

from services.animefire_client import (
    get_anime_details,
    get_episodes,
    get_episode_player,
)

EPISODES_PER_PAGE = 15
SEARCH_RESULTS_PER_PAGE = 8


def _strip_html(text: str):
    return re.sub(r"<[^>]+>", "", text or "")


def _truncate_text(text: str, limit: int):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _anime_text(title: str, description: str):
    safe_title = html.escape((title or "Sem título").strip())
    description = _strip_html(description)
    description = _truncate_text(description, 850)
    description = html.escape(description)

    return (
        f"🎬 <b>{safe_title}</b>\n"
        f"╭──────────────╮\n"
        f"📖 <b>Sinopse</b>\n"
        f"{description}\n"
        f"╰──────────────╯"
    )


def _episode_list_text(title: str, offset: int, total: int):
    safe_title = html.escape((title or "Sem título").strip())
    current_page = (offset // EPISODES_PER_PAGE) + 1
    total_pages = max(1, ((total - 1) // EPISODES_PER_PAGE) + 1)

    return (
        f"📺 <b>{safe_title}</b>\n"
        f"╭──────────────╮\n"
        f"🎞 <b>Episódios:</b> {total}\n"
        f"📄 <b>Página:</b> {current_page}/{total_pages}\n"
        f"╰──────────────╯\n\n"
        f"Escolha um episódio:"
    )


def _player_text(title: str, episode: str, server: str, total_episodes: int, quality: str):
    safe_title = html.escape((title or "Sem título").strip())
    safe_ep = html.escape(str(episode))
    safe_server = html.escape(server.upper())
    safe_quality = html.escape(quality.upper())

    return (
        f"▶️ <b>{safe_title}</b>\n"
        f"╭──────────────╮\n"
        f"🎞 <b>Episódio:</b> {safe_ep}\n"
        f"🛰 <b>Servidor detectado:</b> {safe_server}\n"
        f"🎚 <b>Qualidade:</b> {safe_quality}\n"
        f"📚 <b>Total:</b> {total_episodes}\n"
        f"╰──────────────╯"
    )


def _search_text(query: str, page: int, total: int):
    total_pages = max(1, ((total - 1) // SEARCH_RESULTS_PER_PAGE) + 1)
    safe_query = html.escape((query or "").strip())

    return (
        f"🔎 <b>Busca de animes</b>\n"
        f"━━━━━━━━━━━━━━\n\n"
        f"🎬 <b>Pesquisa:</b> {safe_query}\n"
        f"📄 <b>Página:</b> {page}/{total_pages}\n"
        f"📚 <b>Resultados:</b> {total}\n\n"
        f"Toque em um anime para abrir."
    )


def _search_keyboard(results: list, page: int, total: int, token: str):
    rows = []

    start = (page - 1) * SEARCH_RESULTS_PER_PAGE
    end = start + SEARCH_RESULTS_PER_PAGE
    page_items = results[start:end]

    for idx, item in enumerate(page_items, start=start + 1):
        title = (item.get("title") or "Sem título").strip()

        if len(title) > 42:
            title = title[:39].rstrip() + "..."

        rows.append([
            InlineKeyboardButton(
                f"{idx}. {title}",
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

    nav = []
    if offset > 0:
        prev_offset = max(0, offset - EPISODES_PER_PAGE)
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"eps|{anime_id}|{prev_offset}"))

    if offset + EPISODES_PER_PAGE < total:
        next_offset = offset + EPISODES_PER_PAGE
        nav.append(InlineKeyboardButton("➡️", callback_data=f"eps|{anime_id}|{next_offset}"))

    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton("🔙 Voltar", callback_data=f"anime|{anime_id}")
    ])

    return InlineKeyboardMarkup(rows)


def _player_keyboard(
    anime_id: str,
    detected_video: str,
    prev_episode,
    next_episode,
):
    rows = [
        [InlineKeyboardButton("▶ Assistir", url=detected_video)]
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
        InlineKeyboardButton("📚 Lista de episódios", callback_data=f"eps|{anime_id}|0")
    ])

    return InlineKeyboardMarkup(rows)


async def _safe_edit_text(query, text: str, reply_markup=None):
    try:
        await query.edit_message_text(
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception:
        await query.message.reply_text(
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )


async def _safe_edit_photo(query, photo_url: str, caption: str, reply_markup=None):
    try:
        await query.edit_message_media(
            media=InputMediaPhoto(
                media=photo_url,
                caption=caption,
                parse_mode="HTML",
            ),
            reply_markup=reply_markup,
        )
    except Exception:
        try:
            await query.message.reply_photo(
                photo=photo_url,
                caption=caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except Exception:
            await _safe_edit_text(query, caption, reply_markup=reply_markup)


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""

    if data.startswith("sp|"):
        _, token, page_str = data.split("|", 2)
        page = int(page_str)

        session = context.user_data.get(f"search_session:{token}")
        if not session:
            await query.answer("A busca expirou. Faça outra.", show_alert=True)
            return

        raw_query = session["query"]
        results = session["results"]
        total = len(results)

        text = _search_text(raw_query, page, total)
        keyboard = _search_keyboard(results, page, total, token)

        first_idx = (page - 1) * SEARCH_RESULTS_PER_PAGE
        cover_url = ""
        if 0 <= first_idx < len(results):
            try:
                anime = await get_anime_details(results[first_idx]["id"])
                cover_url = anime.get("cover_url") or ""
            except Exception:
                cover_url = ""

        if cover_url:
            await _safe_edit_photo(query, cover_url, text, keyboard)
        else:
            await _safe_edit_text(query, text, keyboard)
        return

    if data.startswith("sa|"):
        _, token, idx_str = data.split("|", 2)
        idx = int(idx_str)

        session = context.user_data.get(f"search_session:{token}")
        if not session:
            await query.answer("A busca expirou. Faça outra.", show_alert=True)
            return

        results = session["results"]
        if idx < 0 or idx >= len(results):
            await query.answer("Resultado inválido.", show_alert=True)
            return

        anime_id = results[idx]["id"]
        anime = await get_anime_details(anime_id)

        text = _anime_text(
            anime.get("title", "Sem título"),
            anime.get("description", "Sem descrição"),
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📺 Ver episódios", callback_data=f"eps|{anime_id}|0")]
        ])

        cover_url = anime.get("cover_url") or ""
        if cover_url:
            await _safe_edit_photo(query, cover_url, text, keyboard)
        else:
            await _safe_edit_text(query, text, keyboard)
        return

    if data.startswith("anime|"):
        anime_id = data.split("|", 1)[1]

        anime = await get_anime_details(anime_id)

        text = _anime_text(
            anime.get("title", "Sem título"),
            anime.get("description", "Sem descrição"),
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📺 Ver episódios", callback_data=f"eps|{anime_id}|0")]
        ])

        cover_url = anime.get("cover_url") or ""
        if cover_url:
            await _safe_edit_photo(query, cover_url, text, keyboard)
        else:
            await _safe_edit_text(query, text, keyboard)
        return

    if data.startswith("eps|"):
        _, anime_id, offset_str = data.split("|", 2)
        offset = int(offset_str)

        anime = await get_anime_details(anime_id)
        payload = await get_episodes(anime_id, offset, EPISODES_PER_PAGE)

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

        cover_url = anime.get("cover_url") or ""
        if cover_url:
            await _safe_edit_photo(query, cover_url, text, keyboard)
        else:
            await _safe_edit_text(query, text, keyboard)
        return

    if data.startswith("ep|"):
        _, anime_id, episode = data.split("|", 2)

        anime = await get_anime_details(anime_id)
        player = await get_episode_player(anime_id, episode)

        detected_video = player["video"]
        server = player["server"]
        total_episodes = player["total_episodes"]
        quality = player.get("quality", "hd")
        prev_episode = player["prev_episode"]
        next_episode = player["next_episode"]

        text = _player_text(
            anime.get("title", "Sem título"),
            episode,
            server,
            total_episodes,
            quality,
        )

        keyboard = _player_keyboard(
            anime_id=anime_id,
            detected_video=detected_video,
            prev_episode=prev_episode,
            next_episode=next_episode,
        )

        cover_url = anime.get("cover_url") or ""
        if cover_url:
            await _safe_edit_photo(query, cover_url, text, keyboard)
        else:
            await _safe_edit_text(query, text, keyboard)
        return
