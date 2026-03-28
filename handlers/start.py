import asyncio
import html
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
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

# URL base do teu miniapp
# Exemplo:
# MINIAPP_URL = "https://seudominio.com/miniapp"
# ou
# MINIAPP_URL = "https://SEU_IP:8000"
MINIAPP_URL = "https://jerusalem-editorials-screensavers-for.trycloudflare.com/miniapp/index.html"

START_COOLDOWN = 1.2
START_DEEP_LINK_TTL = 8.0

_START_USER_LOCKS = {}
_START_INFLIGHT = {}


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


def _variant_keyboard(group_item: dict) -> InlineKeyboardMarkup:
    rows = []

    base = MINIAPP_URL.rstrip("/")

    variants = group_item.get("variants") or []
    sub_variant = next((v for v in variants if not v.get("is_dubbed")), None)
    dub_variant = next((v for v in variants if v.get("is_dubbed")), None)

    if sub_variant:
        rows.append([
            InlineKeyboardButton(
                "🇯🇵 Legendado",
                web_app=WebAppInfo(
                    url=f"{base}/?anime={sub_variant['id']}"
                ),
            )
        ])

    if dub_variant:
        rows.append([
            InlineKeyboardButton(
                "🇧🇷 Dublado",
                web_app=WebAppInfo(
                    url=f"{base}/?anime={dub_variant['id']}"
                ),
            )
        ])

    if not rows:
        default_id = group_item.get("default_anime_id") or group_item.get("id")
        rows.append([
            InlineKeyboardButton(
                "📺 Ver episódios",
                web_app=WebAppInfo(
                    url=f"{base}/?anime={default_id}"
                ),
            )
        ])

    return InlineKeyboardMarkup(rows)

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
            return anime, item

        for variant in (item.get("variants") or []):
            if variant.get("id") == anime_id:
                return anime, item

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
                        f"🎬 <b>{html.escape(anime.get('title', 'Sem título'))}</b>\n\n"
                        f"🎞 <b>Episódio:</b> {html.escape(str(episode))}\n"
                        f"🎚 <b>Qualidade:</b> {html.escape(quality)}\n"
                        f"📚 <b>Total:</b> {total_episodes}\n\n"
                        f"Escolha uma opção abaixo para continuar."
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

                    cover = (
                        anime.get("media_image_url")
                        or anime.get("cover_url")
                        or anime.get("banner_url")
                        or None
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

            if arg.startswith("anime_"):
                anime_id = arg[len("anime_"):].strip()

                loading_msg = await message.reply_text(
                    "⏳ <b>Abrindo o anime para você...</b>",
                    parse_mode="HTML",
                )

                try:
                    anime, group_item = await _resolve_group_from_anime_id(anime_id)

                    cover = (
                        anime.get("media_image_url")
                        or anime.get("cover_url")
                        or anime.get("banner_url")
                        or None
                    )

                    variants = group_item.get("variants") or []
                    sub_count = 1 if any(not v.get("is_dubbed") for v in variants) else 0
                    dub_count = 1 if any(v.get("is_dubbed") for v in variants) else 0
                    available_versions = sub_count + dub_count

                    if available_versions <= 1:
                        default_id = group_item.get("default_anime_id") or anime_id

                        await _safe_delete_message(loading_msg)

                        text = _anime_text(anime)
                        keyboard = InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton(
                                    "📺 Ver episódios",
                                    callback_data=f"eps|{default_id}|0",
                                )
                            ]
                        ])

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

                    text = _anime_text(anime)
                    keyboard = _variant_keyboard(group_item)

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

            text = (
                f"🎬 <b>Bem-vindo ao {BOT_BRAND}!</b>\n\n"
                "Aqui você pode encontrar animes de forma rápida, direto no Telegram.\n\n"
                "✨ <b>O que você pode fazer aqui:</b>\n\n"
                "• 🔎 Buscar qualquer anime\n"
                "• 📺 Navegar pelos episódios\n"
                "• ✅ Marcar episódios como vistos\n"
                "• ⚡ Assistir rápido e sem complicação\n\n"
                "Use <code>/buscar</code> para começar."
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
