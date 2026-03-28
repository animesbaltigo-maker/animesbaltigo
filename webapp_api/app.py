from __future__ import annotations

import asyncio
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
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
MAX_EPISODES_FETCH = 1200

SECTION_TTL = 60 * 15
RECENT_TTL = 60
SEARCH_TTL = 60 * 10
ANIME_TTL = 60 * 60 * 2
EPISODE_TTL = 60 * 4
HERO_TTL = 60 * 10
PROGRESS_TTL = 60 * 60 * 24 * 30

PROXY_TIMEOUT = httpx.Timeout(connect=8.0, read=60.0, write=60.0, pool=10.0)
PROXY_MAX_RETRIES = 3
PROXY_CHUNK_SIZE = 1024 * 1024
PROXY_KEEPALIVE_EXPIRY = 30
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
    description="API do miniapp com catálogo, episódios, progresso e proxy de vídeo",
    version="5.0.0",
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
_PROXY_CLIENT: httpx.AsyncClient | None = None


class ProgressPayload(BaseModel):
    user_id: str
    anime_id: str
    episode: str
    watched_seconds: int = 0
    duration_seconds: int = 0
    title: str = ""
    cover_url: str = ""
    completed: bool = False


def _get_proxy_limits() -> httpx.Limits:
    return httpx.Limits(
        max_connections=PROXY_MAX_CONNECTIONS,
        max_keepalive_connections=PROXY_MAX_KEEPALIVE,
        keepalive_expiry=PROXY_KEEPALIVE_EXPIRY,
    )


async def get_proxy_client() -> httpx.AsyncClient:
    global _PROXY_CLIENT
    if _PROXY_CLIENT is None or _PROXY_CLIENT.is_closed:
        _PROXY_CLIENT = httpx.AsyncClient(
            timeout=PROXY_TIMEOUT,
            follow_redirects=True,
            limits=_get_proxy_limits(),
            http2=False,
        )
    return _PROXY_CLIENT


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _clean_description(text: str) -> str:
    value = text or ""
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
        value = re.sub(pattern, "", value, flags=re.IGNORECASE | re.DOTALL)
    return _clean(value)


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


