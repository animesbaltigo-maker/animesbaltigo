import asyncio
import html
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import RetryAfter, TelegramError
from telegram.ext import ContextTypes

from config import (
    ADMIN_IDS,
    BOT_USERNAME,
    CANAL_POSTAGEM,
    DATA_DIR,
    GROQ_API_KEY,
    HTTP_TIMEOUT,
    SOURCE_SITE_BASE,
    STICKER_DIVISOR,
)
from services.animefire_client import get_anime_details, search_anime


POSTED_ANIME_JSON_PATH = Path(DATA_DIR) / "animes_postados.json"
ANILIST_LINKS_JSON_PATH = Path(DATA_DIR) / "anilist_anime_links.json"
POSTALL_DELAY_SECONDS = 15
POSTALL_DEFAULT_LIMIT = 10
POSTALL_MAX_LIMIT = 50
POSTALL_MAX_PAGES = 43
POSTALL_LOCK = asyncio.Lock()

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS = ("llama-3.1-8b-instant", "llama-3.3-70b-versatile")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": SOURCE_SITE_BASE,
}

GENRE_NORMALIZATION = {
    "acao": "Ação",
    "ação": "Ação",
    "acao e aventura": "Ação e Aventura",
    "ação e aventura": "Ação e Aventura",
    "action": "Ação",
    "action adventure": "Ação e Aventura",
    "adventure": "Aventura",
    "animacao": "Animação",
    "animação": "Animação",
    "animazione": "Animação",
    "animation": "Animação",
    "comedia": "Comédia",
    "comédia": "Comédia",
    "commedia": "Comédia",
    "comedy": "Comédia",
    "drama": "Drama",
    "dramma": "Drama",
    "fantasia": "Fantasia",
    "fantasy": "Fantasia",
    "misterio": "Mistério",
    "mistério": "Mistério",
    "mystery": "Mistério",
    "romance": "Romance",
    "sci-fi & fantasia": "Sci-Fi e Fantasia",
    "sci-fi & fantasy": "Sci-Fi e Fantasia",
    "sci fi fantasia": "Sci-Fi e Fantasia",
    "sci fi fantasy": "Sci-Fi e Fantasia",
}


def _truncate_text(text: str, limit: int = 360) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _is_admin(user_id: int | None) -> bool:
    return user_id in ADMIN_IDS if user_id is not None else False


def _pick_main_title(anime: dict) -> str:
    return anime.get("title_romaji") or anime.get("title") or "Sem titulo"


def _pick_second_title(anime: dict) -> str:
    second = anime.get("title_english") or anime.get("title_native") or ""
    main = _pick_main_title(anime)

    if second and second.strip().lower() != main.strip().lower():
        return second
    return ""


def _format_status(status: str | None) -> str:
    return status or "N/A"


def _clean_description(description: str) -> str:
    description = re.sub(r"<[^>]+>", " ", description or "")
    description = html.unescape(description)
    description = re.sub(r"\s+", " ", description).strip()

    lowered = description.lower()
    blocked = (
        "este site nao hospeda nenhum video em seu servidor",
        "este site não hospeda nenhum vídeo em seu servidor",
        "todo conteudo e provido de terceiros nao afiliados",
        "todo conteúdo é provido de terceiros não afiliados",
        "sinopse:",
    )

    for bad in blocked:
        if lowered.startswith(bad):
            description = description[len(bad):].strip(" :-")
            break

    return description or "Sem sinopse disponivel."


def _normalize_genre(genre: str) -> str:
    genre = re.sub(r"\s+", " ", str(genre or "")).strip()
    key = genre.lower()
    key = key.replace("&", " e ")
    key = re.sub(r"\s+", " ", key).strip()
    return GENRE_NORMALIZATION.get(key) or GENRE_NORMALIZATION.get(genre.lower()) or genre


def _hashtag_genre(genre: str) -> str:
    genre = _normalize_genre(genre)
    parts = re.findall(r"[A-Za-zÀ-ÿ0-9]+", genre)
    if not parts:
        return ""
    return "#" + "".join(part[:1].upper() + part[1:] for part in parts)


def _slug_title(slug: str) -> str:
    slug = re.sub(r"-\d+$", "", slug or "")
    slug = slug.replace("-", " ").strip()
    return slug.title() if slug else "Anime"


