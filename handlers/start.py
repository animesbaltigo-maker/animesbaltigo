import asyncio
import html
import time
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import BOT_BRAND, BOT_USERNAME
from services.animefire_client import get_anime_details, get_episode_player, search_anime
from services.metrics import is_episode_watched
from services.referral_db import (
    init_referral_db,
    register_interaction,
    upsert_user,
)
from services.user_registry import register_user
from utils.gatekeeper import ensure_channel_membership


BANNER_URL = "https://photo.chelpbot.me/AgACAgEAAxkBZ987imm1UGdjCzV5n7FN2F6Ayew0umj2AAJkC2sbJAWhRWilm7WSjeD5AQADAgADeQADOgQ/photo.jpg"

START_DEEP_LINK_TTL = 8.0

_START_USER_LOCKS = {}
_START_INFLIGHT = {}

EMOJI_MARK_WATCHED = "5427009714745517609"
EMOJI_UNMARK_WATCHED = "5465665476971471368"
EMOJI_SELECTED = "4970142833605345805"

EMOJI_PLAY = "5802968169467350939"
EMOJI_LIST = "5249231689695115145"
EMOJI_LEFT = "5258236805890710909"
EMOJI_RIGHT = "5260450573768990626"
EMOJI_TV = "4986030703612264985"
EMOJI_PLUS = "5393194986252542669"
EMOJI_PIRATE = "6001409398142930455"
EMOJI_BLOCK = "5240241223632954241"
EMOJI_LUPA = "5309965701241379366"


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

    return {q for q in qualities if q}


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

    hd_icon = EMOJI_SELECTED if selected_quality == "HD" else None
    sd_icon = EMOJI_SELECTED if selected_quality == "SD" else None

    if available_qualities:
        if "HD" not in available_qualities:
            hd_icon = EMOJI_BLOCK
        if "SD" not in available_qualities:
            sd_icon = EMOJI_BLOCK

    watched = is_episode_watched(user_id, anime_id, episode)

    watch_btn = InlineKeyboardButton(
        text="Desmarcar como visto" if watched else "Marcar como visto",
        callback_data=f"unvw|{anime_id}|{episode}" if watched else f"vw|{anime_id}|{episode}",
        icon_custom_emoji_id=EMOJI_UNMARK_WATCHED if watched else EMOJI_MARK_WATCHED,
    )

    rows = [
        [
            InlineKeyboardButton(
                text="Assistir",
                url=detected_video or "https://t.me",
                icon_custom_emoji_id=EMOJI_PLAY,
            )
        ],
        [watch_btn],
        [
            InlineKeyboardButton(
                text="HD",
                callback_data=f"ql|{anime_id}|{episode}|HD",
                icon_custom_emoji_id=hd_icon,
            ),
            InlineKeyboardButton(
                text="SD",
                callback_data=f"ql|{anime_id}|{episode}|SD",
                icon_custom_emoji_id=sd_icon,
            ),
        ],
    ]

    nav = []
    if prev_episode:
        nav.append(
            InlineKeyboardButton(
                text="Anterior",
                callback_data=f"ep|{anime_id}|{prev_episode}",
                icon_custom_emoji_id=EMOJI_LEFT,
            )
        )
    if next_episode:
        nav.append(
            InlineKeyboardButton(
                text="Próximo",
                callback_data=f"ep|{anime_id}|{next_episode}",
                icon_custom_emoji_id=EMOJI_RIGHT,
            )
        )

    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton(
            text="Lista de episódios",
            callback_data=f"eps|{anime_id}|0",
            icon_custom_emoji_id=EMOJI_LIST,
        )
    ])

    return InlineKeyboardMarkup(rows)


def _anime_text(anime: dict) -> str:
    title = html.escape((anime.get("title") or "Sem título").strip()).upper()
    description = (anime.get("description") or "Sem descrição disponível.").strip()

    if len(description) > 280:
        description = description[:277].rstrip() + "..."

    description = html.escape(description)

    score = anime.get("score")
    status = anime.get("status")
    genres = anime.get("genres") or []
    episodes = anime.get("episodes")
    season_year = anime.get("season_year")

    info_lines = []

    if score:
        info_lines.append(f"⭐ <b>Pontuação:</b> <code>{html.escape(str(score))}</code>")
    if status:
        info_lines.append(f"📡 <b>Situação:</b> <code>{html.escape(str(status))}</code>")
    if season_year:
        info_lines.append(f"📅 <b>Lançamento:</b> <code>{html.escape(str(season_year))}</code>")
    if episodes:
        info_lines.append(f"📚 <b>Episódios:</b> <code>{html.escape(str(episodes))}</code>")

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


