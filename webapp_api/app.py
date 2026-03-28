from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

HOME_SECTION_LIMIT = 12
GRID_PAGE_LIMIT = 24

SECTION_TTL = 60 * 15
RECENT_TTL = 60
SEARCH_TTL = 60 * 10
ANIME_TTL = 60 * 60 * 2
HERO_TTL = 60 * 10
PROGRESS_TTL = 60 * 60 * 24 * 30
MAX_EPISODES_FETCH = 1200

# Episode TTL is intentionally short because video links expire.
# With ?refresh=1 the cache is bypassed entirely.
EPISODE_TTL = 60 * 4

# ── Proxy tunables ────────────────────────────────────────────────────────────
PROXY_TIMEOUT = httpx.Timeout(connect=8.0, read=60.0, write=60.0, pool=10.0)
PROXY_MAX_RETRIES = 3
PROXY_CHUNK_SIZE = 1024 * 1024  # 1 MB chunks for fast streaming
PROXY_KEEPALIVE_EXPIRY = 30     # seconds to keep idle connections alive
PROXY_MAX_CONNECTIONS = 100
PROXY_MAX_KEEPALIVE = 20

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

BASE_DIR = Path(__file__).resolve().parent.parent
MINIAPP_DIR = BASE_DIR / "miniapp"

app = FastAPI(
    title="QG BALTIGO API",
    description="API do miniapp do bot com catálogo, episódios e proxy de vídeo",
    version="4.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_CACHE: dict[str, dict[str, Any]] = {}
_LOCKS: dict[str, asyncio.Lock] = {}
_PROGRESS: dict[str, dict[str, Any]] = {}

# ── Global persistent proxy client ───────────────────────────────────────────
# One client for the entire process lifetime; uses connection pooling and
# keep-alive so every proxy request re-uses an already-open TCP connection
# instead of paying the full TLS handshake cost every time.
_PROXY_CLIENT: httpx.AsyncClient | None = None


def _get_proxy_limits() -> httpx.Limits:
    return httpx.Limits(
        max_connections=PROXY_MAX_CONNECTIONS,
        max_keepalive_connections=PROXY_MAX_KEEPALIVE,
        keepalive_expiry=PROXY_KEEPALIVE_EXPIRY,
    )


class ProgressPayload(BaseModel):
    user_id: str
    anime_id: str
    episode: str
    watched_seconds: int = 0
    duration_seconds: int = 0
    title: str = ""
    cover_url: str = ""
    completed: bool = False


async def get_proxy_client() -> httpx.AsyncClient:
    global _PROXY_CLIENT
    if _PROXY_CLIENT is None or _PROXY_CLIENT.is_closed:
        _PROXY_CLIENT = httpx.AsyncClient(
            timeout=PROXY_TIMEOUT,
            follow_redirects=True,
            limits=_get_proxy_limits(),
            http2=False,  # many CDNs behave better with HTTP/1.1
        )
    return _PROXY_CLIENT


# =============================================================================
# HELPERS
# =============================================================================

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
        r"Clique aqui.*",
        r"assista também.*",
        r"veja também.*",
    ]
    for pattern in junk_patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    return _clean(text)


def _clean_genres(genres: list[str] | None) -> list[str]:
    if not genres:
        return []
    seen: set[str] = set()
    cleaned: list[str] = []
    skip_prefixes = ("animes de ", "oie", "clique")
    for genre in genres:
        g = _clean(str(genre))
        if not g:
            continue
        gl = g.lower()
        if any(gl.startswith(prefix) for prefix in skip_prefixes):
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
        if not v or len(v) > 120:
            continue
        if v not in seen:
            seen.add(v)
            cleaned.append(v)
    return cleaned


def _is_dubbed(anime_id: str, title: str) -> bool:
    value_id = (anime_id or "").lower()
    value_title = (title or "").lower()
    return (
        "dublado" in value_id
        or "dublado" in value_title
        or "(dub)" in value_title
    )


def _section_conf(section: str) -> dict[str, str] | None:
    return SECTIONS.get((section or "").strip().lower())


def _section_url(slug: str, page: int) -> str:
    if page <= 1:
        return f"{BASE_URL}/{slug}"
    return f"{BASE_URL}/{slug}/{page}"


def _cache_get(key: str, ttl: int) -> Any | None:
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


async def _cached(key: str, ttl: int, factory) -> Any:
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


