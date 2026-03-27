from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from services.animefire_client import (
    search_anime,
    get_anime_details,
    get_episodes,
    get_episode_player,
    preload_popular_cache,
)
from services.recent_episodes_client import get_recent_episodes

BASE_DIR = Path(__file__).resolve().parent.parent
MINIAPP_DIR = BASE_DIR / "miniapp"

app = FastAPI(title="QG BALTIGO API", docs_url="/docs", redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if MINIAPP_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(MINIAPP_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def serve_index():
    index_file = MINIAPP_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file), media_type="text/html")
    raise HTTPException(status_code=404, detail=f"index.html não encontrado em {index_file}")


@app.get("/api/health")
async def health():
    return {"ok": True}


def _normalize_card(item: dict) -> dict:
    anime_id = item.get("anime_id") or item.get("id") or ""
    title = item.get("title") or anime_id.replace("-", " ").title()
    episode = item.get("episode")
    thumb = (
        item.get("thumb")
        or item.get("cover_url")
        or item.get("banner_url")
        or item.get("image")
        or ""
    )

    is_dubbed = "dub" in title.lower() or "dublado" in title.lower()

    return {
        "id": anime_id,
        "anime_id": anime_id,
        "title": title,
        "display_title": title,
        "episode": episode,
        "cover_url": thumb,
        "banner_url": thumb,
        "thumb": thumb,
        "prefix": "DUB" if is_dubbed else "LEG",
        "is_dubbed": is_dubbed,
        "status": "",
        "year": None,
        "episodes": None,
        "score": None,
        "genres": [],
        "studio": "",
        "description": "",
    }


@app.get("/api/catalog/home")
async def catalog_home():
    try:
        recent_raw = await get_recent_episodes(limit=120)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    recent_raw = recent_raw or []
    normalized = [_normalize_card(x) for x in recent_raw if x.get("anime_id") or x.get("id")]

    dubbed = [x for x in normalized if x["is_dubbed"]]
    subbed = [x for x in normalized if not x["is_dubbed"]]

    sections = [
        {
            "key": "recentes",
            "title": "Últimos Episódios",
            "items": normalized[:12],
            "total_pages": 1,
            "has_next": False,
        },
        {
            "key": "dublados",
            "title": "Dublados",
            "items": dubbed[:12],
            "total_pages": 1,
            "has_next": False,
        },
        {
            "key": "legendados",
            "title": "Legendados",
            "items": subbed[:12],
            "total_pages": 1,
            "has_next": False,
        },
        {
            "key": "atualizados",
            "title": "Recém Atualizados",
            "items": normalized[:12],
            "total_pages": 1,
            "has_next": False,
        },
    ]

    sections = [s for s in sections if s["items"]]

    return {"ok": True, "sections": sections}


@app.get("/api/catalog/list")
async def catalog_list(
    section: str = Query("recentes"),
    page: int = Query(1, ge=1),
):
    PAGE_SIZE = 24

    try:
        recent_raw = await get_recent_episodes(limit=300)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    recent_raw = recent_raw or []
    normalized = [_normalize_card(x) for x in recent_raw if x.get("anime_id") or x.get("id")]

    dubbed = [x for x in normalized if x["is_dubbed"]]
    subbed = [x for x in normalized if not x["is_dubbed"]]

    section_map = {
        "recentes": ("Últimos Episódios", normalized),
        "atualizados": ("Recém Atualizados", normalized),
        "dublados": ("Dublados", dubbed),
        "legendados": ("Legendados", subbed),
    }

    title, all_items = section_map.get(section, ("Últimos Episódios", normalized))

    total = len(all_items)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE) if total else 1
    page = max(1, min(page, total_pages))
    start = (page - 1) * PAGE_SIZE
    items = all_items[start : start + PAGE_SIZE]

    return {
        "ok": True,
        "section": section,
        "title": title,
        "page": page,
        "total_pages": total_pages,
        "count": total,
        "has_next": page < total_pages,
        "items": items,
    }


@app.get("/api/search")
async def search(
    q: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
):
    PAGE_SIZE = 24

    try:
        all_items = await search_anime(q)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    all_items = all_items or []
    total = len(all_items)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE) if total else 1
    page = max(1, min(page, total_pages))
    start = (page - 1) * PAGE_SIZE
    items = all_items[start : start + PAGE_SIZE]

    return {
        "ok": True,
        "query": q,
        "page": page,
        "total_pages": total_pages,
        "count": total,
        "has_next": page < total_pages,
        "items": items,
    }


@app.get("/api/anime/{anime_id:path}")
async def anime_detail(anime_id: str):
    if "/episode/" in anime_id:
        raise HTTPException(status_code=404, detail="Use /api/anime/{id}/episode/{ep}")

    try:
        item = await get_anime_details(anime_id)
        eps_payload = await get_episodes(anime_id, 0, 3000)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "ok": True,
        "item": item,
        "episodes": eps_payload.get("items", []),
        "total_episodes": eps_payload.get("total", 0),
    }


@app.get("/api/anime/{anime_id}/episode/{episode}")
async def episode_player(
    anime_id: str,
    episode: str,
    quality: str = Query("HD"),
):
    try:
        details = await get_anime_details(anime_id)
        player = await get_episode_player(anime_id, episode, quality)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    item = {**details, **player}

    return {
        "ok": True,
        "anime_id": anime_id,
        "episode": episode,
        "item": item,
    }


@app.on_event("startup")
async def on_startup():
    try:
        asyncio.create_task(preload_popular_cache())
    except Exception:
        pass
