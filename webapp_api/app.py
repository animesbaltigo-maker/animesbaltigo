from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from core.http_client import get_http_client
from services.animefire_client import (
    get_anime_details,
    get_episode_player,
    get_episodes,
    search_anime,
)
from services.recent_episodes_client import get_recent_episodes

BASE_URL = "https://animefire.io"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": BASE_URL,
    "Origin": BASE_URL,
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

HOME_SECTION_LIMIT = 10
GRID_PAGE_LIMIT = 20
MAX_HOME_CONCURRENCY = 4
MAX_RECENT_DETAIL_CONCURRENCY = 6

SECTION_TTL = 60 * 15
RECENT_TTL = 60
SEARCH_TTL = 60 * 10
ANIME_TTL = 60 * 60 * 2
EPISODE_TTL = 60 * 60

SECTIONS: dict[str, dict[str, str]] = {
    "recentes": {"title": "Últimos Episódios", "kind": "recent"},
    "em_lancamento": {"title": "Em lançamento", "slug": "em-lancamento"},
    "atualizados": {"title": "Atualizados", "slug": "animes-atualizados"},
    "top": {"title": "Top Animes", "slug": "top-animes"},
    "legendados": {"title": "Legendados", "slug": "lista-de-animes-legendados"},
    "dublados": {"title": "Dublados", "slug": "lista-de-animes-dublados"},
    "acao": {"title": "Ação", "slug": "genero/acao"},
    "aventura": {"title": "Aventura", "slug": "genero/aventura"},
    "comedia": {"title": "Comédia", "slug": "genero/comedia"},
    "drama": {"title": "Drama", "slug": "genero/drama"},
    "fantasia": {"title": "Fantasia", "slug": "genero/fantasia"},
    "romance": {"title": "Romance", "slug": "genero/romance"},
    "sobrenatural": {"title": "Sobrenatural", "slug": "genero/sobrenatural"},
    "suspense": {"title": "Suspense", "slug": "genero/suspense"},
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("baltigo_api")

app = FastAPI(title="QG BALTIGO API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_CACHE: dict[str, dict[str, Any]] = {}
_LOCKS: dict[str, asyncio.Lock] = {}

BASE_DIR = Path(__file__).resolve().parent.parent
MINIAPP_DIR = BASE_DIR / "miniapp"

if MINIAPP_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(MINIAPP_DIR)), name="static")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _clean_description(text: str) -> str:
    text = text or ""
    junk_patterns = [
        r"Oie.*?Clique Aqui",
        r"Reportar Erro:.*",
        r"Publicado Dia:.*",
        r"Dê o máximo de detalhes.*",
        r"se o vídeo não carregar.*",
        r"quer ser notificado sempre que um episódio novo for lançado\?.*",
    ]
    for pattern in junk_patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    return _clean(text)


def _clean_genres(genres: list[str] | None) -> list[str]:
    if not genres:
        return []

    seen: set[str] = set()
    cleaned: list[str] = []

    for genre in genres:
        g = _clean(str(genre))
        if not g:
            continue
        if g.lower().startswith("animes de "):
            continue
        if g.lower() in {"oie ツ", "clique aqui"}:
            continue
        if g not in seen:
            seen.add(g)
            cleaned.append(g)

    return cleaned


def _clean_alt_titles(values: list[str] | None) -> list[str]:
    if not values:
        return []

    cleaned: list[str] = []
    seen: set[str] = set()

    for value in values:
        v = _clean_description(str(value))
        if not v:
            continue
        if len(v) > 120:
            continue
        if v not in seen:
            seen.add(v)
            cleaned.append(v)

    return cleaned


def _cache_get(key: str, ttl: int):
    item = _CACHE.get(key)
    if not item:
        return None
    if time.time() - item["ts"] > ttl:
        _CACHE.pop(key, None)
        return None
    return item["data"]


def _cache_set(key: str, data: Any) -> Any:
    _CACHE[key] = {"ts": time.time(), "data": data}
    return data