def _clean_group_search_title(title: str) -> str:
    title = str(title or "")
    title = re.sub(r"\s+[–-]\s+todos\s+os\s+epis[oó]dios.*$", "", title, flags=re.I)
    title = re.sub(r"\s*\((legendado|dublado)\)\s*$", "", title, flags=re.I)
    title = re.sub(r"\s+(legendado|dublado)\s*$", "", title, flags=re.I)
    return re.sub(r"\s+", " ", title).strip()


def _normal_post_key(anime_id: str, title: str = "") -> str:
    raw = (anime_id or title or "").lower()
    raw = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    raw = raw.replace("-todos-os-episodios", "")
    raw = re.sub(r"-(legendado|dublado|dub|sub)$", "", raw)
    return raw or anime_id or title


def _load_posted_keys() -> set[str]:
    try:
        if not POSTED_ANIME_JSON_PATH.exists():
            return set()
        data = json.loads(POSTED_ANIME_JSON_PATH.read_text(encoding="utf-8"))
    except Exception:
        return set()

    if isinstance(data, list):
        return {str(item) for item in data if item}
    if isinstance(data, dict):
        values = data.get("posted") or data.get("animes") or []
        return {str(item) for item in values if item}
    return set()


def _save_posted_keys(keys: set[str]) -> None:
    POSTED_ANIME_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    POSTED_ANIME_JSON_PATH.write_text(
        json.dumps(sorted(keys), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_anilist_links() -> dict:
    try:
        if not ANILIST_LINKS_JSON_PATH.exists():
            return {}
        data = json.loads(ANILIST_LINKS_JSON_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_anilist_link(anime: dict, group_item: dict | None = None) -> None:
    anilist_id = anime.get("anilist_id")
    if not anilist_id:
        return

    group_item = group_item or {}
    anime_id = anime.get("id") or group_item.get("default_anime_id") or group_item.get("id")
    if not anime_id:
        return

    data = _load_anilist_links()
    data[str(anilist_id)] = {
        "id": anime_id,
        "default_anime_id": group_item.get("default_anime_id") or anime_id,
        "title": group_item.get("title") or anime.get("title") or "",
        "variants": group_item.get("variants") or [],
    }

    ANILIST_LINKS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    ANILIST_LINKS_JSON_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _post_deep_link_payload(anime: dict) -> str:
    anilist_id = anime.get("anilist_id")
    if anilist_id:
        return f"anime_al_{anilist_id}"
    return f"anime_{anime.get('id', '')}"


def _anilist_media_url(anime: dict) -> str:
    anilist_id = anime.get("anilist_id")
    if not anilist_id:
        return ""
    return f"http://img.anili.st/media/{anilist_id}"


def _photo_candidates(anime: dict) -> list[str]:
    candidates = [
        _anilist_media_url(anime),
        anime.get("banner_url") or "",
        anime.get("cover_url") or "",
        anime.get("media_image_url") or "",
    ]

    unique = []
    seen = set()
    for url in candidates:
        url = (url or "").strip()
        if url and url not in seen:
            unique.append(url)
            seen.add(url)
    return unique


def _genres_text(anime: dict) -> str:
    genres = anime.get("genres") or []
    if not genres:
        return "N/A"
    clean = []
    for genre in genres[:4]:
        tag = _hashtag_genre(str(genre))
        if tag:
            clean.append(tag)
    return ", ".join(clean) if clean else "N/A"


def _build_keyboard(anime: dict) -> InlineKeyboardMarkup:
    anilist_url = anime.get("anilist_url") or ""
    trailer_url = ""
    trailer_id = anime.get("trailer_id") or ""
    trailer_site = (anime.get("trailer_site") or "").lower()

    if trailer_site == "youtube" and trailer_id:
        trailer_url = f"https://www.youtube.com/watch?v={trailer_id}"

    rows = [
        [
            InlineKeyboardButton(
                "▶️ Assistir agora",
                url=f"https://t.me/{BOT_USERNAME}?start={_post_deep_link_payload(anime)}",
            )
        ]
    ]

    second_row = []
    if trailer_url:
        second_row.append(InlineKeyboardButton("🎬 Trailer", url=trailer_url))
    if anilist_url:
        second_row.append(InlineKeyboardButton("⭐ AniList", url=anilist_url))
    if second_row:
        rows.append(second_row)

    return InlineKeyboardMarkup(rows)


def _build_caption(anime: dict, ai_description: str) -> str:
    title_1 = _pick_main_title(anime)
    title_1 = re.sub(r"\s+[–-]\s+todos\s+os\s+epis[oó]dios.*$", "", title_1, flags=re.I)
    title_1 = re.sub(r"\s*\((legendado|dublado)\)\s*$", "", title_1, flags=re.I)
    title_1 = re.sub(r"\s+(legendado|dublado)\s*$", "", title_1, flags=re.I)
    title_1 = html.escape(title_1.strip() or _pick_main_title(anime))
    title_2 = html.escape(_pick_second_title(anime))
    full_title = f"{title_1} | {title_2}" if title_2 else title_1

    genres = html.escape(_genres_text(anime))
    status = html.escape(_format_status(anime.get("status")))
    year = html.escape(str(
        anime.get("year")
        or anime.get("release_year")
        or anime.get("season_year")
        or anime.get("seasonYear")
        or "N/A"
    ))
    description = html.escape(_truncate_text(ai_description, 430))

    return (
        f"<b>{full_title}</b>\n\n"
        f"<b>Gênero(s):</b> <i>{genres}</i>\n"
        f"<b>Status:</b> <i>{status}.</i>\n"
        f"<b>Ano:</b> <i>{year}.</i>\n\n"
        f"💬 <i>{description}</i>"
    )


def _fallback_description(anime: dict) -> str:
    title = _pick_main_title(anime)
    title = re.sub(r"\s+[–-]\s+todos\s+os\s+epis[oó]dios.*$", "", title, flags=re.I)
    genres = [_normalize_genre(genre) for genre in (anime.get("genres") or []) if str(genre).strip()]
    genre_hint = ", ".join(genres[:2]).lower() if genres else "anime"
    status = (anime.get("status") or "").lower()
    status_hint = "com uma historia completa" if "final" in status else "com novos conflitos e descobertas"

    return (
        f"{title} entrega uma jornada de {genre_hint}, {status_hint}, personagens marcantes "
        "e momentos que misturam emoção, tensão e aventura na medida certa. Uma boa escolha "
        "para quem quer começar um anime direto, sem complicação e com aquela vontade de ver "
        "só mais um episódio."
    )


async def _generate_ai_description(anime: dict) -> str:
    fallback = _fallback_description(anime)
    if not GROQ_API_KEY:
        return fallback

    source_description = _clean_description(anime.get("description") or "")
    payload_base = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "Voce escreve descricoes curtas de animes para posts de Telegram. "
                    "Crie um paragrafo natural em portugues do Brasil, sem copiar literalmente "
                    "a sinopse, sem spoilers grandes, sem markdown, sem emojis e com 45 a 70 palavras."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Titulo: {_pick_main_title(anime)}\n"
                    f"Titulo alternativo: {_pick_second_title(anime)}\n"
                    f"Generos: {', '.join(anime.get('genres') or [])}\n"
                    f"Ano: {anime.get('year') or anime.get('release_year') or ''}\n"
                    f"Status: {anime.get('status') or ''}\n"
                    f"Sinopse de referencia: {source_description}\n\n"
                    "Gere apenas a descricao final."
                ),
            },
        ],
        "temperature": 0.55,
        "max_tokens": 150,
    }

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        for model in GROQ_MODELS:
            try:
                response = await client.post(
                    GROQ_API_URL,
                    headers=headers,
                    json={**payload_base, "model": model},
                )
                response.raise_for_status()
                data = response.json()
                content = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                content = _clean_description(content)
                content = content.strip('"“” ')
                if len(content) >= 40:
                    return _truncate_text(content, 430)
            except Exception:
                continue

    return fallback


