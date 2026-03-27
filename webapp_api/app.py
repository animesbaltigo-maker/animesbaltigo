from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from services.animefire_client import (
    get_anime_details,
    get_episode_player,
    get_episodes,
    search_anime,
)
from services.recent_episodes_client import get_recent_episodes

# ================= CONFIG =================

BASE_DIR = Path(__file__).resolve().parent.parent
MINIAPP_DIR = BASE_DIR / "miniapp"

app = FastAPI(title="Baltigo API", version="5.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADERS = {
    "User-Agent": "Mozilla/5.0",
}

# CACHE
_CACHE: dict[str, dict[str, Any]] = {}
_LOCKS: dict[str, asyncio.Lock] = {}

EPISODE_TTL = 45
ANIME_TTL = 1800
SEARCH_TTL = 300

# PROXY
PROXY_TIMEOUT = httpx.Timeout(8.0, read=20.0)
PROXY_LIMITS = httpx.Limits(max_connections=200, max_keepalive_connections=100)
PROXY_CHUNK = 1024 * 1024
PROXY_RETRIES = 1

_proxy_client: httpx.AsyncClient | None = None

# ================= CACHE =================

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

async def _cached(key: str, ttl: int, fn):
    data = _cache_get(key, ttl)
    if data is not None:
        return data

    lock = _LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        data = _cache_get(key, ttl)
        if data is not None:
            return data
        result = await fn()
        return _cache_set(key, result)

# ================= STARTUP =================

@app.on_event("startup")
async def startup():
    global _proxy_client
    _proxy_client = httpx.AsyncClient(
        timeout=PROXY_TIMEOUT,
        limits=PROXY_LIMITS,
        follow_redirects=True,
        headers={"User-Agent": HEADERS["User-Agent"]},
    )

@app.on_event("shutdown")
async def shutdown():
    global _proxy_client
    if _proxy_client:
        await _proxy_client.aclose()

# ================= ROOT =================

@app.get("/")
def root():
    return {"ok": True}

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "cache": len(_CACHE),
        "proxy": _proxy_client is not None
    }

# ================= STATIC =================

if MINIAPP_DIR.exists():
    app.mount("/miniapp", StaticFiles(directory=str(MINIAPP_DIR)), name="miniapp")

@app.get("/app")
async def index():
    file = MINIAPP_DIR / "index.html"
    return FileResponse(file)

# ================= SEARCH =================

@app.get("/api/search")
async def search(q: str = Query(...)):
    async def run():
        items = await search_anime(q)
        return items

    return {
        "ok": True,
        "items": await _cached(f"s:{q}", SEARCH_TTL, run)
    }

# ================= ANIME =================

@app.get("/api/anime/{anime_id}")
async def anime(anime_id: str):
    async def run():
        data = await get_anime_details(anime_id)
        eps = await get_episodes(anime_id, 0, 400)
        return {
            "item": data,
            "episodes": eps.get("all_items") or eps.get("items") or []
        }

    result = await _cached(f"a:{anime_id}", ANIME_TTL, run)

    if not result:
        raise HTTPException(404)

    return {"ok": True, **result}

# ================= EPISODE =================

@app.get("/api/anime/{anime_id}/episode/{episode}")
async def episode(
    anime_id: str,
    episode: str,
    quality: str = Query("HD"),
    refresh: int = Query(0),
):
    q = quality.upper()

    async def resolve():
        data = await get_episode_player(anime_id, episode, q)
        if not data:
            return None

        video = data.get("video")

        if not video:
            alt = "SD" if q == "HD" else "HD"
            data = await get_episode_player(anime_id, episode, alt)

        return data

    key = f"ep:{anime_id}:{episode}:{q}"

    if refresh:
        result = await resolve()
        _CACHE.pop(key, None)
    else:
        result = await _cached(key, EPISODE_TTL, resolve)

    if not result:
        raise HTTPException(404)

    return {"ok": True, "item": result}

# ================= PROXY =================

async def _fetch(method: str, url: str, headers: dict):
    if not _proxy_client:
        raise Exception("proxy off")

    last = None

    for _ in range(PROXY_RETRIES + 1):
        try:
            r = await _proxy_client.request(method, url, headers=headers)
            if r.status_code in (200, 206):
                return r
            last = Exception(r.status_code)
        except Exception as e:
            last = e

        await asyncio.sleep(0.2)

    raise last

@app.api_route("/api/proxy-stream", methods=["GET", "HEAD"])
async def proxy(request: Request, url: str):
    range_header = request.headers.get("range")

    headers = {
        "Accept": "*/*",
        "Connection": "keep-alive",
    }

    if range_header:
        headers["Range"] = range_header

    try:
        method = "HEAD" if request.method == "HEAD" else "GET"
        r = await _fetch(method, url, headers)

        h = {
            "Content-Type": r.headers.get("content-type", "video/mp4"),
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=30",
        }

        if "content-range" in r.headers:
            h["Content-Range"] = r.headers["content-range"]

        if "content-length" in r.headers:
            h["Content-Length"] = r.headers["content-length"]

        if request.method == "HEAD":
            return Response(status_code=r.status_code, headers=h)

        async def stream():
            async for chunk in r.aiter_bytes(PROXY_CHUNK):
                yield chunk

        return StreamingResponse(stream(), headers=h)

    except Exception as e:
        print("PROXY ERROR:", e)
        raise HTTPException(502)