def _cache_invalidate_prefix(*prefixes: str) -> None:
    if not prefixes:
        return

    for key in list(_CACHE.keys()):
        if any(key.startswith(prefix) for prefix in prefixes):
            _CACHE.pop(key, None)


async def _cached(key: str, ttl: int, factory):
    cached = _cache_get(key, ttl)
    if cached is not None:
        return cached

    lock = _LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _cache_get(key, ttl)
        if cached is not None:
            return cached
        data = await factory()
        return _cache_set(key, data)


async def _get(url: str) -> str:
    client = await get_http_client()
    response = await client.get(url, headers=HEADERS)
    response.raise_for_status()
    return response.text


def _extract_slug_from_href(href: str) -> str:
    href = (href or "").strip()
    match = re.search(r"/animes/([^/]+?)(?:/)?$", href)
    return match.group(1).strip() if match else ""


def _extract_last_page(page_html: str, slug: str) -> int:
    soup = BeautifulSoup(page_html, "html.parser")
    max_page = 1

    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        pattern = rf"/{re.escape(slug)}/(\d+)(?:/)?$"
        match = re.search(pattern, href)
        if match:
            page_num = int(match.group(1))
            max_page = max(max_page, page_num)

    return max_page


def _extract_listing_cards(page_html: str) -> list[dict]:
    soup = BeautifulSoup(page_html, "html.parser")
    found: dict[str, dict] = {}

    for anchor in soup.select("a[href*='/animes/']"):
        href = (anchor.get("href") or "").strip()
        anime_id = _extract_slug_from_href(href)
        if not anime_id:
            continue

        title = ""
        title_el = anchor.select_one(".animeTitle")
        if title_el:
            title = _clean(title_el.get_text(" ", strip=True))

        img = anchor.find("img")
        cover = ""
        if img:
            cover = img.get("data-src") or img.get("src") or ""
            title = title or _clean(img.get("alt") or "")

        title = title or anime_id.replace("-", " ").title()
        is_dubbed = "dublado" in title.lower() or "dublado" in anime_id.lower()

        found[anime_id] = {
            "id": anime_id,
            "title": title,
            "display_title": f"[{'DUB' if is_dubbed else 'LEG'}] {title}",
            "prefix": "DUB" if is_dubbed else "LEG",
            "is_dubbed": is_dubbed,
            "cover_url": cover,
            "banner_url": cover,
            "description": "",
            "genres": [],
            "score": None,
            "status": "",
            "episodes": None,
            "year": None,
            "studio": "",
            "url": urljoin(BASE_URL, href),
        }

    return list(found.values())


def _shape_details(data: dict, fallback_id: str = "") -> dict:
    anime_id = data.get("id") or fallback_id
    is_dubbed = (
        bool(data.get("is_dubbed"))
        or "dublado" in (anime_id or "").lower()
        or "dublado" in (data.get("title") or "").lower()
    )
    title = data.get("title") or anime_id.replace("-", " ").title()

    return {
        "id": anime_id,
        "title": title,
        "display_title": f"[{'DUB' if is_dubbed else 'LEG'}] {title}",
        "prefix": "DUB" if is_dubbed else "LEG",
        "is_dubbed": is_dubbed,
        "cover_url": data.get("cover_url") or data.get("media_image_url") or data.get("banner_url") or "",
        "banner_url": data.get("banner_url") or data.get("cover_url") or data.get("media_image_url") or "",
        "description": _clean_description(data.get("description") or ""),
        "genres": _clean_genres(data.get("genres") or []),
        "score": data.get("score"),
        "status": data.get("status") or "",
        "episodes": data.get("episodes"),
        "year": data.get("season_year"),
        "studio": _clean(data.get("studio") or ""),
        "alt_titles": _clean_alt_titles(data.get("alt_titles") or []),
    }


def _section_conf(section: str) -> dict[str, str] | None:
    return SECTIONS.get((section or "").strip().lower())


def _section_url(slug: str, page: int) -> str:
    if page <= 1:
        return f"{BASE_URL}/{slug}"
    return f"{BASE_URL}/{slug}/{page}"


def _normalize_quality(quality: str) -> str:
    q = (quality or "HD").strip().upper()
    return q if q else "HD"


