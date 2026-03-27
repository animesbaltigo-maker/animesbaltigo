from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware

from services.animefire_client import get_episode_player

# =========================
# CONFIG
# =========================

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

VIDEO_TTL = 60 * 5

# GLOBAL HTTP CLIENT (POOLING)
http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(20.0, read=60.0),
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=40),
    headers={
        "User-Agent": "Mozilla/5.0",
    },
)

VIDEO_CACHE: dict[str, dict[str, Any]] = {}

# =========================
# CACHE
# =========================

def cache_get(key):
    item = VIDEO_CACHE.get(key)
    if not item:
        return None
    if time.time() - item["ts"] > VIDEO_TTL:
        VIDEO_CACHE.pop(key, None)
        return None
    return item["data"]

def cache_set(key, data):
    VIDEO_CACHE[key] = {
        "ts": time.time(),
        "data": data
    }

# =========================
# VIDEO FETCH
# =========================

async def fetch_video(anime_id, episode, quality, refresh=False):
    cache_key = f"{anime_id}:{episode}:{quality}"

    if not refresh:
        cached = cache_get(cache_key)
        if cached:
            return cached

    data = await get_episode_player(anime_id, episode, quality)

    if not data or not data.get("video"):
        # fallback automático
        if quality == "HD":
            data = await get_episode_player(anime_id, episode, "SD")

    if not data or not data.get("video"):
        raise HTTPException(404, "Video não encontrado")

    cache_set(cache_key, data)
    return data

# =========================
# PROXY STREAM
# =========================

async def stream_video(url: str, request: Request):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://animefire.io",
    }

    if "range" in request.headers:
        headers["Range"] = request.headers["range"]

    retries = 3

    for attempt in range(retries):
        try:
            response = await http_client.get(url, headers=headers, follow_redirects=True)

            if response.status_code in (200, 206):
                def iterator():
                    yield from response.iter_bytes(chunk_size=1024 * 1024)

                return StreamingResponse(
                    iterator(),
                    status_code=response.status_code,
                    headers={
                        "Content-Type": response.headers.get("content-type", "video/mp4"),
                        "Accept-Ranges": "bytes",
                        "Content-Length": response.headers.get("content-length", ""),
                        "Content-Range": response.headers.get("content-range", ""),
                    },
                )

        except Exception:
            await asyncio.sleep(0.5 * (attempt + 1))

    raise HTTPException(502, "Falha no streaming")

# =========================
# ENDPOINT
# =========================

@app.get("/episode")
async def episode(
    anime_id: str,
    episode: str,
    quality: str = "HD",
    refresh: int = 0,
):
    data = await fetch_video(anime_id, episode, quality, refresh=bool(refresh))
    return data


@app.get("/stream")
async def stream(request: Request, url: str):
    return await stream_video(url, request)