def _extract_listing_cards(page_html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(page_html, "html.parser")
    found: dict[str, dict[str, Any]] = {}

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


def _shape_details(data: dict[str, Any], fallback_id: str = "") -> dict[str, Any]:
    anime_id = data.get("id") or fallback_id
    title = data.get("title") or anime_id.replace("-", " ").title()
    dubbed = bool(data.get("is_dubbed")) or _is_dubbed(anime_id, title)
    return {
        "id": anime_id,
        "title": title,
        "display_title": f"[{'DUB' if dubbed else 'LEG'}] {title}",
        "prefix": "DUB" if dubbed else "LEG",
        "is_dubbed": dubbed,
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


def _shape_episode_payload(anime_id: str, episode: str, quality: str, item: dict[str, Any]) -> dict[str, Any]:
    video = item.get("video") or ""
    videos = item.get("videos") or {}
    available_qualities = item.get("available_qualities") or []

    if not available_qualities and isinstance(videos, dict):
        available_qualities = list(videos.keys())

    if not video and isinstance(videos, dict):
        normalized_quality = (quality or "HD").upper()
        video = videos.get(normalized_quality) or videos.get("HD") or videos.get("SD") or ""

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
        score += 5
    return score


def _normalize_episode_item(item: dict[str, Any]) -> dict[str, Any] | None:
    raw_number = item.get("number") or item.get("episode") or item.get("ep")
    parsed = _parse_episode_number(raw_number)
    if parsed is None:
        return None

    return {
        **item,
        "number": str(parsed.normalize()),
        "numeric": float(parsed),
        "title": _clean(item.get("title") or ""),
        "description": _clean_description(item.get("description") or ""),
    }


def _normalize_episodes(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    best_by_number: dict[str, dict[str, Any]] = {}

    for item in items or []:
        normalized = _normalize_episode_item(item)
        if not normalized:
            continue

        key = normalized["number"]
        previous = best_by_number.get(key)
        if not previous or _episode_score(normalized) > _episode_score(previous):
            best_by_number[key] = normalized

    episodes = sorted(best_by_number.values(), key=lambda item: item["numeric"])

    for idx, item in enumerate(episodes):
        item["prev_episode"] = episodes[idx - 1]["number"] if idx > 0 else None
        item["next_episode"] = episodes[idx + 1]["number"] if idx < len(episodes) - 1 else None
        item["total_episodes"] = len(episodes)

    return episodes


def _is_valid_catalog_item(item: dict[str, Any]) -> bool:
    if not item.get("id"):
        return False
    if not _clean(item.get("title") or ""):
        return False
    if not (item.get("cover_url") or item.get("banner_url")):
        return False
    return True


def _filter_valid_items(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [item for item in (items or []) if _is_valid_catalog_item(item)]


async def _get_recent_page(page: int) -> dict[str, Any]:
    async def factory():
        recent = await get_recent_episodes(limit=250)
        seen: set[str] = set()
        items: list[dict[str, Any]] = []

        for item in recent:
            anime_id = item.get("anime_id")
            if not anime_id or anime_id in seen:
                continue
            seen.add(anime_id)

            title = item.get("title") or anime_id.replace("-", " ").title()
            dubbed = _is_dubbed(anime_id, title)
            cover = item.get("thumb") or item.get("image") or item.get("cover") or item.get("cover_url") or ""

            if not cover:
                try:
                    details = await get_anime_details(anime_id)
                    cover = details.get("cover_url") or details.get("media_image_url") or details.get("banner_url") or ""
                except Exception:
                    cover = ""

            items.append(
                {
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
                }
            )

        items = _filter_valid_items(items)
        total = len(items)
        total_pages = max(1, (total + GRID_PAGE_LIMIT - 1) // GRID_PAGE_LIMIT) if total else 1
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


async def _get_paginated_section_page(section: str, page: int) -> dict[str, Any]:
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
        page_html = meta["first_html"] if current_page == 1 else await _get(_section_url(slug, current_page))
        items = _filter_valid_items(_extract_listing_cards(page_html))
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


@app.on_event("startup")
async def _startup_tasks():
    await get_proxy_client()

    async def _recent_refresher():
        while True:
            try:
                _invalidate_prefix("recentes:")
                _invalidate_prefix("page:recentes")
                _invalidate_key("hero:home")
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


@app.get("/")
def root():
    return {
        "ok": True,
        "name": "QG BALTIGO API",
        "version": "5.0.0",
        "sections": list(SECTIONS.keys()),
    }


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "cache_entries": len(_CACHE),
        "progress_users": len(_PROGRESS),
        "timestamp": int(time.time()),
    }


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
    results = await asyncio.gather(*tasks, return_exceptions=True)

    payload = []
    for section, result in zip(ordered_sections, results):
        conf = _section_conf(section)
        title = conf["title"] if conf else section

        if isinstance(result, Exception):
            payload.append({
                "key": section,
                "title": title,
                "page": 1,
                "total_pages": 0,
                "items": [],
            })
        else:
            payload.append({
                "key": section,
                "title": result["title"],
                "page": 1,
                "total_pages": result["total_pages"],
                "items": result["items"][:HOME_SECTION_LIMIT],
            })

    return {"ok": True, "sections": payload}


@app.get("/api/catalog/list")
async def catalog_list(
    section: str = Query("dublados"),
    page: int = Query(1, ge=1),
):
    data = await _get_paginated_section_page(section, page)
    return {"ok": bool(data["items"]), **data}


@app.get("/api/catalog/hero")
async def catalog_hero():
    async def factory():
        top = await _get_paginated_section_page("top", 1)
        recent = await _get_recent_page(1)

        candidates = [*(top.get("items") or []), *(recent.get("items") or [])]
        candidates = _filter_valid_items(candidates)
        hero = candidates[0] if candidates else None
        if not hero:
            return {"ok": False, "item": None}

        try:
            details = await get_anime_details(hero["id"])
            item = _shape_details(details, hero["id"])
        except Exception:
            item = hero

        return {"ok": True, "item": item}

    return await _cached("hero:home", HERO_TTL, factory)


@app.get("/api/search")
async def api_search(
    q: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    limit: int = Query(GRID_PAGE_LIMIT, ge=1, le=60),
):
    query = q.strip()

    async def factory():
        raw_items = await search_anime(query)
        shaped: list[dict[str, Any]] = []
        for item in raw_items:
            anime_id = item.get("id") or ""
            title = item.get("title") or anime_id
            dubbed = bool(item.get("is_dubbed")) or _is_dubbed(anime_id, title)
            shaped.append(
                {
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
                }
            )
        return shaped

    shaped = _filter_valid_items(await _cached(f"search:{query.lower()}", SEARCH_TTL, factory))

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
async def api_anime(anime_id: str):
    async def factory():
        data = await get_anime_details(anime_id)
        if not data:
            return None

        episodes_payload = await get_episodes(anime_id, 0, MAX_EPISODES_FETCH)
        episodes_raw = episodes_payload.get("all_items") or episodes_payload.get("items") or []
        episodes = _normalize_episodes(episodes_raw)

        item = _shape_details(data, anime_id)
        item["episodes"] = len(episodes)

        return {
            "item": item,
            "episodes": episodes,
        }

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

    if refresh:
        _invalidate_key(cache_key)

    async def factory():
        item = await get_episode_player(anime_id, episode, quality)
        if not item:
            return None

        payload = _shape_episode_payload(anime_id, episode, quality, item)

        if not payload.get("video"):
            fallback_quality = "SD" if quality == "HD" else "HD"
            try:
                fallback_item = await get_episode_player(anime_id, episode, fallback_quality)
                if fallback_item:
                    fallback_payload = _shape_episode_payload(anime_id, episode, fallback_quality, fallback_item)
                    if fallback_payload.get("video"):
                        return fallback_payload
            except Exception:
                pass

        return payload

    payload = await _cached(cache_key, EPISODE_TTL, factory)
    if not payload:
        raise HTTPException(status_code=404, detail="Episódio não encontrado")

    return {"ok": True, "item": payload}


@app.post("/api/progress")
async def save_progress(payload: ProgressPayload):
    user = _PROGRESS.setdefault(payload.user_id, {})
    user[payload.anime_id] = {
        **payload.model_dump(),
        "updated_at": int(time.time()),
        "expires_at": int(time.time()) + PROGRESS_TTL,
    }
    return {"ok": True}


@app.get("/api/progress/{user_id}")
async def get_progress(user_id: str):
    items = list((_PROGRESS.get(user_id) or {}).values())
    now = int(time.time())
    items = [item for item in items if item.get("expires_at", now + 1) > now]
    items.sort(key=lambda item: item.get("updated_at", 0), reverse=True)
    return {"ok": True, "items": items}


@app.post("/api/cache/clear")
async def clear_cache(prefix: str = Query("", description="Prefixo das chaves a limpar; vazio = tudo")):
    if prefix:
        _invalidate_prefix(prefix)
        cleared = f"prefix:{prefix}"
    else:
        count = len(_CACHE)
        _CACHE.clear()
        cleared = f"all:{count}"

    return {"ok": True, "cleared": cleared}


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


async def _proxy_request_with_retry(method: str, url: str, headers: dict[str, str]) -> httpx.Response:
    client = await get_proxy_client()
    last_error: Exception | None = None

    for attempt in range(PROXY_MAX_RETRIES + 1):
        try:
            request = client.build_request(method=method, url=url, headers=headers)
            response = await client.send(request, stream=True)
            if response.status_code >= 500:
                await response.aclose()
                raise httpx.HTTPStatusError("upstream server error", request=request, response=response)
            return response
        except Exception as exc:
            last_error = exc
            if attempt >= PROXY_MAX_RETRIES:
                break
            await asyncio.sleep(0.35 * (attempt + 1))

    raise HTTPException(status_code=502, detail=f"Falha ao abrir stream: {last_error}")


def _build_proxy_headers(request: Request) -> dict[str, str]:
    headers: dict[str, str] = {
        "User-Agent": HEADERS["User-Agent"],
        "Referer": BASE_URL,
        "Origin": BASE_URL,
        "Accept": request.headers.get("accept", "*/*"),
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
    }
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]
    return headers


@app.api_route("/api/proxy", methods=["GET", "HEAD"])
async def proxy_stream(request: Request, url: str = Query(...)):
    method = request.method.upper()
    upstream_headers = _build_proxy_headers(request)
    upstream_response = await _proxy_request_with_retry(method, url, upstream_headers)

    passthrough_headers = {
        key: value
        for key, value in upstream_response.headers.items()
        if key.lower() in {
            "accept-ranges",
            "cache-control",
            "content-length",
            "content-range",
            "content-type",
            "date",
            "etag",
            "expires",
            "last-modified",
        }
    }
    passthrough_headers["Access-Control-Allow-Origin"] = "*"
    passthrough_headers["Access-Control-Expose-Headers"] = "Content-Length, Content-Range, Accept-Ranges, Content-Type"

    if method == "HEAD":
        await upstream_response.aclose()
        return Response(status_code=upstream_response.status_code, headers=passthrough_headers)

    async def body_iterator():
        try:
            async for chunk in upstream_response.aiter_bytes(PROXY_CHUNK_SIZE):
                if chunk:
                    yield chunk
        finally:
            await upstream_response.aclose()

    return StreamingResponse(
        body_iterator(),
        status_code=upstream_response.status_code,
        headers=passthrough_headers,
        media_type=upstream_response.headers.get("content-type", "application/octet-stream"),
    )
