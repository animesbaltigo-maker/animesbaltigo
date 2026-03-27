from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
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
    "User-Agent": "Mozilla/5.0",
    "Referer": BASE_URL,
}

HOME_SECTION_LIMIT = 10
GRID_PAGE_LIMIT = 20
MAX_EPISODES_FETCH = 400

SECTION_TTL = 60 * 15
RECENT_TTL = 60
SEARCH_TTL = 60 * 10
ANIME_TTL = 60 * 60 * 2
EPISODE_TTL = 60 * 60
HOME_TTL = 60 * 5

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
_START_TS = time.time()


class MissingMiniappFile(Exception):
    pass


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or value == "":
            return default
        return int(str(value).strip())
    except Exception:
        return default


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


def _cover_fallback() -> str:
    return "https://placehold.co/600x900/111111/ffffff?text=QG+BALTIGO"


def _banner_fallback() -> str:
    return "https://placehold.co/1280x720/111111/ffffff?text=QG+BALTIGO"


def _normalize_title(title: str, anime_id: str) -> str:
    value = _clean(title)
    if value:
        return value
    return anime_id.replace("-", " ").title()


def _infer_dubbed(title: str, anime_id: str) -> bool:
    check = f"{title} {anime_id}".lower()
    return "dublado" in check or "dub" in check


def _strip_audio_tag_from_title(title: str) -> str:
    value = _clean(title)
    value = re.sub(r"\s*\[(?:DUB|LEG)\]\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*-\s*(?:dublado|legendado)$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+\((?:dublado|legendado)\)$", "", value, flags=re.IGNORECASE)
    return _clean(value)


def _make_card_payload(data: dict[str, Any]) -> dict[str, Any]:
    anime_id = _clean(str(data.get("id") or ""))
    title = _normalize_title(str(data.get("title") or ""), anime_id)
    title = _strip_audio_tag_from_title(title)
    is_dubbed = bool(data.get("is_dubbed")) or _infer_dubbed(title, anime_id)
    cover = data.get("cover_url") or data.get("media_image_url") or data.get("banner_url") or ""
    banner = data.get("banner_url") or data.get("cover_url") or data.get("media_image_url") or ""
    episodes = _safe_int(data.get("episodes"))
    available = bool(episodes and episodes > 0)

    payload = {
        "id": anime_id,
        "title": title,
        "display_title": title,
        "audio_tag": "DUB" if is_dubbed else "LEG",
        "prefix": "DUB" if is_dubbed else "LEG",
        "is_dubbed": is_dubbed,
        "cover_url": cover or _cover_fallback(),
        "banner_url": banner or cover or _banner_fallback(),
        "description": _clean_description(str(data.get("description") or "")),
        "genres": _clean_genres(data.get("genres") or []),
        "score": data.get("score"),
        "status": _clean(str(data.get("status") or "")),
        "episodes": episodes,
        "year": _safe_int(data.get("season_year"), _safe_int(data.get("year"))),
        "studio": _clean(str(data.get("studio") or "")),
        "alt_titles": _clean_alt_titles(data.get("alt_titles") or []),
        "episode": data.get("episode"),
        "url": data.get("url") or "",
        "available": available,
        "watch_label": "Assistir agora" if available else "Não disponível no momento",
    }
    return payload


def _shape_details(data: dict[str, Any], fallback_id: str = "") -> dict[str, Any]:
    anime_id = data.get("id") or fallback_id
    shaped = _make_card_payload({**data, "id": anime_id})
    shaped["available"] = bool(shaped.get("episodes") and int(shaped["episodes"]) > 0)
    shaped["watch_label"] = "Assistir agora" if shaped["available"] else "Não disponível no momento"
    return shaped


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


