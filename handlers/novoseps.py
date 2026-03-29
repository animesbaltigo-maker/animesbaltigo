import asyncio
import html
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import ADMIN_IDS, BOT_USERNAME, DATA_DIR
from services.animefire_client import get_anime_details
from services.recent_episodes_client import get_recent_episodes


CANAL_ATUALIZACOES = "@AtualizacoesOn"
POSTED_JSON_PATH = str(Path(DATA_DIR) / "episodios_postados.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


INVALID_TITLES = {
    "",
    "sem título",
    "sem titulo",
    "n/a",
    "na",
    "none",
    "null",
    "-",
    "|",
    "undefined",
}


def _is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


def _ensure_parent_dir(filepath: str) -> None:
    parent = os.path.dirname(filepath)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _load_json_list(filepath: str) -> list[str]:
    if not os.path.exists(filepath):
        return []

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return [str(x) for x in data]
    except Exception as e:
        logging.warning("Falha ao ler JSON %s: %r", filepath, e)

    return []


def _save_json(filepath: str, data: Any) -> None:
    _ensure_parent_dir(filepath)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    text = text.replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _sanitize_title(text: Any) -> str:
    text = _clean_text(text)

    text = re.sub(
        r"\s*-\s*[Ee]pis[oó]dio\s+\d+\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\s*[Ee]pis[oó]dio\s+\d+\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*\|\s*$", "", text).strip()
    text = re.sub(r"^\|\s*", "", text).strip()

    return text


def _is_invalid_title(text: Any) -> bool:
    normalized = _sanitize_title(text).strip().lower()
    return normalized in INVALID_TITLES


def _pick_main_title(anime: dict) -> str:
    candidates = [
        anime.get("title_romaji"),
        anime.get("title"),
        anime.get("title_english"),
        anime.get("title_native"),
    ]

    for candidate in candidates:
        cleaned = _sanitize_title(candidate)
        if not _is_invalid_title(cleaned):
            return cleaned

    return "Anime"


def _pick_second_title(anime: dict) -> str:
    main = _pick_main_title(anime).strip().lower()

    candidates = [
        anime.get("title_english"),
        anime.get("title_native"),
        anime.get("title"),
        anime.get("title_romaji"),
    ]

    for candidate in candidates:
        cleaned = _sanitize_title(candidate)
        if _is_invalid_title(cleaned):
            continue
        if cleaned.strip().lower() == main:
            continue
        return cleaned

    return ""


def _infer_season_number(anime: dict) -> str:
    candidates = [
        anime.get("title"),
        anime.get("title_romaji"),
        anime.get("title_english"),
        anime.get("title_native"),
        anime.get("id"),
    ]

    patterns = [
        r"\b(?:season|temporada|part|parte)\s*(\d+)\b",
        r"\b(\d+)(?:st|nd|rd|th)\s+season\b",
    ]

    for raw in candidates:
        text = _clean_text(raw).lower()
        if not text:
            continue

        for pattern in patterns:
            m = re.search(pattern, text)
            if m:
                return m.group(1)

    return "1"


def _normalize_genres(genres: list[Any]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()

    for genre in genres or []:
        text = _clean_text(genre).lstrip("#").strip()
        if not text:
            continue
        if text in {",", ".", "-", "|"}:
            continue

        key = text.lower()
        if key in seen:
            continue

        seen.add(key)
        cleaned.append(text)

    return cleaned


def _format_genres(genres: list[Any]) -> str:
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
    episode_text = html.escape(str(episode))

    return (
        f"🎬 <b>{full_title}</b>\n\n"
        f"» <b>Temporada:</b> [ <i>{season_number}</i> ]\n"
        f"» <b>Episódio:</b> [ <i>{episode_text}</i> ]\n"
        f"» <b>Gênero(s):</b> <i>{genres_text}</i>\n\n"
        f"» <b>@AtualizacoesOn</b>"
    )


def _build_episode_deep_link(anime_id: str, episode: str) -> str:
    anime_id = str(anime_id).strip()
    episode = str(episode).strip()
    return f"https://t.me/{BOT_USERNAME}?start=ep_{anime_id}__{episode}"


def _build_episode_keyboard(anime_id: str, episode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "▶️ Ver episódio",
                    url=_build_episode_deep_link(anime_id, episode),
                )
            ]
        ]
    )


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

        return True, _pick_main_title(anime)

    except Exception as e:
        logging.exception("Erro ao postar anime_id=%s ep=%s: %r", anime_id, episode, e)
        return False, str(anime_id)


async def _check_and_post_recent(
    context: ContextTypes.DEFAULT_TYPE,
    limit: int = 10,
    delay_seconds: float = 2.0,
) -> tuple[int, int]:
    posted_ids = set(_load_json_list(POSTED_JSON_PATH))
    items = await get_recent_episodes(limit=limit)

    queue = []
    for item in items:
        key = str(item.get("key", "")).strip()
        anime_id = str(item.get("anime_id", "")).strip()
        episode = str(item.get("episode", "")).strip()

        if not key or not anime_id or not episode:
            continue
        if key in posted_ids:
            continue

        queue.append(
            {
                "key": key,
                "anime_id": anime_id,
                "episode": episode,
            }
        )

    success_count = 0
    fail_count = 0

    for item in queue:
        ok, _ = await _post_one_episode(
            context=context,
            anime_id=item["anime_id"],
            episode=item["episode"],
        )

        if ok:
            posted_ids.add(item["key"])
            _save_json(POSTED_JSON_PATH, sorted(posted_ids))
            success_count += 1
        else:
            fail_count += 1

        await asyncio.sleep(delay_seconds)

    return success_count, fail_count


async def postnovoseps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    message = update.effective_message

    if not message:
        return

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
            context=context,
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
        logging.exception("ERRO POSTNOVOSEPS: %r", e)
        await msg.edit_text(
            "❌ <b>Não consegui postar os episódios novos.</b>",
            parse_mode="HTML",
        )


async def auto_post_new_eps_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.info("[AUTO_NOVOSEPS] iniciando checagem...")
    try:
        success_count, fail_count = await _check_and_post_recent(
            context=context,
            limit=12,
            delay_seconds=2.0,
        )
        logging.info(
            "[AUTO_NOVOSEPS] postados=%s falhas=%s",
            success_count,
            fail_count,
        )
    except Exception as e:
        logging.exception("[AUTO_NOVOSEPS] erro=%r", e)
