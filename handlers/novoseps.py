import asyncio
import html
import json
import os
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import ADMIN_IDS, BOT_USERNAME, DATA_DIR
from services.animefire_client import get_anime_details
from services.recent_episodes_client import get_recent_episodes


CANAL_ATUALIZACOES = "@AtualizacoesOn"
POSTED_JSON_PATH = str(DATA_DIR / "episodios_postados.json")


def _is_admin(user_id: int | None) -> bool:
    if user_id is None:
        return False
    return user_id in ADMIN_IDS


def _ensure_parent_dir(filepath: str):
    parent = os.path.dirname(filepath)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _load_json_list(filepath: str) -> list:
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_json(filepath: str, data):
    _ensure_parent_dir(filepath)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _sanitize_title(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s*-\s*[Ee]pis[oó]dio\s+\d+\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*[Ee]pis[oó]dio\s+\d+\s*$", "", text, flags=re.IGNORECASE)
    return text.strip() or "Sem título"


def _pick_main_title(anime: dict) -> str:
    title = anime.get("title_romaji") or anime.get("title") or "Sem título"
    return _sanitize_title(title)


def _pick_second_title(anime: dict) -> str:
    second = anime.get("title_english") or anime.get("title_native") or ""
    second = _sanitize_title(second)
    main = _pick_main_title(anime)

    if second and second.strip().lower() != main.strip().lower():
        return second
    return ""


def _infer_season_number(anime: dict) -> str:
    candidates = [
        anime.get("title") or "",
        anime.get("title_romaji") or "",
        anime.get("title_english") or "",
        anime.get("id") or "",
    ]

    patterns = [
        r"\b(?:season|temporada|part)\s*(\d+)\b",
        r"\b(\d+)(?:st|nd|rd|th)\s+season\b",
    ]

    for text in candidates:
        lower = text.lower()
        for pattern in patterns:
            m = re.search(pattern, lower)
            if m:
                return m.group(1)

    return "1"


def _normalize_genres(genres: list) -> list[str]:
    cleaned = []

    for g in genres or []:
        text = str(g or "").strip()
        if not text:
            continue

        text = text.replace("\n", " ").replace("\r", " ")
        text = re.sub(r"\s+", " ", text).strip()
        text = text.lstrip("#").strip()

        if not text or text in {",", ".", "-", "|"}:
            continue

        cleaned.append(text)

    unique = []
    seen = set()

    for g in cleaned:
        key = g.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(g)

    return unique


def _format_genres(genres: list) -> str:
    normalized = _normalize_genres(genres)
    if not normalized:
        return "N/A"

    return ", ".join(f"#{g}" for g in normalized[:4])


def _build_episode_caption(anime: dict, episode: str) -> str:
    title_1 = html.escape(_pick_main_title(anime))
    title_2 = html.escape(_pick_second_title(anime))
    full_title = f"{title_1} | {title_2}" if title_2 else title_1

    genres_text = html.escape(_format_genres(anime.get("genres") or []))
    season_number = html.escape(_infer_season_number(anime))
    episode = html.escape(str(episode))

    return (
        f"🎬 <b>{full_title}</b>\n\n"
        f"» <b>Temporada:</b> [ <i>{season_number}</i> ]\n"
        f"» <b>Episódio:</b> [ <i>{episode}</i> ]\n"
        f"» <b>Gênero(s):</b> <i>{genres_text}</i>\n\n"
        f"» <b>@AtualizacoesOn</b>"
    )


def _build_episode_deep_link(anime_id: str, episode: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=ep_{anime_id}__{episode}"


def _build_episode_keyboard(anime_id: str, episode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "▶️ Ver episódio",
                url=_build_episode_deep_link(anime_id, episode),
            )
        ]
    ])


async def _post_one_episode(
    context: ContextTypes.DEFAULT_TYPE,
    anime_id: str,
    episode: str,
) -> tuple[bool, str]:
    try:
        anime = await get_anime_details(anime_id)

        photo = (
            anime.get("media_image_url")
            or anime.get("cover_url")
            or anime.get("banner_url")
            or None
        )

        caption = _build_episode_caption(anime, episode)
        keyboard = _build_episode_keyboard(anime_id, episode)

        if photo:
            await context.bot.send_photo(
                chat_id=CANAL_ATUALIZACOES,
                photo=photo,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await context.bot.send_message(
                chat_id=CANAL_ATUALIZACOES,
                text=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

        return True, anime.get("title", anime_id)

    except Exception as e:
        print(f"[NOVOSEPS] erro ao postar {anime_id} ep {episode}: {repr(e)}")
        return False, anime_id


async def _check_and_post_recent(
    context: ContextTypes.DEFAULT_TYPE,
    limit: int = 10,
    delay_seconds: float = 2.0,
) -> tuple[int, int]:
    posted_ids = set(_load_json_list(POSTED_JSON_PATH))
    items = await get_recent_episodes(limit=limit)

    queue = [item for item in items if item["key"] not in posted_ids]

    success_count = 0
    fail_count = 0

    for item in queue:
        ok, _ = await _post_one_episode(context, item["anime_id"], item["episode"])

        if ok:
            posted_ids.add(item["key"])
            _save_json(POSTED_JSON_PATH, sorted(posted_ids))
            success_count += 1
        else:
            fail_count += 1

        await asyncio.sleep(delay_seconds)

    return success_count, fail_count


async def postnovoseps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    message = update.effective_message

    if not _is_admin(user_id):
        await message.reply_text(
            "❌ <b>Você não tem permissão para usar este comando.</b>",
            parse_mode="HTML",
        )
        return

    limit = 10
    if context.args:
        try:
            limit = max(1, min(30, int(context.args[0])))
        except Exception:
            limit = 10

    msg = await message.reply_text(
        "📡 <b>Buscando episódios novos...</b>",
        parse_mode="HTML",
    )

    try:
        success_count, fail_count = await _check_and_post_recent(
            context,
            limit=limit,
            delay_seconds=2.0,
        )

        await msg.edit_text(
            f"✅ <b>Checagem concluída.</b>\n\n"
            f"<b>Postados:</b> <code>{success_count}</code>\n"
            f"<b>Falhas:</b> <code>{fail_count}</code>",
            parse_mode="HTML",
        )

    except Exception as e:
        print("ERRO POSTNOVOSEPS:", repr(e))
        await msg.edit_text(
            "❌ <b>Não consegui postar os episódios novos.</b>",
            parse_mode="HTML",
        )


async def auto_post_new_eps_job(context: ContextTypes.DEFAULT_TYPE):
    print("[AUTO_NOVOSEPS] iniciando checagem...")
    try:
        success_count, fail_count = await _check_and_post_recent(
            context,
            limit=12,
            delay_seconds=2.0,
        )
        print(f"[AUTO_NOVOSEPS] postados={success_count} falhas={fail_count}")

    except Exception as e:
        print(f"[AUTO_NOVOSEPS] erro={repr(e)}")