def _invalidate_key(key: str) -> None:
    _CACHE.pop(key, None)


def _invalidate_prefix(prefix: str) -> None:
    for key in list(_CACHE.keys()):
        if key.startswith(prefix):
            _CACHE.pop(key, None)


async def _get(url: str) -> str:
    client = await get_http_client()
    response = await client.get(url, headers=HEADERS)
    response.raise_for_status()
    return response.text


def _extract_slug_from_href(href: str) -> str:
    href = (href or "").strip()
    match = re.search(r"/animes/([^/?#]+?)(?:/)?(?:\?.*)?$", href)
    return match.group(1).strip() if match else ""


def _extract_last_page(page_html: str, slug: str) -> int:
    soup = BeautifulSoup(page_html, "html.parser")
    max_page = 1
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        pattern = rf"/{re.escape(slug)}/(\d+)(?:/)?(?:\?.*)?$"
        match = re.search(pattern, href)
        if match:
            max_page = max(max_page, int(match.group(1)))
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
        dubbed = _is_dubbed(anime_id, title)

        found[anime_id] = {
            "id": anime_id,
            "title": title,
            "display_title": f"[{'DUB' if dubbed else 'LEG'}] {title}",
            "prefix": "DUB" if dubbed else "LEG",
            "is_dubbed": dubbed,
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
    title = data.get("title") or anime_id.replace("-", " ").title()
    dubbed = bool(data.get("is_dubbed")) or _is_dubbed(anime_id, title)
    return {
        "id": anime_id,
        "title": title,
        "display_title": f"[{'DUB' if dubbed else 'LEG'}] {title}",
        "prefix": "DUB" if dubbed else "LEG",
        "is_dubbed": dubbed,
        "cover_url": (
            data.get("cover_url")
            or data.get("media_image_url")
            or data.get("banner_url")
            or ""
        ),
        "banner_url": (
            data.get("banner_url")
            or data.get("cover_url")
            or data.get("media_image_url")
            or ""
        ),
        "description": _clean_description(data.get("description") or ""),
        "genres": _clean_genres(data.get("genres") or []),
        "score": data.get("score"),
        "status": data.get("status") or "",
        "episodes": data.get("episodes"),
        "year": data.get("season_year"),
        "studio": _clean(data.get("studio") or ""),
        "alt_titles": _clean_alt_titles(data.get("alt_titles") or []),
    }


def _shape_episode_payload(anime_id: str, episode: str, quality: str, item: dict) -> dict:
    video = item.get("video") or ""
    videos = item.get("videos") or {}
    available_qualities = item.get("available_qualities") or []

    if not available_qualities and isinstance(videos, dict):
        available_qualities = list(videos.keys())

    if not video and isinstance(videos, dict):
        normalized_quality = (quality or "HD").upper()
        video = (
            videos.get(normalized_quality)
            or videos.get("HD")
            or videos.get("SD")
            or ""
        )

    return {
        "anime_id": anime_id,
        "episode": episode,
        "video": video,
        "videos": videos,
        "quality": item.get("quality") or quality.upper(),
        "available_qualities": available_qualities,
        "title": item.get("title") or "",
        "description": _clean_description(item.get("description") or ""),
        "thumb": item.get("thumb") or item.get("image") or "",
        "is_dubbed": bool(item.get("is_dubbed")),
        "prev_episode": item.get("prev_episode"),
        "next_episode": item.get("next_episode"),
        "total_episodes": item.get("total_episodes"),
    }


def _parse_episode_number(value: str | int | float | None) -> Decimal | None:
    raw = str(value or "").strip().replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", raw)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def _episode_score(item: dict[str, Any]) -> int:
    score = 0
    if item.get("title"):
        score += 2
    if item.get("description"):
        score += 1
    if item.get("thumb") or item.get("image"):
        score += 1
    if item.get("video"):
        score += 3
    return score


def _normalize_episode_item(ep: dict[str, Any]) -> dict[str, Any]:
    value = dict(ep or {})
    number = value.get("number") or value.get("episode") or value.get("ep") or value.get("slug") or ""
    parsed = _parse_episode_number(number)
    value["number"] = str(number).strip()
    value["_sort_number"] = parsed if parsed is not None else Decimal("999999")
    value["_sort_title"] = _clean(str(value.get("title") or value.get("number") or ""))
    value["_score"] = _episode_score(value)
    return value


def _normalize_episodes(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not items:
        return []

    dedup: dict[str, dict[str, Any]] = {}
    for raw in items:
        item = _normalize_episode_item(raw)
        number = item.get("number") or item.get("_sort_title") or "sem-numero"
        key = f"{number}|{item.get('url') or item.get('slug') or item.get('_sort_title')}"
        previous = dedup.get(key)
        if previous is None or item.get("_score", 0) >= previous.get("_score", 0):
            dedup[key] = item

    ordered = sorted(dedup.values(), key=lambda x: (x.get("_sort_number", Decimal("999999")), x.get("_sort_title", "")))
    cleaned: list[dict[str, Any]] = []
    total = len(ordered)
    for index, item in enumerate(ordered):
        item.pop("_score", None)
        item.pop("_sort_title", None)
        item.pop("_sort_number", None)
        item["prev_episode"] = ordered[index - 1]["number"] if index > 0 else None
        item["next_episode"] = ordered[index + 1]["number"] if index < total - 1 else None
        item["total_episodes"] = total
        cleaned.append(item)
    return cleaned


def _has_minimum_catalog_fields(item: dict[str, Any]) -> bool:
    return bool(item.get("id") and item.get("title") and (item.get("cover_url") or item.get("banner_url")))


def _truncate_description(text: str, size: int = 220) -> str:
    value = _clean_description(text)
    if len(value) <= size:
        return value
    return value[:size].rstrip() + "..."


async def _build_featured_payload() -> dict[str, Any] | None:
    async def factory():
        candidates = []
        for section in ("em_lancamento", "top", "recentes"):
            try:
                page = await _get_paginated_section_page(section, 1)
                candidates.extend(page.get("items") or [])
            except Exception:
                continue

        seen: set[str] = set()
        for candidate in candidates:
            anime_id = candidate.get("id")
            if not anime_id or anime_id in seen:
                continue
            seen.add(anime_id)
            try:
                details = await get_anime_details(anime_id)
                if not details:
                    continue
                shaped = _shape_details(details, anime_id)
                episodes_payload = await get_episodes(anime_id, 0, 60)
                episodes = _normalize_episodes(episodes_payload.get("all_items") or episodes_payload.get("items") or [])
                if not episodes:
                    continue
                return {
                    **shaped,
                    "description_short": _truncate_description(shaped.get("description") or candidate.get("description") or ""),
                    "first_episode": episodes[0].get("number") or "1",
                }
            except Exception:
                continue
        return None

    return await _cached("home:featured", HERO_TTL, factory)


# =============================================================================
# PAGINATION / CATALOG
# =============================================================================

async def _get_recent_page(page: int) -> dict:
    async def factory():
        recent = await get_recent_episodes(limit=200)
        seen: set[str] = set()
        items = []

        for item in recent:
            anime_id = item.get("anime_id")
            if not anime_id or anime_id in seen:
                continue
            seen.add(anime_id)

            title = item.get("title") or anime_id.replace("-", " ").title()
            dubbed = _is_dubbed(anime_id, title)
            cover = (
                item.get("thumb")
                or item.get("image")
                or item.get("cover")
                or item.get("cover_url")
                or ""
            )

            if not cover:
                try:
                    details = await get_anime_details(anime_id)
                    cover = (
                        details.get("cover_url")
                        or details.get("media_image_url")
                        or details.get("banner_url")
                        or ""
                    )
                except Exception:
                    cover = ""

            items.append({
                "id": anime_id,
                "title": title,
                "display_title": f"[{'DUB' if dubbed else 'LEG'}] {title}",
                "prefix": "DUB" if dubbed else "LEG",
                "is_dubbed": dubbed,
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
            })

        total = len(items)
        total_pages = max(1, (total + GRID_PAGE_LIMIT - 1) // GRID_PAGE_LIMIT)
        current_page = min(max(1, page), total_pages)
        start = (current_page - 1) * GRID_PAGE_LIMIT
        end = start + GRID_PAGE_LIMIT

        return {
            "section": "recentes",
            "title": _section_conf("recentes")["title"],
            "page": current_page,
            "limit": GRID_PAGE_LIMIT,
            "total_items": total,
            "total_pages": total_pages,
            "has_next": current_page < total_pages,
            "has_prev": current_page > 1,
            "items": items[start:end],
        }

    return await _cached(f"recentes:{page}", RECENT_TTL, factory)


async def _get_paginated_section_page(section: str, page: int) -> dict:
    conf = _section_conf(section)
    if not conf:
        return {
            "section": section,
            "title": section.replace("_", " ").title(),
            "page": page,
            "limit": GRID_PAGE_LIMIT,
            "total_items": 0,
            "total_pages": 0,
            "has_next": False,
            "has_prev": page > 1,
            "items": [],
        }

    if conf.get("kind") == "recent":
        return await _get_recent_page(page)

    slug = conf["slug"]

    async def meta_factory():
        first_html = await _get(_section_url(slug, 1))
        total_pages = _extract_last_page(first_html, slug)
        return {"first_html": first_html, "total_pages": total_pages}

    meta = await _cached(f"meta:{section}", SECTION_TTL, meta_factory)
    total_pages = max(1, int(meta["total_pages"]))
    current_page = min(max(1, page), total_pages)

    async def page_factory():
        if current_page == 1:
            page_html = meta["first_html"]
        else:
            page_html = await _get(_section_url(slug, current_page))

        items = _extract_listing_cards(page_html)
        return {
            "section": section,
            "title": conf["title"],
            "page": current_page,
            "limit": GRID_PAGE_LIMIT,
            "total_items": total_pages * GRID_PAGE_LIMIT,
            "total_pages": total_pages,
            "has_next": current_page < total_pages,
            "has_prev": current_page > 1,
            "items": items[:GRID_PAGE_LIMIT],
        }

    return await _cached(f"page:{section}:{current_page}", SECTION_TTL, page_factory)


# =============================================================================
# BACKGROUND
# =============================================================================

@app.on_event("startup")
async def _startup_tasks():
    # Pre-create the proxy client on startup so the first request is instant.
    await get_proxy_client()

    async def _recent_refresher():
        while True:
            try:
                _invalidate_prefix("recentes:")
                _invalidate_prefix("page:recentes")
            except Exception:
                pass
            await asyncio.sleep(60)

    asyncio.create_task(_recent_refresher())


@app.on_event("shutdown")
async def _shutdown_tasks():
    global _PROXY_CLIENT
    if _PROXY_CLIENT and not _PROXY_CLIENT.is_closed:
        await _PROXY_CLIENT.aclose()
        _PROXY_CLIENT = None


# =============================================================================
# ROOT / HEALTH
# =============================================================================

@app.get("/")
def root():
    return {
        "ok": True,
        "name": "QG BALTIGO API",
        "version": "4.0.0",
        "sections": list(SECTIONS.keys()),
    }


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "cache_entries": len(_CACHE),
        "timestamp": int(time.time()),
    }


# =============================================================================
# CATALOG
# =============================================================================

@app.get("/api/catalog/home")
async def catalog_home():
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

    tasks = [_get_paginated_section_page(section, 1) for section in ordered_sections]
    featured_task = _build_featured_payload()
    results = await asyncio.gather(*tasks, featured_task, return_exceptions=True)
    featured_result = results[-1]
    section_results = results[:-1]

    payload = []
    for section, result in zip(ordered_sections, section_results):
        conf = _section_conf(section)
        title = conf["title"] if conf else section
        if isinstance(result, Exception):
            payload.append({"key": section, "title": title, "page": 1, "total_pages": 0, "items": []})
        else:
            payload.append({
                "key": section,
                "title": result["title"],
                "page": 1,
                "total_pages": result["total_pages"],
                "items": [item for item in result["items"][:HOME_SECTION_LIMIT] if _has_minimum_catalog_fields(item)],
            })

    featured = None if isinstance(featured_result, Exception) else featured_result
    return {"ok": True, "featured": featured, "sections": payload}


@app.get("/api/catalog/list")
async def catalog_list(
    section: str = Query("dublados"),
    page: int = Query(1, ge=1),
):
    data = await _get_paginated_section_page(section, page)
    data["items"] = [item for item in data["items"] if _has_minimum_catalog_fields(item)]
    return {"ok": bool(data["items"]), **data}


# =============================================================================
# SEARCH
# =============================================================================

@app.get("/api/search")
async def api_search(
    q: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    limit: int = Query(GRID_PAGE_LIMIT, ge=1, le=60),
):
    query = q.strip()

    async def factory():
        raw_items = await search_anime(query)
        shaped = []
        for item in raw_items:
            anime_id = item.get("id") or ""
            title = item.get("title") or anime_id
            dubbed = bool(item.get("is_dubbed")) or _is_dubbed(anime_id, title)
            shaped.append({
                "id": anime_id,
                "title": title,
                "display_title": f"[{'DUB' if dubbed else 'LEG'}] {title}",
                "prefix": "DUB" if dubbed else "LEG",
                "is_dubbed": dubbed,
                "cover_url": item.get("cover_url") or item.get("banner_url") or "",
                "banner_url": item.get("banner_url") or item.get("cover_url") or "",
                "description": "",
                "genres": [],
                "score": None,
                "status": "",
                "episodes": None,
                "year": None,
                "studio": "",
            })
        return [item for item in shaped if _has_minimum_catalog_fields(item)]

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


# =============================================================================
# ANIME / EPISODES
# =============================================================================

@app.get("/api/anime/{anime_id}")
async def api_anime(anime_id: str):
    async def factory():
        data = await get_anime_details(anime_id)
        if not data:
            return None

        episodes_payload = await get_episodes(anime_id, 0, MAX_EPISODES_FETCH)
        episodes = _normalize_episodes(episodes_payload.get("all_items") or episodes_payload.get("items") or [])
        item = _shape_details(data, anime_id)
        if episodes:
            item["episodes"] = len(episodes)
        return {"item": item, "episodes": episodes}

    payload = await _cached(f"anime:{anime_id}", ANIME_TTL, factory)
    if not payload:
        raise HTTPException(status_code=404, detail="Anime não encontrado")

    return {"ok": True, **payload}


@app.get("/api/anime/{anime_id}/episode/{episode}")
async def api_episode(
    anime_id: str,
    episode: str,
    quality: str = Query("HD"),
    refresh: int = Query(0, description="Passe 1 para ignorar cache e buscar link novo"),
):
    quality = (quality or "HD").upper()
    cache_key = f"episode:{anime_id}:{episode}:{quality}"

    # Allow the frontend to force a fresh link fetch when the cached one has expired.
    if refresh:
        _invalidate_key(cache_key)

    async def factory():
        item = await get_episode_player(anime_id, episode, quality)
        if not item:
            return None

        payload = _shape_episode_payload(anime_id, episode, quality, item)

        # Automatic HD → SD fallback when the primary quality has no video.
        if not payload.get("video"):
            fallback_quality = "SD" if quality == "HD" else "HD"
            try:
                fallback_item = await get_episode_player(anime_id, episode, fallback_quality)
                if fallback_item:
                    fallback_payload = _shape_episode_payload(
                        anime_id,
                        episode,
                        fallback_quality,
                        fallback_item,
                    )
                    if fallback_payload.get("video"):
                        return fallback_payload
            except Exception:
                pass

        return payload

    payload = await _cached(cache_key, EPISODE_TTL, factory)
    if not payload:
        raise HTTPException(status_code=404, detail="Episódio não encontrado")

    return {"ok": True, "item": payload}


# =============================================================================
# PROGRESS
# =============================================================================

@app.post("/api/progress")
async def save_progress(payload: ProgressPayload):
    user_id = _clean(payload.user_id) or "guest"
    anime_id = _clean(payload.anime_id)
    episode = _clean(payload.episode)
    if not anime_id or not episode:
        raise HTTPException(status_code=400, detail="anime_id e episode são obrigatórios")

    bucket = _PROGRESS.setdefault(user_id, {})
    bucket[anime_id] = {
        "anime_id": anime_id,
        "episode": episode,
        "watched_seconds": max(0, int(payload.watched_seconds or 0)),
        "duration_seconds": max(0, int(payload.duration_seconds or 0)),
        "title": _clean(payload.title),
        "cover_url": payload.cover_url or "",
        "completed": bool(payload.completed),
        "updated_at": int(time.time()),
    }
    return {"ok": True, "item": bucket[anime_id]}


@app.get("/api/progress")
async def get_progress(user_id: str = Query("guest")):
    items = list((_PROGRESS.get(_clean(user_id) or "guest") or {}).values())
    items.sort(key=lambda item: item.get("updated_at", 0), reverse=True)
    return {"ok": True, "items": items}


# =============================================================================
# CACHE
# =============================================================================

@app.post("/api/cache/clear")
async def clear_cache(
    prefix: str = Query("", description="Prefixo das chaves a limpar; vazio = tudo")
):
    if prefix:
        _invalidate_prefix(prefix)
        cleared = f"prefix:{prefix}"
    else:
        count = len(_CACHE)
        _CACHE.clear()
        cleared = f"all:{count}"

    return {"ok": True, "cleared": cleared}


# =============================================================================
# STATIC / MINIAPP
# =============================================================================

if MINIAPP_DIR.exists():
    app.mount("/miniapp", StaticFiles(directory=str(MINIAPP_DIR)), name="miniapp")


@app.get("/app")
async def app_index():
    index_path = MINIAPP_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend not found")
    response = FileResponse(index_path)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/watch")
async def app_watch():
    watch_path = MINIAPP_DIR / "watch.html"
    if not watch_path.exists():
        raise HTTPException(status_code=404, detail="Watch page not found")
    return FileResponse(watch_path)


# =============================================================================
# PROXY STREAM  (global pooled client, proper Range support, smart retry)
# =============================================================================

async def _proxy_request_with_retry(
    method: str,
    url: str,
    headers: dict[str, str],
) -> httpx.Response:
    """
    Performs a streaming-compatible proxy request with automatic retry.

    Each attempt uses the shared global client so TCP connections are reused.
    On 4xx/5xx the function retries with exponential back-off up to
    PROXY_MAX_RETRIES times before raising.
    """
    client = await get_proxy_client()
    last_error: Exception | None = None

    for attempt in range(PROXY_MAX_RETRIES + 1):
        try:
            # Use stream=True so we never buffer the full response in RAM.
            response = await client.send(
                client.build_request(method, url, headers=headers),
                stream=True,
            )

            if response.status_code in (200, 206):
                return response

            # Non-retryable client errors.
            if 400 <= response.status_code < 500:
                await response.aclose()
                raise Exception(f"Upstream client error: {response.status_code}")

            await response.aclose()
            last_error = Exception(f"Upstream server error: {response.status_code}")

        except (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
            last_error = exc

        except Exception as exc:
            last_error = exc
            # Don't retry on non-network errors.
            if not isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
                break

        if attempt < PROXY_MAX_RETRIES:
            await asyncio.sleep(0.3 * (attempt + 1))

    raise last_error or Exception("Proxy: falha desconhecida após retries")


@app.api_route("/api/proxy-stream", methods=["GET", "HEAD"])
async def proxy_stream(
    request: Request,
    url: str = Query(..., min_length=1),
):
    range_header = request.headers.get("range", "")

    outgoing_headers: dict[str, str] = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "*/*",
        "Connection": "keep-alive",
        "Referer": BASE_URL + "/",
        "Origin": BASE_URL,
    }

    if range_header:
        outgoing_headers["Range"] = range_header

    method = "HEAD" if request.method == "HEAD" else "GET"

    try:
        upstream = await _proxy_request_with_retry(method, url, outgoing_headers)
    except Exception as exc:
        print(f"PROXY ERROR [{method}] {url!r}: {exc!r}")
        raise HTTPException(status_code=502, detail="Erro ao carregar vídeo")

    content_type = upstream.headers.get("content-type", "application/octet-stream")

    # Build the response headers we will forward to the browser.
    passthrough: dict[str, str] = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "Range, Content-Type, Accept",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
        "Accept-Ranges": upstream.headers.get("accept-ranges", "bytes"),
        "Content-Type": content_type,
        # Tell browsers/proxies not to cache proxy responses; caching is done at
        # the application layer (episode endpoint) instead.
        "Cache-Control": "no-store",
    }

    for header in ("content-length", "content-range", "content-disposition", "etag", "last-modified"):
        value = upstream.headers.get(header)
        if value:
            passthrough[header.title().replace("-", "-")] = value

    if method == "HEAD":
        await upstream.aclose()
        return Response(content=b"", status_code=upstream.status_code, headers=passthrough)

    status_code = upstream.status_code

    async def byte_stream():
        try:
            async for chunk in upstream.aiter_bytes(PROXY_CHUNK_SIZE):
                yield chunk
        except (httpx.ReadTimeout, httpx.RemoteProtocolError, GeneratorExit):
            pass
        finally:
            await upstream.aclose()

    return StreamingResponse(
        byte_stream(),
        status_code=status_code,
        headers=passthrough,
        media_type=content_type,
    )
