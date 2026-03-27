from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
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

HOME_LIMIT = 10
GRID_LIMIT = 20

SECTIONS: dict[str, dict[str, str]] = {
    "recentes": {"title": "Últimos Episódios", "kind": "recent"},
    "em_lancamento": {"title": "Em lançamento", "slug": "em-lancamento"},
    "atualizados": {"title": "Atualizados", "slug": "animes-atualizados"},
    "legendados": {"title": "Legendados", "slug": "lista-de-animes-legendados"},
    "dublados": {"title": "Dublados", "slug": "lista-de-animes-dublados"},
    "acao": {"title": "Ação", "slug": "genero/acao"},
    "aventura": {"title": "Aventura", "slug": "genero/aventura"},
    "comedia": {"title": "Comédia", "slug": "genero/comedia"},
}

HOME_ORDER = [
    "recentes",
    "em_lancamento",
    "atualizados",
    "legendados",
    "dublados",
    "acao",
    "aventura",
    "comedia",
]

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

BASE_DIR = Path(__file__).resolve().parent.parent
MINIAPP_DIR = BASE_DIR / "miniapp"

if MINIAPP_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(MINIAPP_DIR)), name="static")

_CACHE: dict[str, dict[str, Any]] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _cache_get(key: str, ttl: int):
    item = _CACHE.get(key)
    if not item:
        return None
    if time.time() - item["ts"] > ttl:
        _CACHE.pop(key, None)
        return None
    return item["data"]


def _cache_set(key: str, data: Any):
    _CACHE[key] = {"ts": time.time(), "data": data}
    return data


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

    if response.status_code in (403, 404):
        logger.warning("Bloqueado ou não encontrado: %s (%s)", url, response.status_code)
        return ""

    response.raise_for_status()
    return response.text


def _section_conf(section: str) -> dict[str, str] | None:
    return SECTIONS.get((section or "").strip().lower())


def _section_url(slug: str, page: int) -> str:
    if page <= 1:
        return f"{BASE_URL}/{slug}"
    return f"{BASE_URL}/{slug}/{page}"


def _extract_slug_from_href(href: str) -> str:
    href = (href or "").strip()
    match = re.search(r"/animes/([^/]+?)(?:/)?$", href)
    return match.group(1).strip() if match else ""


def _extract_last_page(page_html: str, slug: str) -> int:
    if not page_html:
        return 1

    soup = BeautifulSoup(page_html, "html.parser")
    max_page = 1

    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        match = re.search(rf"/{re.escape(slug)}/(\d+)(?:/)?$", href)
        if match:
            max_page = max(max_page, int(match.group(1)))

    return max_page


def _extract_listing_cards(page_html: str) -> list[dict]:
    if not page_html:
        return []

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