def _drop_cache(prefixes: list[str] | None = None) -> None:
    if not prefixes:
        _CACHE.clear()
        return
    for key in list(_CACHE.keys()):
        if any(key.startswith(prefix) for prefix in prefixes):
            _CACHE.pop(key, None)


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

        raw = {
            "id": anime_id,
            "title": title,
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
        found[anime_id] = _make_card_payload(raw)

    return list(found.values())


def _section_conf(section: str) -> dict[str, str] | None:
    return SECTIONS.get((section or "").strip().lower())


def _section_url(slug: str, page: int) -> str:
    if page <= 1:
        return f"{BASE_URL}/{slug}"
    return f"{BASE_URL}/{slug}/{page}"


def _episode_number_sort_key(item: dict[str, Any]) -> tuple[int, str]:
    raw = str(item.get("episode") or "")
    numeric = _safe_int(re.sub(r"[^0-9]", "", raw), 10**9)
    return (numeric if numeric is not None else 10**9, raw)


def _shape_episode_item(anime_id: str, episode: dict[str, Any]) -> dict[str, Any]:
    ep = str(episode.get("episode") or "").strip()
    return {
        "anime_id": anime_id,
        "episode": ep,
        "title": _clean(str(episode.get("title") or f"Episódio {ep}")),
        "thumb": episode.get("thumb") or episode.get("image") or episode.get("cover") or "",
        "available": bool(ep),
    }


async def _get_recent_page(page: int) -> dict[str, Any]:
    async def factory():
        recent = await get_recent_episodes(limit=200)
        seen: set[str] = set()
        items: list[dict[str, Any]] = []

        for item in recent:
            anime_id = item.get("anime_id")
            if not anime_id or anime_id in seen:
                continue
            seen.add(anime_id)

            title = item.get("title") or anime_id.replace("-", " ").title()
            cover = item.get("thumb") or item.get("image") or item.get("cover") or item.get("cover_url") or ""
            if not cover:
                try:
                    details = await get_anime_details(anime_id)
                    cover = details.get("cover_url") or details.get("media_image_url") or details.get("banner_url") or ""
                except Exception:
                    cover = ""

            items.append(
                _make_card_payload(
                    {
                        "id": anime_id,
                        "title": title,
                        "cover_url": cover,
                        "banner_url": cover,
                        "episode": item.get("episode"),
                    }
                )
            )

        total = len(items)
        total_pages = max(1, (total + GRID_PAGE_LIMIT - 1) // GRID_PAGE_LIMIT)
        current_page = min(page, total_pages)
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
            "last_refreshed": int(time.time()),
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
            "last_refreshed": int(time.time()),
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
            "last_refreshed": int(time.time()),
        }

    return await _cached(f"page:{section}:{current_page}", SECTION_TTL, page_factory)


async def _load_home_payload() -> dict[str, Any]:
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

    payload = []
    for section in ordered_sections:
        page_data = await _get_paginated_section_page(section, 1)
        payload.append(
            {
                "key": section,
                "title": page_data["title"],
                "page": 1,
                "total_pages": page_data["total_pages"],
                "items": page_data["items"][:HOME_SECTION_LIMIT],
                "last_refreshed": page_data.get("last_refreshed", int(time.time())),
            }
        )

    hero_item = None
    for section in payload:
        if section["items"]:
            hero_item = section["items"][0]
            break

    return {
        "ok": True,
        "sections": payload,
        "hero": hero_item,
        "last_refreshed": int(time.time()),
        "recommended_poll_seconds": 90,
    }


async def _safe_get_anime_details(anime_id: str) -> dict[str, Any] | None:
    try:
        return await get_anime_details(anime_id)
    except Exception:
        return None


async def _safe_get_episodes(anime_id: str) -> list[dict[str, Any]]:
    try:
        episodes_payload = await get_episodes(anime_id, 0, MAX_EPISODES_FETCH)
    except Exception:
        return []
    episodes = episodes_payload.get("all_items") or episodes_payload.get("items") or []
    shaped = [_shape_episode_item(anime_id, ep) for ep in episodes if ep]
    shaped.sort(key=_episode_number_sort_key)
    return shaped


@app.on_event("startup")
async def _startup_refresh_task():
    async def refresher():
        while True:
            try:
                _drop_cache(["recentes:", "home:"])
            except Exception:
                pass
            await asyncio.sleep(60)

    asyncio.create_task(refresher())


@app.get("/")
def root():
    return {
        "ok": True,
        "name": "QG BALTIGO API",
        "sections": list(SECTIONS.keys()),
        "uptime_seconds": int(time.time() - _START_TS),
    }


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "uptime_seconds": int(time.time() - _START_TS),
        "cache_keys": len(_CACHE),
        "last_refreshed": int(time.time()),
    }


@app.get("/api/catalog/home")
async def catalog_home():
    return await _cached("home:payload", HOME_TTL, _load_home_payload)


@app.get("/api/catalog/list")
async def catalog_list(section: str = Query("dublados"), page: int = Query(1, ge=1)):
    data = await _get_paginated_section_page(section, page)
    return {"ok": bool(data["items"]), **data}


@app.get("/api/search")
async def api_search(
    q: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    limit: int = Query(GRID_PAGE_LIMIT, ge=1, le=60),
):
    query = q.strip()

    async def factory():
        raw_items = await search_anime(query)
        return [_make_card_payload(item) for item in raw_items if item.get("id")]

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
        "last_refreshed": int(time.time()),
    }


