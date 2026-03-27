"""
webapp_api/app.py — API principal do QG BALTIGO
Coloque este arquivo em: ~/animesbaltigo/webapp_api/app.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ── Caminhos ─────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).resolve().parent.parent   # ~/animesbaltigo
MINIAPP_DIR = BASE_DIR / "miniapp"                     # ~/animesbaltigo/miniapp

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="QG BALTIGO API", docs_url="/docs", redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Importações dos serviços ──────────────────────────────────────────────────
from services.animefire_client import (
    search_anime,
    get_anime_details,
    get_episodes,
    get_episode_player,
    preload_popular_cache,
)

# ── Página raiz (index.html) ──────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def serve_index():
    index_file = MINIAPP_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file), media_type="text/html")
    raise HTTPException(status_code=404, detail=f"index.html não encontrado em {index_file}")


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    return {"ok": True}


# ── Catálogo: home ────────────────────────────────────────────────────────────
@app.get("/api/catalog/home")
async def catalog_home():
    """
    Retorna várias seções: recentes, em lançamento, top, legendados, dublados.
    Cada seção tem: key, title, items[], total_pages, has_next.
    """
    from services.recent_episodes_client import (
        get_recent_dubbed,
        get_recent_subbed,
        get_launching,
        get_top_animes,
    )

    async def _safe(coro, fallback=None):
        try:
            return await coro
        except Exception as exc:
            print(f"[HOME] erro: {repr(exc)}")
            return fallback or []

    dubbed_raw, subbed_raw, launching_raw, top_raw = await asyncio.gather(
        _safe(get_recent_dubbed()),
        _safe(get_recent_subbed()),
        _safe(get_launching()),
        _safe(get_top_animes()),
    )

    def _build_section(key, title, raw_items, page_size=12):
        items = (raw_items or [])[:page_size]
        return {
            "key": key,
            "title": title,
            "items": items,
            "total_pages": 1,
            "has_next": False,
        }

    sections = [
        _build_section("em_lancamento", "🔴 Em Lançamento Hoje", launching_raw),
        _build_section("atualizados",   "Recém Atualizados",    subbed_raw),
        _build_section("top",           "🔥 Os Mais Populares", top_raw),
        _build_section("dublados",      "Dublados",             dubbed_raw),
        _build_section("legendados",    "Legendados",           subbed_raw),
    ]

    # remove seções vazias
    sections = [s for s in sections if s["items"]]

    return {"ok": True, "sections": sections}


# ── Catálogo: listagem por seção ──────────────────────────────────────────────
@app.get("/api/catalog/list")
async def catalog_list(
    section: str = Query("dublados"),
    page:    int = Query(1, ge=1),
):
    from services.recent_episodes_client import (
        get_recent_dubbed,
        get_recent_subbed,
        get_launching,
        get_top_animes,
    )

    PAGE_SIZE = 24

    SECTION_MAP = {
        "dublados":      (get_recent_dubbed,  "Dublados"),
        "legendados":    (get_recent_subbed,  "Legendados"),
        "em_lancamento": (get_launching,      "Em Lançamento"),
        "atualizados":   (get_recent_subbed,  "Recém Atualizados"),
        "top":           (get_top_animes,     "Mais Populares"),
        "recentes":      (get_recent_subbed,  "Últimos Episódios"),
    }

    fetcher, title = SECTION_MAP.get(section, (get_recent_subbed, section.replace("_", " ").title()))

    try:
        all_items = await fetcher()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    all_items = all_items or []
    total = len(all_items)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
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


# ── Busca ─────────────────────────────────────────────────────────────────────
@app.get("/api/search")
async def search(
    q:    str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
):
    PAGE_SIZE = 24
    try:
        all_items = await search_anime(q)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    all_items = all_items or []
    total = len(all_items)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
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


# ── Detalhes do anime ─────────────────────────────────────────────────────────
@app.get("/api/anime/{anime_id:path}")
async def anime_detail(anime_id: str):
    # garante que não é uma rota de episódio (tratada abaixo)
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


# ── Player do episódio ────────────────────────────────────────────────────────
@app.get("/api/anime/{anime_id}/episode/{episode}")
async def episode_player(
    anime_id: str,
    episode:  str,
    quality:  str = Query("HD"),
):
    try:
        details  = await get_anime_details(anime_id)
        player   = await get_episode_player(anime_id, episode, quality)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    item = {**details, **player}

    return {
        "ok": True,
        "anime_id": anime_id,
        "episode": episode,
        "item": item,
    }


# ── Startup: pré-aquece cache ─────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup():
    asyncio.create_task(preload_popular_cache())