def _empty_section_payload(section: str, page: int = 1) -> dict:
    conf = _section_conf(section)
    title = conf["title"] if conf else section.replace("_", " ").title()

    return {
        "section": section,
        "title": title,
        "page": page,
        "limit": GRID_LIMIT,
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
        logger.exception("Falha ao buscar detalhes: %s", anime_id)
        return None


def _shape_details(data: dict, fallback_id: str = "") -> dict:
    anime_id = data.get("id") or fallback_id
    title = data.get("title") or anime_id.replace("-", " ").title()
    is_dubbed = (
        bool(data.get("is_dubbed"))
        or "dublado" in anime_id.lower()
        or "dublado" in title.lower()
    )

    return {
        "id": anime_id,
        "title": title,
        "display_title": f"[{'DUB' if is_dubbed else 'LEG'}] {title}",
        "prefix": "DUB" if is_dubbed else "LEG",
        "is_dubbed": is_dubbed,
        "cover_url": data.get("cover_url") or data.get("media_image_url") or data.get("banner_url") or "",
        "banner_url": data.get("banner_url") or data.get("cover_url") or data.get("media_image_url") or "",
        "description": _clean(data.get("description") or ""),
        "genres": data.get("genres") or [],
        "score": data.get("score"),
        "status": data.get("status") or "",
        "episodes": data.get("episodes"),
        "year": data.get("season_year"),
        "studio": _clean(data.get("studio") or ""),
        "alt_titles": data.get("alt_titles") or [],
    }


async def _get_recent_page(page: int) -> dict:
    async def factory():
        try:
            recent = await get_recent_episodes(limit=200)
        except Exception:
            logger.exception("Falha ao buscar recentes")
            return _empty_section_payload("recentes", page)

        seen = set()
        items = []

        for item in recent:
            anime_id = item.get("anime_id")
            if not anime_id or anime_id in seen:
                continue
            seen.add(anime_id)

            title = item.get("title") or anime_id.replace("-", " ").title()
            is_dubbed = "dublado" in title.lower() or "dublado" in anime_id.lower()
            cover = (
                item.get("thumb")
                or item.get("image")
                or item.get("cover")
                or item.get("cover_url")
                or ""
            )

            items.append(
                {
                    "id": anime_id,
                    "title": title,
                    "display_title": f"[{'DUB' if is_dubbed else 'LEG'}] {title}",
                    "prefix": "DUB" if is_dubbed else "LEG",
                    "is_dubbed": is_dubbed,
                    "cover_url": cover,
                    "banner_url": cover,
                    "episode": item.get("episode"),
                    "description": "",
                    "genres": [],
                    "score": None,
                    "status": "",
                    "episodes": None,
                    "year": None,
                    "studio": "",
                }
            )

        total = len(items)
        total_pages = max(1, (total + GRID_LIMIT - 1) // GRID_LIMIT) if total else 1
        current_page = min(max(1, page), total_pages)
        start = (current_page - 1) * GRID_LIMIT
        end = start + GRID_LIMIT

        return {
            "section": "recentes",
            "title": "Últimos Episódios",
            "page": current_page,
            "limit": GRID_LIMIT,
            "total_items": total,
            "total_pages": total_pages,
            "has_next": current_page < total_pages,
            "has_prev": current_page > 1,
            "items": items[start:end],
        }

    return await _cached(f"recentes:{page}", 60, factory)


async def _get_paginated_section_page(section: str, page: int) -> dict:
    conf = _section_conf(section)
    if not conf:
        return _empty_section_payload(section, page)

    if conf.get("kind") == "recent":
        return await _get_recent_page(page)

    slug = conf["slug"]

    async def factory():
        try:
            first_html = await _get(_section_url(slug, 1))
            if not first_html:
                return _empty_section_payload(section, page)

            total_pages = _extract_last_page(first_html, slug)
            current_page = min(max(1, page), max(1, total_pages))

            page_html = first_html if current_page == 1 else await _get(_section_url(slug, current_page))
            if not page_html:
                return _empty_section_payload(section, current_page)

            items = _extract_listing_cards(page_html)[:GRID_LIMIT]

            return {
                "section": section,
                "title": conf["title"],
                "page": current_page,
                "limit": GRID_LIMIT,
                "total_items": len(items),
                "total_pages": total_pages,
                "has_next": current_page < total_pages,
                "has_prev": current_page > 1,
                "items": items,
            }
        except httpx.HTTPStatusError:
            return _empty_section_payload(section, page)
        except Exception:
            logger.exception("Falha na seção %s", section)
            return _empty_section_payload(section, page)

    return await _cached(f"section:{section}:page:{page}", 300, factory)


@app.get("/", include_in_schema=False)
async def serve_index():
    index_file = MINIAPP_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    raise HTTPException(status_code=404, detail="index.html não encontrado")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/catalog/home")
async def catalog_home(home_limit: int = Query(HOME_LIMIT, ge=1, le=20)):
    async def load_section(section: str):
        data = await _get_paginated_section_page(section, 1)
        return {
            "key": section,
            "title": data["title"],
            "page": 1,
            "total_pages": data["total_pages"],
            "items": data["items"][:home_limit],
        }

    sections = await asyncio.gather(*(load_section(section) for section in HOME_ORDER), return_exceptions=True)

    normalized = []
    for i, item in enumerate(sections):
        key = HOME_ORDER[i]
        if isinstance(item, Exception):
            conf = _section_conf(key)
            normalized.append(
                {
                    "key": key,
                    "title": conf["title"] if conf else key,
                    "page": 1,
                    "total_pages": 0,
                    "items": [],
                }
            )
        else:
            normalized.append(item)

    return {"ok": True, "sections": normalized}


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
    limit: int = Query(GRID_LIMIT, ge=1, le=60),
):
    query = q.strip()

    async def factory():
        try:
            raw_items = await search_anime(query)
        except Exception:
            logger.exception("Falha na busca por %s", query)
            return []

        shaped = []
        for item in raw_items:
            anime_id = item.get("id")
            if not anime_id:
                continue

            title = item.get("title") or anime_id
            is_dubbed = bool(item.get("is_dubbed"))

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

    shaped = await _cached(f"search:{query.lower()}", 300, factory)

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
        details = await _safe_get_anime_details(anime_id)
        if not details:
            return None

        try:
            episodes_payload = await get_episodes(anime_id, 0, episode_limit)
        except Exception:
            logger.exception("Falha ao buscar episódios de %s", anime_id)
            episodes_payload = {}

        episodes = episodes_payload.get("all_items") or episodes_payload.get("items") or []

        return {
            "item": _shape_details(details, anime_id),
            "episodes": episodes,
        }

    payload = await _cached(f"anime:{anime_id}:{episode_limit}", 1800, factory)
    if not payload:
        raise HTTPException(status_code=404, detail="Anime não encontrado")

    return {"ok": True, **payload}


@app.get("/api/anime/{anime_id}/episode/{episode}")
async def api_episode(
    anime_id: str,
    episode: str,
    quality: str = Query("HD"),
):
    q = (quality or "HD").upper().strip()

    async def factory():
        try:
            item = await get_episode_player(anime_id, episode, q)
        except Exception:
            logger.exception("Falha ao buscar player %s %s %s", anime_id, episode, q)
            return None
        return item

    item = await _cached(f"player:{anime_id}:{episode}:{q}", 1800, factory)
    if not item:
        raise HTTPException(status_code=404, detail="Episódio não encontrado")

    return {"ok": True, "item": item}