async def _send_with_retry(coro_factory, retries: int = 2):
    for attempt in range(retries + 1):
        try:
            return await coro_factory()
        except RetryAfter as exc:
            await asyncio.sleep(int(exc.retry_after) + 1)
        except TelegramError:
            if attempt >= retries:
                raise
            await asyncio.sleep(2 + attempt)


async def _send_anime_post(context: ContextTypes.DEFAULT_TYPE, anime: dict) -> None:
    description = await _generate_ai_description(anime)
    caption = _build_caption(anime, description)
    keyboard = _build_keyboard(anime)

    last_error = None
    for photo in _photo_candidates(anime):
        try:
            await _send_with_retry(
                lambda photo=photo: context.bot.send_photo(
                    chat_id=CANAL_POSTAGEM,
                    photo=photo,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            )
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            continue

    if last_error is not None or not _photo_candidates(anime):
        await _send_with_retry(
            lambda: context.bot.send_message(
                chat_id=CANAL_POSTAGEM,
                text=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        )

    await _send_with_retry(
        lambda: context.bot.send_sticker(
            chat_id=CANAL_POSTAGEM,
            sticker=STICKER_DIVISOR,
        )
    )


async def _post_one_anime(context: ContextTypes.DEFAULT_TYPE, anime_id: str) -> dict:
    anime = await get_anime_details(anime_id)
    group_item = await _resolve_group_for_post(anime_id, anime)
    default_id = group_item.get("default_anime_id") or group_item.get("id")
    if default_id and default_id != anime_id:
        try:
            anime = await get_anime_details(default_id)
            anime_id = default_id
        except Exception:
            pass
    _save_anilist_link(anime, group_item)
    await _send_anime_post(context, anime)
    return anime


async def _resolve_group_for_post(anime_id: str, anime: dict) -> dict:
    title = anime.get("title") or anime.get("title_romaji") or anime_id.replace("-", " ").title()
    search_title = _clean_group_search_title(title) or title
    try:
        results = await search_anime(search_title)
    except Exception:
        results = []

    for item in results:
        default_id = item.get("default_anime_id") or item.get("id")
        if default_id == anime_id:
            return item
        for variant in item.get("variants") or []:
            if variant.get("id") == anime_id:
                return item

    return {
        "id": anime_id,
        "default_anime_id": anime_id,
        "title": title,
        "variants": [{
            "id": anime_id,
            "title": title,
            "is_dubbed": bool(anime.get("is_dubbed")),
        }],
    }


def _archive_url(page: int) -> str:
    base = SOURCE_SITE_BASE.rstrip("/")
    return f"{base}/anime/page/{page}"


async def _request_archive_page(page: int) -> str:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        response = await client.get(_archive_url(page), headers=HEADERS)
        if response.status_code == 404:
            return ""
        response.raise_for_status()
        return response.text


def _extract_catalog_items(html_doc: str) -> list[dict]:
    soup = BeautifulSoup(html_doc or "", "html.parser")
    items = []
    seen = set()

    for anchor in soup.select("a[href*='/anime/']"):
        href = anchor.get("href") or ""
        href = urljoin(SOURCE_SITE_BASE, href)
        parsed = urlparse(href)
        parts = [part for part in parsed.path.split("/") if part]

        if len(parts) < 2 or parts[0] != "anime":
            continue

        anime_id = parts[1].strip()
        if not anime_id or anime_id in {"page", "categoria", "genero"}:
            continue
        if anime_id in seen:
            continue

        title = ""
        img = anchor.select_one("img")
        if img:
            title = img.get("alt") or img.get("title") or ""
        if not title:
            title_el = anchor.select_one("h1,h2,h3,h4,.title,.titulo,.nome")
            title = title_el.get_text(" ", strip=True) if title_el else ""
        if not title:
            title = anchor.get_text(" ", strip=True)
        title = re.sub(r"\s+", " ", title).strip() or _slug_title(anime_id)

        seen.add(anime_id)
        items.append({"id": anime_id, "title": title})

    return items


async def _collect_next_catalog_items(limit: int, posted_keys: set[str]) -> list[dict]:
    selected = []
    selected_keys = set()

    for page in range(1, POSTALL_MAX_PAGES + 1):
        html_doc = await _request_archive_page(page)
        if not html_doc:
            break

        items = _extract_catalog_items(html_doc)
        if not items:
            break

        for item in items:
            key = _normal_post_key(item["id"], item.get("title", ""))
            if key in posted_keys or key in selected_keys:
                continue
            selected.append(item)
            selected_keys.add(key)
            if len(selected) >= limit:
                return selected

    return selected


async def postanime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None

    if not _is_admin(user_id):
        await update.effective_message.reply_text(
            "❌ <b>Você não tem permissão para usar este comando.</b>",
            parse_mode="HTML",
        )
        return

    if not context.args:
        await update.effective_message.reply_text(
            "❌ <b>Faltou o nome do anime.</b>\n\n"
            "Use assim:\n"
            "<code>/postanime nome do anime</code>\n\n"
            "📌 <b>Exemplo:</b>\n"
            "<code>/postanime one piece</code>",
            parse_mode="HTML",
        )
        return

    query = " ".join(context.args).strip()
    msg = await update.effective_message.reply_text(
        "📤 <b>Montando postagem...</b>",
        parse_mode="HTML",
    )

    try:
        results = await search_anime(query)
        if not results:
            await msg.edit_text("❌ <b>Não encontrei esse anime.</b>", parse_mode="HTML")
            return

        anime_id = results[0]["id"]
        anime = await _post_one_anime(context, anime_id)

        await msg.edit_text(
            f"✅ <b>Anime postado no canal.</b>\n\n"
            f"<code>{html.escape(anime.get('title') or anime_id)}</code>",
            parse_mode="HTML",
        )

    except Exception as exc:
        print("ERRO POSTANIME:", repr(exc))
        await msg.edit_text(
            "❌ <b>Não consegui postar esse anime.</b>",
            parse_mode="HTML",
        )


async def _run_postall_batch(context: ContextTypes.DEFAULT_TYPE, msg, limit: int) -> None:
    posted_keys = _load_posted_keys()

    async with POSTALL_LOCK:
        queue = await _collect_next_catalog_items(limit, posted_keys)
        if not queue:
            await msg.edit_text(
                "✅ <b>Nenhum anime novo para postar agora.</b>",
                parse_mode="HTML",
            )
            return

        total = len(queue)
        posted_count = 0
        errors = 0

        for index, item in enumerate(queue, start=1):
            title_for_status = html.escape(item.get("title") or item["id"])
            await msg.edit_text(
                f"📤 <b>Postando {index}/{total}</b>\n"
                f"<code>{title_for_status}</code>",
                parse_mode="HTML",
            )

            try:
                anime = await _post_one_anime(context, item["id"])
                key = _normal_post_key(item["id"], anime.get("title") or item.get("title", ""))
                posted_keys.add(key)
                _save_posted_keys(posted_keys)
                posted_count += 1
            except Exception as exc:
                errors += 1
                print("ERRO POSTALL:", item["id"], repr(exc))

            if index < total:
                await asyncio.sleep(POSTALL_DELAY_SECONDS)

        await msg.edit_text(
            f"✅ <b>Postagem em lote finalizada.</b>\n\n"
            f"Postados: <b>{posted_count}</b>\n"
            f"Falhas: <b>{errors}</b>",
            parse_mode="HTML",
        )


async def postall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None

    if not _is_admin(user_id):
        await update.effective_message.reply_text(
            "❌ <b>Você não tem permissão para usar este comando.</b>",
            parse_mode="HTML",
        )
        return

    if POSTALL_LOCK.locked():
        await update.effective_message.reply_text(
            "⏳ <b>Já tem um postall rodando.</b>\n"
            "Quando ele terminar, você pode iniciar outro lote.",
            parse_mode="HTML",
        )
        return

    raw_limit = context.args[0] if context.args else str(POSTALL_DEFAULT_LIMIT)
    try:
        limit = int(raw_limit)
    except ValueError:
        limit = POSTALL_DEFAULT_LIMIT
    limit = max(1, min(limit, POSTALL_MAX_LIMIT))

    msg = await update.effective_message.reply_text(
        f"📚 <b>Postall iniciado em segundo plano.</b>\n"
        f"Vou postar <b>{limit}</b> anime(s) novos e o bot continua respondendo normalmente.",
        parse_mode="HTML",
    )

    async def runner():
        try:
            await _run_postall_batch(context, msg, limit)
        except Exception as exc:
            print("ERRO POSTALL GERAL:", repr(exc))
            await msg.edit_text(
                "❌ <b>Não consegui montar a fila do postall.</b>",
                parse_mode="HTML",
            )

    context.application.create_task(runner())