def _variant_keyboard(group_item: dict) -> InlineKeyboardMarkup:
    rows = []

    variants = group_item.get("variants") or []
    sub_variant = next((v for v in variants if not v.get("is_dubbed")), None)
    dub_variant = next((v for v in variants if v.get("is_dubbed")), None)

    if sub_variant:
        rows.append([
            InlineKeyboardButton(
                text="🇯🇵 Legendado",
                callback_data=f"var|{sub_variant['id']}",
            )
        ])

    if dub_variant:
        rows.append([
            InlineKeyboardButton(
                text="🇧🇷 Dublado",
                callback_data=f"var|{dub_variant['id']}",
            )
        ])

    if not rows:
        default_id = group_item.get("default_anime_id") or group_item.get("id")
        rows.append([
            InlineKeyboardButton(
                text="Ver episódios",
                callback_data=f"eps|{default_id}|0",
                icon_custom_emoji_id=EMOJI_TV,
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


def _set_inflight(user_id: int, payload: str):
    _START_INFLIGHT[_deep_link_key(user_id, payload)] = _now()


def _clear_inflight(user_id: int, payload: str):
    _START_INFLIGHT.pop(_deep_link_key(user_id, payload), None)


async def _safe_delete_message(msg):
    try:
        if msg:
            await msg.delete()
    except Exception:
        pass


async def start(update, context: ContextTypes.DEFAULT_TYPE):
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
            if arg.startswith("ep_") and "__" in arg:
                raw = arg[3:]
                anime_id, episode = raw.rsplit("__", 1)

                loading_msg = await message.reply_text(
                    "⏳ <b>Abrindo o episódio para você...</b>",
                    parse_mode="HTML",
                )

                try:
                    anime = await get_anime_details(anime_id)
                    player = await get_episode_player(anime_id, episode, "HD")

                    detected_quality = _normalize_quality(player.get("quality", "HD"))
                    total_episodes = player.get("total_episodes", 0)

                    text = (
                            f"🎬 <b>{html.escape(_clean_player_title(anime.get('title', 'Sem título')))}</b>\n\n"
                            f"🎞 <b>Episódio:</b> {html.escape(str(episode))}\n"
                            f"🎚 <b>Qualidade:</b> {html.escape(str(detected_quality))}\n"
                            f"📚 <b>Total:</b> {html.escape(str(total_episodes))}\n\n"
                            f"Escolha uma opção abaixo para continuar."
                    )

                    keyboard = _player_keyboard(
                        anime_id=anime_id,
                        episode=episode,
                        detected_video=player.get("video"),
                        prev_episode=player.get("prev_episode"),
                        next_episode=player.get("next_episode"),
                        selected_quality=player.get("quality", "HD"),
                        user_id=user.id,
                        available_qualities=_available_quality_set(player),
                    )

                    await _safe_delete_message(loading_msg)

                    photo = (
                        anime.get("media_image_url")
                        or anime.get("cover_url")
                        or anime.get("banner_url")
                    )

                    if photo:
                        await message.reply_photo(
                            photo=photo,
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

                except Exception as e:
                    await _safe_delete_message(loading_msg)
                    await message.reply_text("❌ Erro ao abrir episódio.")
                    print(f"[START][EP] {e}")
                    return

            if arg.startswith("anime_"):
                anime_id = arg[6:]

                loading_msg = await message.reply_text(
                    "⏳ <b>Abrindo o anime para você...</b>",
                    parse_mode="HTML",
                )

                try:
                    anime = await get_anime_details(anime_id)
                    results = await search_anime(anime.get("title") or anime_id)

                    group_item = None
                    for item in results:
                        if (item.get("default_anime_id") or item.get("id")) == anime_id:
                            group_item = item
                            break

                        for variant in (item.get("variants") or []):
                            if variant.get("id") == anime_id:
                                group_item = item
                                break

                        if group_item:
                            break

                    if not group_item:
                        group_item = {
                            "id": anime_id,
                            "default_anime_id": anime_id,
                            "title": anime.get("title") or anime_id,
                            "variants": [{
                                "id": anime_id,
                                "title": anime.get("title") or anime_id,
                                "is_dubbed": False,
                            }],
                        }

                    text = _anime_text(anime)
                    keyboard = _variant_keyboard(group_item)

                    await _safe_delete_message(loading_msg)

                    photo = (
                        anime.get("media_image_url")
                        or anime.get("cover_url")
                        or anime.get("banner_url")
                    )

                    if photo:
                        await message.reply_photo(
                            photo=photo,
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

                except Exception as e:
                    await _safe_delete_message(loading_msg)
                    await message.reply_text("❌ Erro ao abrir anime.")
                    print(f"[START][ANIME] {e}")
                    return

            text = (
                f"🎬 <b>Bem-vindo ao {BOT_BRAND}!</b>\n\n"
                "Aqui você pode encontrar animes de forma rápida, direto no Telegram.\n\n"
                "✨ <b>O que você pode fazer aqui:</b>\n\n"
                "• 🔎 Buscar qualquer anime\n"
                "• 📺 Navegar pelos episódios\n"
                "• ⚡ Assistir rápido e sem complicação\n\n"
                "Use <code>/buscar nome</code> para começar."
            )

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        text="Adicionar ao grupo",
                        url=f"https://t.me/{BOT_USERNAME}?startgroup=true",
                        icon_custom_emoji_id=EMOJI_PLUS,
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="QG Baltigo",
                        url="https://t.me/QG_BALTIGO",
                        icon_custom_emoji_id=EMOJI_PIRATE,
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Buscar anime",
                        switch_inline_query_current_chat="",
                        icon_custom_emoji_id=EMOJI_LUPA,
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