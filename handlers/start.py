import asyncio
import html
import time
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import BOT_BRAND, BOT_USERNAME
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


BANNER_URL = "https://photo.chelpbot.me/AgACAgEAAxkBZ987imm1UGdjCzV5n7FN2F6Ayew0umj2AAJkC2sbJAWhRWilm7WSjeD5AQADAgADeQADOgQ/photo.jpg"

START_COOLDOWN = 1.2
START_DEEP_LINK_TTL = 8.0

_START_USER_LOCKS = {}
_START_INFLIGHT = {}


# 🔥 REMOVE " - Episódio X" DO TÍTULO
def _clean_player_title(title: str) -> str:
    title = (title or "").strip()

    title = re.sub(r"\s*[-–—]?\s*epis[oó]dio\s*\d+\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*[-–—]?\s*episode\s*\d+\s*$", "", title, flags=re.IGNORECASE)

    return title.strip(" -–—")


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
        qualities.add(_normalize_quality(str(q)))

    videos = player.get("videos") or {}
    if isinstance(videos, dict):
        for q in videos.keys():
            qualities.add(_normalize_quality(str(q)))

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

    if "HD" not in available_qualities:
        hd_label += " 🚫"
    if "SD" not in available_qualities:
        sd_label += " 🚫"

    if selected_quality == "HD":
        hd_label += " 🔘"
    else:
        sd_label += " 🔘"

    watched = is_episode_watched(user_id, anime_id, episode)

    watch_btn = InlineKeyboardButton(
        "❌ Desmarcar como visto" if watched else "✅ Marcar como visto",
        callback_data=f"unvw|{anime_id}|{episode}" if watched else f"vw|{anime_id}|{episode}",
    )

    rows = [
        [InlineKeyboardButton("▶️ Assistir", url=detected_video or "https://t.me")],
        [watch_btn],
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

    rows.append([
        InlineKeyboardButton("📋 Lista de episódios", callback_data=f"eps|{anime_id}|0")
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


async def _safe_delete_message(msg):
    try:
        await msg.delete()
    except Exception:
        pass


async def start(update, context):
    init_referral_db()

    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    upsert_user(user.id, user.username, user.first_name)
    register_interaction(user.id)
    register_user(user.id)

    arg = (context.args[0] or "").strip() if context.args else ""

    is_member = await ensure_channel_membership(update, context)
    if not is_member:
        return

    user_lock = _safe_user_lock(user.id)

    async with user_lock:
        if arg:
            _set_inflight(user.id, arg)

        try:
            # 🔥 ABRIR EPISÓDIO DIRETO
            if arg.startswith("ep_") and "__" in arg:
                raw = arg[3:]
                anime_id, episode = raw.rsplit("__", 1)

                loading = await message.reply_text("⏳ Abrindo episódio...")

                try:
                    anime = await get_anime_details(anime_id)
                    player = await get_episode_player(anime_id, episode, "HD")

                    clean_title = _clean_player_title(anime.get("title"))

                    text = (
                        f"🎬 <b>{html.escape(clean_title)}</b>\n\n"
                        f"🎞 Episódio: {episode}\n"
                        f"🎚 Qualidade: {_normalize_quality(player.get('quality'))}\n"
                        f"📚 Total: {player.get('total_episodes')}\n\n"
                        f"Escolha uma opção abaixo para continuar."
                    )

                    keyboard = _player_keyboard(
                        anime_id,
                        episode,
                        player.get("video"),
                        player.get("prev_episode"),
                        player.get("next_episode"),
                        player.get("quality"),
                        user.id,
                        _available_quality_set(player),
                    )

                    await _safe_delete_message(loading)

                    await message.reply_photo(
                        photo=anime.get("media_image_url"),
                        caption=text,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )

                    return

                except Exception as e:
                    await _safe_delete_message(loading)
                    await message.reply_text("❌ Erro ao abrir episódio.")
                    print(e)
                    return

            # 🔥 START NORMAL
            text = (
                "🎌 Um lugar para encontrar e assistir animes direto no Telegram.\n\n"
                "🔎 Busque qualquer anime\n"
                "📺 Assista sem complicação\n"
                "✅ Marque episódios como vistos\n\n"
                "Digite /buscar para começar."
            )

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔎 Buscar anime", switch_inline_query_current_chat="")],
                [InlineKeyboardButton("➕ Adicionar ao grupo", url=f"https://t.me/{BOT_USERNAME}?startgroup=true")]
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