def _safe_total_items_from_page(total_pages: int, current_items: int) -> int:
    if total_pages <= 1:
        return current_items
    return ((total_pages - 1) * GRID_PAGE_LIMIT) + current_items


def _empty_section_payload(section: str, page: int = 1) -> dict:
    conf = _section_conf(section)
    title = conf["title"] if conf else section.replace("_", " ").title()
    return {
        "section": section,
        "title": title,
        "page": page,
        "limit": GRID_PAGE_LIMIT,
        "total_items": 0,
        "total_pages": 0,
        "has_next": False,
        "has_prev": page > 1,
        "items": [],
    }


async def _safe_get_anime_details(anime_id: str) -> dict | None:
    try:
        return await get_anime_details(anime_id)
    except Exception:
        logger.exception("Falha ao buscar detalhes do anime: %s", anime_id)
        return None


async def _fetch_recent_cover_with_fallback(anime_id: str, fallback_cover: str) -> str:
    if fallback_cover:
        return fallback_cover

    details = await _safe_get_anime_details(anime_id)
    if not details:
        return ""

    return (
        details.get("cover_url")
        or details.get("media_image_url")
        or details.get("banner_url")
        or ""
    )


async def _get_recent_page(page: int) -> dict:
    async def factory():
        try:
            recent = await get_recent_episodes(limit=200)
        except Exception:
            logger.exception("Falha ao buscar episódios recentes")
            return _empty_section_payload("recentes", page)

        seen = set()
        raw_items = []

        for item in recent:
            anime_id = item.get("anime_id")
            if not anime_id or anime_id in seen:
                continue
            seen.add(anime_id)
            raw_items.append(item)

        semaphore = asyncio.Semaphore(MAX_RECENT_DETAIL_CONCURRENCY)

        async def build_item(item: dict) -> dict:
            anime_id = item.get("anime_id")
            title = item.get("title") or anime_id.replace("-", " ").title()
            is_dubbed = "dublado" in title.lower() or "dublado" in anime_id.lower()
            cover = (
                item.get("thumb")
                or item.get("image")
                or item.get("cover")
                or item.get("cover_url")
                or ""
            )

            async with semaphore:
                final_cover = await _fetch_recent_cover_with_fallback(anime_id, cover)

            return {
                "id": anime_id,
                "title": title,
                "display_title": f"[{'DUB' if is_dubbed else 'LEG'}] {title}",
                "prefix": "DUB" if is_dubbed else "LEG",
                "is_dubbed": is_dubbed,
                "cover_url": final_cover,
                "banner_url": final_cover,
                "episode": item.get("episode"),
                "description": "",
                "genres": [],
                "score": None,
                "status": "",
                "episodes": None,
                "year": None,
                "studio": "",
            }

        items = await asyncio.gather(*(build_item(item) for item in raw_items), return_exceptions=True)

        normalized_items = []
        for item in items:
            if isinstance(item, Exception):
                logger.exception("Falha ao montar item recente", exc_info=item)
                continue
            normalized_items.append(item)

        total = len(normalized_items)
        total_pages = max(1, (total + GRID_PAGE_LIMIT - 1) // GRID_PAGE_LIMIT) if total else 1
        current_page = min(max(1, page), total_pages)
        start = (current_page - 1) * GRID_PAGE_LIMIT
        end = start + GRID_PAGE_LIMIT
        page_items = normalized_items[start:end]

        return {
            "section": "recentes",
            "title": _section_conf("recentes")["title"],
            "page": current_page,
            "limit": GRID_PAGE_LIMIT,
            "total_items": total,
            "total_pages": total_pages,
            "has_next": current_page < total_pages,
            "has_prev": current_page > 1,
            "items": page_items,
        }

    return await _cached(f"recentes:{page}", RECENT_TTL, factory)


async def _get_paginated_section_page(section: str, page: int) -> dict:
    conf = _section_conf(section)
    if not conf:
        return _empty_section_payload(section, page)

    if conf.get("kind") == "recent":
        return await _get_recent_page(page)

    slug = conf["slug"]

    async def meta_factory():
        first_html = await _get(_section_url(slug, 1))
        total_pages = _extract_last_page(first_html, slug)
        return {"first_html": first_html, "total_pages": total_pages}

    try:
        meta = await _cached(f"meta:{section}", SECTION_TTL, meta_factory)
        total_pages = max(1, int(meta["total_pages"]))
        current_page = min(max(1, page), total_pages)

        async def page_factory():
            page_html = meta["first_html"] if current_page == 1 else await _get(_section_url(slug, current_page))
            items = _extract_listing_cards(page_html)[:GRID_PAGE_LIMIT]
            total_items = _safe_total_items_from_page(total_pages, len(items))

            return {
                "section": section,
                "title": conf["title"],
                "page": current_page,
                "limit": GRID_PAGE_LIMIT,
                "total_items": total_items,
                "total_pages": total_pages,
                "has_next": current_page < total_pages,
                "has_prev": current_page > 1,
                "items": items,
            }

        return await _cached(f"page:{section}:{current_page}", SECTION_TTL, page_factory)

    except Exception:
        logger.exception("Falha ao carregar seção '%s' página %s", section, page)
        return _empty_section_payload(section, page)


@app.on_event("startup")
async def _startup_refresh_task():
    async def refresher():
        while True:
            try:
                _cache_invalidate_prefix("recentes:", "meta:")
            except Exception:
                logger.exception("Falha ao invalidar cache periódico")
            await asyncio.sleep(60)

    asyncio.create_task(refresher())


@app.get("/", include_in_schema=False)
async def serve_index():
    index_file = MINIAPP_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    raise HTTPException(status_code=404, detail="index.html não encontrado")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/debug/home")
async def debug_home():
    ordered_sections = [
        "recentes",
        "em_lancamento",
        "atualizados",
        "top",
        "legendados",
        "dublados",
        "acao",
        "aventura",
        "comedia",
    ]

    result = []
    for section in ordered_sections:
        try:
            data = await _get_paginated_section_page(section, 1)
            result.append(
                {
                    "section": section,
                    "ok": True,
                    "count": len(data.get("items", [])),
                    "title": data.get("title", ""),
                }
            )
        except Exception as e:
            result.append(
                {
                    "section": section,
                    "ok": False,
                    "error": str(e),
                }
            )

    return {"ok": True, "sections": result}


@app.get("/api/catalog/home")
async def catalog_home(
    home_limit: int = Query(HOME_SECTION_LIMIT, ge=1, le=20),
):
    ordered_sections = [
        "recentes",
        "em_lancamento",
        "atualizados",
        "top",
        "legendados",
        "dublados",
        "acao",
        "aventura",
        "comedia",
    ]

    semaphore = asyncio.Semaphore(MAX_HOME_CONCURRENCY)

    async def load_section(section: str):
        async with semaphore:
            try:
                page_data = await _get_paginated_section_page(section, 1)
                return {
                    "key": section,
                    "title": page_data["title"],
                    "page": 1,
                    "total_pages": page_data["total_pages"],
                    "items": page_data["items"][:home_limit],
                }
            except Exception:
                logger.exception("Falha ao montar home da seção '%s'", section)
                conf = _section_conf(section)
                return {
                    "key": section,
                    "title": conf["title"] if conf else section,
                    "page": 1,
                    "total_pages": 0,
                    "items": [],
                }

    try:
        payload = await asyncio.gather(*(load_section(section) for section in ordered_sections))
        return {"ok": True, "sections": payload}
    except Exception:
        logger.exception("Falha geral na home")
        return {
            "ok": True,
            "sections": [
                {
                    "key": section,
                    "title": (_section_conf(section) or {}).get("title", section),
                    "page": 1,
                    "total_pages": 0,
                    "items": [],
                }
                for section in ordered_sections
            ],
        }


@app.get("/api/catalog/list")
async def catalog_list(
    section: str = Query("dublados"),
    page: int = Query(1, ge=1),
):
    data = await _get_paginated_section_page(section, page)
    return {"ok": True, **data}


@app.get("/api/search")
async def api_search(
    q: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    limit: int = Query(GRID_PAGE_LIMIT, ge=1, le=60),
):
    query = q.strip()

    async def factory():
        try:
            raw_items = await search_anime(query)
        except Exception:
            logger.exception("Falha na busca por '%s'", query)
            return []

        shaped = []

        for item in raw_items:
            anime_id = item.get("id")
            if not anime_id:
                continue

            is_dubbed = bool(item.get("is_dubbed"))
            title = item.get("title") or anime_id

            shaped.append(
                {
                    "id": anime_id,
                    "title": title,
                    "display_title": f"[{'DUB' if is_dubbed else 'LEG'}] {title}",
                    "prefix": "DUB" if is_dubbed else "LEG",
                    "cover_url": item.get("cover_url") or item.get("banner_url") or "",
                    "banner_url": item.get("banner_url") or item.get("cover_url") or "",
                    "is_dubbed": is_dubbed,
                    "description": "",
                    "genres": [],
                    "score": None,
                    "status": "",
                    "episodes": None,
                    "year": None,
                    "studio": "",
                }
            )

        return shaped

    shaped = await _cached(f"search:{query.lower()}", SEARCH_TTL, factory)

    total = len(shaped)
    total_pages = max(1, (total + limit - 1) // limit) if total else 0
    current_page = min(page, total_pages) if total_pages else 1
    start = (current_page - 1) * limit
    end = start + limit

    return {
        "ok": True,
        "query": query,
        "items": shaped[start:end],
        "count": total,
        "page": current_page,
        "limit": limit,
        "total_pages": total_pages,
        "has_next": current_page < total_pages if total_pages else False,
        "has_prev": current_page > 1,
    }


@app.get("/api/anime/{anime_id}")
async def api_anime(
    anime_id: str,
    episode_limit: int = Query(400, ge=1, le=2000),
):
    async def factory():
        data = await _safe_get_anime_details(anime_id)
        if not data:
            return None

        try:
            episodes_payload = await get_episodes(anime_id, 0, episode_limit)
        except Exception:
            logger.exception("Falha ao buscar episódios de %s", anime_id)
            episodes_payload = {}

        episodes = episodes_payload.get("all_items") or episodes_payload.get("items") or []

        return {
            "item": _shape_details(data, anime_id),
            "episodes": episodes,
        }

    payload = await _cached(f"anime:{anime_id}:limit:{episode_limit}", ANIME_TTL, factory)
    if not payload:
        raise HTTPException(status_code=404, detail="Anime não encontrado")

    return {"ok": True, **payload}


@app.get("/api/anime/{anime_id}/episode/{episode}")
async def api_episode(
    anime_id: str,
    episode: str,
    quality: str = Query("HD"),
):
    normalized_quality = _normalize_quality(quality)
    key = f"episode:{anime_id}:{episode}:{normalized_quality}"

    async def factory():
        try:
            item = await get_episode_player(anime_id, episode, normalized_quality)
        except Exception:
            logger.exception(
                "Falha ao buscar player do anime=%s episodio=%s quality=%s",
                anime_id,
                episode,
                normalized_quality,
            )
            return None

        if not item:
            return None

        available_qualities = item.get("available_qualities") or []
        if normalized_quality not in available_qualities and available_qualities:
            selected_quality = item.get("quality") or available_qualities[0]
        else:
            selected_quality = item.get("quality") or normalized_quality

        return {
            "anime_id": anime_id,
            "episode": episode,
            "video": item.get("video") or "",
            "videos": item.get("videos") or {},
            "quality": selected_quality,
            "available_qualities": available_qualities,
            "title": item.get("title") or "",
            "description": _clean_description(item.get("description") or ""),
            "thumb": item.get("thumb") or item.get("image") or "",
            "prev_episode": item.get("prev_episode"),
            "next_episode": item.get("next_episode"),
            "total_episodes": item.get("total_episodes"),
        }

    payload = await _cached(key, EPISODE_TTL, factory)
    if not payload:
        raise HTTPException(status_code=404, detail="Episódio não encontrado")

    return {"ok": True, "item": payload}