@app.get("/api/anime/{anime_id}")
async def api_anime(anime_id: str):
    async def factory():
        data = await _safe_get_anime_details(anime_id)
        if not data:
            return None
        episodes = await _safe_get_episodes(anime_id)
        item = _shape_details(data, anime_id)
        if not item.get("episodes"):
            item["episodes"] = len(episodes)
        item["available"] = bool(episodes)
        item["watch_label"] = "Assistir agora" if episodes else "Não disponível no momento"
        return {
            "item": item,
            "episodes": episodes,
            "last_refreshed": int(time.time()),
            "recommended_poll_seconds": 120,
        }

    payload = await _cached(f"anime:{anime_id}", ANIME_TTL, factory)
    if not payload:
        raise HTTPException(status_code=404, detail="Anime não encontrado")
    return {"ok": True, **payload}


@app.get("/api/anime/{anime_id}/episode/{episode}")
async def api_episode(anime_id: str, episode: str, quality: str = Query("HD")):
    normalized_quality = quality.upper().strip() or "HD"
    key = f"episode:{anime_id}:{episode}:{normalized_quality}"

    async def factory():
        item = await get_episode_player(anime_id, episode, normalized_quality)
        if not item:
            return None

        details = await _safe_get_anime_details(anime_id) or {}
        anime = _shape_details(details, anime_id) if details else _make_card_payload({"id": anime_id, "title": anime_id.replace('-', ' ').title()})
        thumb = item.get("thumb") or item.get("image") or anime.get("cover_url") or _cover_fallback()
        banner = anime.get("banner_url") or anime.get("cover_url") or _banner_fallback()
        available_qualities = item.get("available_qualities") or []
        available_qualities = [str(q).upper() for q in available_qualities if q]
        if normalized_quality not in available_qualities and normalized_quality:
            available_qualities.insert(0, normalized_quality)
        available_qualities = list(dict.fromkeys(available_qualities))

        video = item.get("video") or ""
        videos = item.get("videos") or {}
        has_video = bool(video or videos)

        return {
            "anime_id": anime_id,
            "anime": anime,
            "episode": str(episode),
            "video": video,
            "videos": videos,
            "quality": item.get("quality") or normalized_quality,
            "available_qualities": available_qualities,
            "title": item.get("title") or f"{anime.get('title', anime_id)} - Episódio {episode}",
            "description": _clean_description(item.get("description") or ""),
            "thumb": thumb,
            "banner": banner,
            "prev_episode": item.get("prev_episode"),
            "next_episode": item.get("next_episode"),
            "total_episodes": item.get("total_episodes") or anime.get("episodes"),
            "available": has_video,
            "watch_label": "Assistindo" if has_video else "Não disponível no momento",
            "last_refreshed": int(time.time()),
        }

    payload = await _cached(key, EPISODE_TTL, factory)
    if not payload:
        raise HTTPException(status_code=404, detail="Episódio não encontrado")
    return {"ok": True, "item": payload}


@app.post("/api/catalog/refresh")
async def api_refresh(scope: str = Query("all")):
    scope = (scope or "all").strip().lower()
    if scope == "home":
        _drop_cache(["home:"])
    elif scope == "recent":
        _drop_cache(["recentes:", "home:"])
    elif scope == "anime":
        _drop_cache(["anime:", "episode:"])
    else:
        _drop_cache()
    return {"ok": True, "scope": scope, "last_refreshed": int(time.time())}


BASE_DIR = Path(__file__).resolve().parent.parent
MINIAPP_DIR = BASE_DIR / "miniapp"
app.mount("/miniapp", StaticFiles(directory=str(MINIAPP_DIR)), name="miniapp")


def _file_or_raise(path: Path) -> Path:
    if not path.exists():
        raise MissingMiniappFile(str(path))
    return path


@app.exception_handler(MissingMiniappFile)
async def _missing_miniapp_handler(_, exc: MissingMiniappFile):
    return JSONResponse(status_code=404, content={"ok": False, "detail": f"Arquivo miniapp não encontrado: {exc}"})


@app.get("/app")
async def app_index():
    return FileResponse(_file_or_raise(MINIAPP_DIR / "index.html"))


@app.get("/watch")
async def app_watch():
    watch_file = MINIAPP_DIR / "watch.html"
    if watch_file.exists():
        return FileResponse(watch_file)
    return FileResponse(_file_or_raise(MINIAPP_DIR / "index.html"))
