# webapp_api/app.py

```python
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import BASE_DIR, DATA_DIR, HOME_SECTION_LIMIT, PREFERRED_CHAPTER_LANG
from services.catalog_client import (
    flatten_chapters,
    get_chapter_reader_payload,
    get_home_payload,
    get_recent_chapters,
    get_title_bundle,
    get_title_search,
    search_titles,
)
from services.media_pipeline import resolve_telegraph_asset_path
from services.metrics import get_last_read_entry, mark_chapter_read

MINIAPP_DIR = BASE_DIR / "miniapp"
PROGRESS_PATH = Path(DATA_DIR) / "miniapp_progress.json"

app = FastAPI(
    title="Mangas Baltigo API",
    description="API otimizada do miniapp de mangás",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


class ProgressPayload(BaseModel):
    user_id: str = Field(min_length=1)
    title_id: str = Field(min_length=1)
    title_name: str = ""
    chapter_id: str = Field(min_length=1)
    chapter_number: str = ""
    chapter_url: str = ""
    page_index: int = 0
    total_pages: int = 0


_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = asyncio.Lock()
_RECENT_TTL = 20
_HOME_TTL = 25
_TITLE_TTL = 90
_CHAPTER_TTL = 90
_SECTIONS_TTL = 25
_SEARCH_TTL = 20


def _now() -> float:
    return time.time()


def _cache_key(namespace: str, **kwargs: Any) -> str:
    raw = json.dumps({"ns": namespace, **kwargs}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


async def _cache_get(namespace: str, ttl: int, **kwargs: Any) -> Any | None:
    key = _cache_key(namespace, **kwargs)
    async with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None
        if entry["expires_at"] < _now():
            _CACHE.pop(key, None)
            return None
        return entry["value"]


async def _cache_set(namespace: str, value: Any, ttl: int, **kwargs: Any) -> Any:
    key = _cache_key(namespace, **kwargs)
    async with _CACHE_LOCK:
        _CACHE[key] = {
            "value": value,
            "expires_at": _now() + ttl,
        }
    return value


async def _cached(namespace: str, ttl: int, producer, **kwargs: Any) -> Any:
    cached = await _cache_get(namespace, ttl, **kwargs)
    if cached is not None:
        return cached
    value = await producer()
    return await _cache_set(namespace, value, ttl, **kwargs)


async def _stale_while_revalidate(namespace: str, ttl: int, stale_ttl: int, producer, **kwargs: Any) -> Any:
    key = _cache_key(namespace, **kwargs)
    async with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and entry["soft_expires_at"] >= _now():
            return entry["value"]
        if entry and entry["hard_expires_at"] >= _now():
            if not entry.get("refreshing"):
                entry["refreshing"] = True
                asyncio.create_task(_refresh_cache_entry(key, producer, ttl, stale_ttl))
            return entry["value"]

    value = await producer()
    async with _CACHE_LOCK:
        _CACHE[key] = {
            "value": value,
            "soft_expires_at": _now() + ttl,
            "hard_expires_at": _now() + stale_ttl,
            "refreshing": False,
        }
    return value


async def _refresh_cache_entry(key: str, producer, ttl: int, stale_ttl: int) -> None:
    try:
        value = await producer()
        async with _CACHE_LOCK:
            _CACHE[key] = {
                "value": value,
                "soft_expires_at": _now() + ttl,
                "hard_expires_at": _now() + stale_ttl,
                "refreshing": False,
            }
    except Exception:
        async with _CACHE_LOCK:
            if key in _CACHE:
                _CACHE[key]["refreshing"] = False


async def _invalidate_prefix(namespace: str) -> None:
    async with _CACHE_LOCK:
        for key in list(_CACHE.keys()):
            # chave sha1 nao permite prefixo real; limpamos tudo que for cache do app
            _CACHE.pop(key, None)


def _load_progress() -> dict[str, dict[str, Any]]:
    if not PROGRESS_PATH.exists():
        return {}
    try:
        return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_progress(data: dict[str, dict[str, Any]]) -> None:
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _progress_key(user_id: str, title_id: str) -> str:
    return f"{user_id}:{title_id}"


def _public_last_read(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if not entry:
        return None
    return {
        "title_id": entry.get("title_id") or "",
        "title_name": entry.get("title_name") or "",
        "chapter_id": entry.get("chapter_id") or "",
        "chapter_number": entry.get("chapter_number") or "",
        "updated_at": entry.get("updated_at") or "",
        "page_index": int(entry.get("page_index") or 0),
        "total_pages": int(entry.get("total_pages") or 0),
    }


def _public_chapter(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not item:
        return None
    return {
        "chapter_id": item.get("chapter_id") or "",
        "chapter_number": item.get("chapter_number") or "",
        "chapter_language": item.get("chapter_language") or "",
        "chapter_volume": item.get("chapter_volume") or "",
        "group_name": item.get("group_name") or "",
        "updated_at": item.get("updated_at") or "",
    }


def _has_real_chapter(item: dict[str, Any]) -> bool:
    return bool((item.get("chapter_id") or "").strip())


def _public_title_item(item: dict[str, Any]) -> dict[str, Any]:
    latest_value = item.get("latest_chapter")
    if isinstance(latest_value, dict):
        latest_value = latest_value.get("chapter_number") or latest_value.get("chapter_id") or ""

    return {
        "title_id": item.get("title_id") or "",
        "chapter_id": item.get("chapter_id") or "",
        "title": item.get("display_title") or item.get("title") or "",
        "cover_url": item.get("cover_url") or "",
        "background_url": item.get("background_url") or item.get("cover_url") or "",
        "status": item.get("status") or "",
        "rating": item.get("rating") or "",
        "updated_at": item.get("updated_at") or "",
        "latest_chapter": latest_value or "",
        "chapter_number": item.get("chapter_number") or latest_value or "",
        "adult": bool(item.get("adult")),
    }


def _sorted_filtered_chapters(bundle: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    chapters = flatten_chapters({"chapters": bundle.get("chapters") or []}, lang)
    clean = [c for c in chapters if _has_real_chapter(c)]

    def chapter_sort(item: dict[str, Any]) -> tuple[float, str]:
        raw = str(item.get("chapter_number") or "").strip()
        try:
            return (float(raw), item.get("updated_at") or "")
        except Exception:
            return (-1.0, item.get("updated_at") or "")

    clean.sort(key=chapter_sort, reverse=True)
    return clean


def _public_title_bundle(bundle: dict[str, Any], lang: str) -> dict[str, Any]:
    chapters = _sorted_filtered_chapters(bundle, lang)
    latest = next((item for item in chapters if item.get("chapter_id")), None)

    return {
        "title_id": bundle.get("title_id") or "",
        "title": bundle.get("display_title") or bundle.get("title") or "",
        "preferred_title": bundle.get("preferred_title") or "",
        "alt_titles": bundle.get("alt_titles") or [],
        "description": bundle.get("description") or bundle.get("anilist_description") or "",
        "cover_url": bundle.get("cover_url") or "",
        "background_url": bundle.get("background_url") or bundle.get("cover_url") or "",
        "banner_url": bundle.get("banner_url") or bundle.get("background_url") or bundle.get("cover_url") or "",
        "cover_color": bundle.get("cover_color") or "",
        "status": bundle.get("status") or bundle.get("anilist_status") or "",
        "rating": bundle.get("rating") or "",
        "genres": bundle.get("genres") or [],
        "authors": bundle.get("authors") or [],
        "published": bundle.get("published") or "",
        "languages": bundle.get("languages") or [],
        "total_chapters": len(chapters),
        "anilist_url": bundle.get("anilist_url") or "",
        "anilist_score": bundle.get("anilist_score") or 0,
        "anilist_format": bundle.get("anilist_format") or "",
        "anilist_status": bundle.get("anilist_status") or "",
        "anilist_chapters": bundle.get("anilist_chapters") or 0,
        "anilist_volumes": bundle.get("anilist_volumes") or 0,
        "adult": bool(bundle.get("adult")),
        "chapters": [_public_chapter(item) for item in chapters],
        "latest_chapter": _public_chapter(latest or bundle.get("latest_chapter")),
    }


def _public_reader_payload(payload: dict[str, Any]) -> dict[str, Any]:
    images = [img for img in (payload.get("images") or []) if str(img or "").strip()]
    return {
        "title_id": payload.get("title_id") or "",
        "title": payload.get("title") or "",
        "chapter_id": payload.get("chapter_id") or "",
        "chapter_number": payload.get("chapter_number") or "",
        "chapter_language": payload.get("chapter_language") or "",
        "chapter_volume": payload.get("chapter_volume") or "",
        "cover_url": payload.get("cover_url") or "",
        "image_count": len(images),
        "images": images,
        "total_chapters": payload.get("total_chapters") or 0,
        "previous_chapter": _public_chapter(payload.get("previous_chapter")),
        "next_chapter": _public_chapter(payload.get("next_chapter")),
    }


def _normalize_query(text: str) -> str:
    import unicodedata
    import re

    text = unicodedata.normalize("NFKD", (text or "").strip().lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _search_score(query: str, item: dict[str, Any]) -> tuple[int, int, int]:
    q = _normalize_query(query)
    title = _normalize_query(item.get("title") or item.get("preferred_title") or item.get("display_title") or "")
    tags = [_normalize_query(tag) for tag in (item.get("genres") or [])]
    if not q:
        return (0, 0, 0)
    if title == q:
        return (500, 0, -len(title))
    if title.startswith(q):
        return (400, 0, -len(title))
    if q in title:
        return (300, 0, -len(title))
    if any(q in tag for tag in tags):
        return (220, 0, -len(title))
    overlap = len(set(q.split()) & set(title.split()))
    return (100 + overlap * 10, 0, -len(title))


async def _search_with_suggestions(query: str, limit: int) -> dict[str, Any]:
    raw_results = await _cached(
        "search",
        _SEARCH_TTL,
        lambda: search_titles(query, limit=max(20, limit * 3)),
        query=query,
        limit=max(20, limit * 3),
    )

    candidates = []
    for item in raw_results:
        if not item.get("title_id"):
            continue
        candidates.append(item)

    ranked = sorted(candidates, key=lambda item: _search_score(query, item), reverse=True)
    ranked = ranked[:limit]

    if ranked:
        return {
            "query": query,
            "results": [_public_title_item(item) for item in ranked],
            "suggestions": [],
        }

    home = await _home_payload(limit=max(10, limit))
    pool = []
    for key in ("featured", "popular", "recent_titles", "latest_titles"):
        pool.extend(home.get(key) or [])

    seen: set[str] = set()
    dedup_pool = []
    for item in pool:
        title_id = item.get("title_id") or ""
        if not title_id or title_id in seen:
            continue
        seen.add(title_id)
        dedup_pool.append(item)

    suggestions = sorted(dedup_pool, key=lambda item: _search_score(query, item), reverse=True)[:6]
    return {
        "query": query,
        "results": [],
        "suggestions": [_public_title_item(item) for item in suggestions if item.get("title_id")],
    }


async def _home_payload(limit: int) -> dict[str, Any]:
    async def producer() -> dict[str, Any]:
        payload, recent_chapters = await asyncio.gather(
            get_home_payload(limit=limit),
            get_recent_chapters(limit=min(limit * 2, 24)),
        )

        featured = [_public_title_item(item) for item in (payload.get("featured") or []) if _has_real_chapter(item)]
        popular = [_public_title_item(item) for item in (payload.get("popular") or []) if _has_real_chapter(item)]
        recent_titles = [_public_title_item(item) for item in (payload.get("recent_titles") or []) if _has_real_chapter(item)]
        latest_titles = [_public_title_item(item) for item in (payload.get("latest_titles") or []) if _has_real_chapter(item)]

        public_recent_chapters = []
        seen_chapters: set[str] = set()
        for item in recent_chapters:
            chapter_id = item.get("chapter_id") or ""
            if not chapter_id or chapter_id in seen_chapters:
                continue
            seen_chapters.add(chapter_id)
            public_recent_chapters.append(_public_title_item(item))

        latest_titles.sort(key=lambda item: (item.get("updated_at") or "", item.get("latest_chapter") or ""), reverse=True)
        public_recent_chapters.sort(key=lambda item: (item.get("updated_at") or "", item.get("chapter_number") or item.get("latest_chapter") or ""), reverse=True)

        return {
            "featured": featured[:limit],
            "popular": popular[:limit],
            "recent_titles": recent_titles[:limit],
            "latest_titles": latest_titles[:limit],
            "recent_chapters": public_recent_chapters[: max(limit, 12)],
        }

    return await _stale_while_revalidate("home", _HOME_TTL, _HOME_TTL * 3, producer, limit=limit)


async def _title_payload(title_id: str, lang: str, user_id: str = "") -> dict[str, Any]:
    async def producer() -> dict[str, Any]:
        bundle = await get_title_bundle(title_id, lang)
        public_bundle = _public_title_bundle(bundle, lang)
        if user_id:
            public_bundle["last_read"] = _public_last_read(get_last_read_entry(user_id, public_bundle["title_id"]))
        return public_bundle

    return await _cached("title", _TITLE_TTL, producer, title_id=title_id, lang=lang, user_id=user_id)


async def _chapter_payload(chapter_id: str, lang: str) -> dict[str, Any]:
    async def producer() -> dict[str, Any]:
        payload = await get_chapter_reader_payload(chapter_id, lang)
        return _public_reader_payload(payload)

    return await _cached("chapter", _CHAPTER_TTL, producer, chapter_id=chapter_id, lang=lang)


@app.get("/api/ping")
async def ping() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/home")
async def api_home(limit: int = Query(HOME_SECTION_LIMIT, ge=4, le=24)):
    return await _home_payload(limit=limit)


@app.get("/api/search")
async def api_search(q: str = Query("", min_length=1), limit: int = Query(12, ge=1, le=24)):
    return await _search_with_suggestions(q, limit)


@app.get("/api/sections/{section_name}")
async def api_section(section_name: str, limit: int = Query(12, ge=1, le=24)):
    async def producer() -> dict[str, Any]:
        if section_name == "recent_chapters":
            items = await get_recent_chapters(limit=max(limit, 12))
            clean = [_public_title_item(item) for item in items if _has_real_chapter(item)]
            clean.sort(key=lambda item: (item.get("updated_at") or "", item.get("chapter_number") or item.get("latest_chapter") or ""), reverse=True)
            return {"items": clean[:limit]}

        section_map = {
            "featured": "getFeatured",
            "popular": "getPopular",
            "recent_titles": "getRecentRead",
            "latest_titles": "getLatestTable",
        }
        search_type = section_map.get(section_name)
        if not search_type:
            raise HTTPException(status_code=404, detail="Secao nao encontrada.")

        extra = {"search_time": "week"} if search_type == "getRecentRead" else {}
        items = await get_title_search(search_type, limit=max(limit, 16), **extra)
        clean = [_public_title_item(item) for item in items if _has_real_chapter(item)]
        if section_name in {"latest_titles", "recent_titles"}:
            clean.sort(key=lambda item: (item.get("updated_at") or "", item.get("latest_chapter") or ""), reverse=True)
        return {"items": clean[:limit]}

    return await _stale_while_revalidate("section", _SECTIONS_TTL, _SECTIONS_TTL * 3, producer, section_name=section_name, limit=limit)


@app.get("/api/title/{title_id}")
async def api_title(title_id: str, user_id: str = Query(""), lang: str = Query(PREFERRED_CHAPTER_LANG)):
    try:
        return await _title_payload(title_id, lang, user_id)
    except Exception as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/title/{title_id}/chapters")
async def api_title_chapters(title_id: str, lang: str = Query(PREFERRED_CHAPTER_LANG)):
    try:
        bundle = await _title_payload(title_id, lang)
        return {
            "title_id": bundle["title_id"],
            "title": bundle.get("title") or "",
            "chapters": bundle.get("chapters") or [],
        }
    except Exception as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/chapter/{chapter_id}")
async def api_chapter(chapter_id: str, lang: str = Query(PREFERRED_CHAPTER_LANG)):
    try:
        return await _chapter_payload(chapter_id, lang)
    except Exception as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/progress")
async def api_get_progress(user_id: str = Query(...), title_id: str = Query(...)):
    data = _load_progress()
    return _public_last_read(data.get(_progress_key(user_id, title_id))) or {}


@app.post("/api/progress")
async def api_save_progress(payload: ProgressPayload):
    data = _load_progress()
    key = _progress_key(payload.user_id, payload.title_id)
    stored = payload.model_dump()
    data[key] = stored
    _save_progress(data)

    mark_chapter_read(
        user_id=payload.user_id,
        title_id=payload.title_id,
        chapter_id=payload.chapter_id,
        chapter_number=payload.chapter_number,
        title_name=payload.title_name,
        chapter_url=payload.chapter_url,
    )

    await _invalidate_prefix("cache")
    return {"ok": True}


@app.post("/api/refresh")
async def api_refresh():
    await _invalidate_prefix("cache")
    return {"ok": True}


@app.get("/api/media/telegraph/{asset_key}/{asset_name}")
async def api_telegraph_media(asset_key: str, asset_name: str):
    try:
        asset_path = resolve_telegraph_asset_path(asset_key, asset_name)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    return FileResponse(
        asset_path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/")
async def root():
    return FileResponse(MINIAPP_DIR / "index.html")


@app.middleware("http")
async def add_perf_headers(request: Request, call_next):
    start = time.perf_counter()
    response: Response = await call_next(request)
    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
    response.headers["X-Response-Time"] = f"{elapsed_ms}ms"
    response.headers["Cache-Control"] = response.headers.get("Cache-Control", "no-store")
    return response


if MINIAPP_DIR.exists():
    app.mount("/miniapp", StaticFiles(directory=MINIAPP_DIR, html=True), name="miniapp")
```

---

# miniapp/index.html

```html
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover" />
  <title>BALTIGO — Mangás</title>
  <meta name="theme-color" content="#05060f" />
  <meta name="mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,700;1,9..40,400&display=swap" rel="stylesheet" />
  <style>
    :root {
      --bg-void:#05060f;
      --bg-base:#080b18;
      --bg-raised:#0d1128;
      --bg-float:#111630;
      --bg-glass:rgba(255,255,255,0.045);
      --bg-glass-2:rgba(255,255,255,0.07);
      --violet:#7c3aed;
      --violet-2:#9b5cf6;
      --cyan:#06b6d4;
      --rose:#f43f5e;
      --green:#22c55e;
      --amber:#f59e0b;
      --grad-brand:linear-gradient(135deg,#7c3aed 0%,#2563eb 60%,#06b6d4 100%);
      --grad-card:linear-gradient(160deg,rgba(124,58,237,0.12) 0%,rgba(6,182,212,0.06) 100%);
      --grad-shine:linear-gradient(105deg,transparent 40%,rgba(255,255,255,0.06) 50%,transparent 60%);
      --text:#eef2ff;
      --text-2:#94a3b8;
      --text-3:#64748b;
      --border:rgba(255,255,255,0.08);
      --border-2:rgba(255,255,255,0.13);
      --border-v:rgba(124,58,237,0.45);
      --shadow-sm:0 2px 12px rgba(0,0,0,0.4);
      --shadow-md:0 8px 32px rgba(0,0,0,0.5);
      --shadow-lg:0 20px 60px rgba(0,0,0,0.6);
      --shadow-v:0 8px 32px rgba(124,58,237,0.3);
      --header-h:64px;
      --max-w:1440px;
      --r-xs:8px;--r-sm:12px;--r-md:18px;--r-lg:24px;--r-xl:32px;
      --safe-top:env(safe-area-inset-top,0px);
      --safe-bottom:env(safe-area-inset-bottom,0px);
      --font-d:'Syne',sans-serif;
      --font-b:'DM Sans',sans-serif;
      --ease:cubic-bezier(0.22,1,0.36,1);
    }
    *,*::before,*::after{box-sizing:border-box;-webkit-tap-highlight-color:transparent;margin:0;padding:0}
    html{scroll-behavior:smooth}
    body{
      font-family:var(--font-b);
      font-size:15px;
      color:var(--text);
      background:var(--bg-void);
      min-height:100dvh;
      overflow-x:hidden;
      -webkit-font-smoothing:antialiased;
    }
    body::before{
      content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
      background:
        radial-gradient(ellipse 80% 50% at 110% -10%,rgba(124,58,237,0.18) 0%,transparent 60%),
        radial-gradient(ellipse 60% 40% at -10% 110%,rgba(6,182,212,0.12) 0%,transparent 60%),
        radial-gradient(ellipse 40% 30% at 50% 50%,rgba(37,99,235,0.07) 0%,transparent 70%);
    }
    a{color:inherit;text-decoration:none}
    img{display:block;max-width:100%}
    button{font-family:var(--font-b);cursor:pointer;border:none;outline:none;background:none;color:inherit}
    input{font-family:var(--font-b);border:none;outline:none;background:none;color:inherit}
    .app{position:relative;z-index:1;min-height:100dvh}
    .hidden{display:none !important}
    .topbar{
      position:fixed;top:0;left:0;right:0;z-index:110;
      height:calc(var(--header-h) + var(--safe-top));padding-top:var(--safe-top);
      transition:transform .3s var(--ease),opacity .3s;
    }
    .topbar.reader-hide{transform:translateY(-100%);opacity:0;pointer-events:none}
    .topbar::before{
      content:'';position:absolute;inset:0;
      background:linear-gradient(180deg,rgba(5,6,15,0.98) 0%,rgba(5,6,15,0.82) 70%,transparent 100%);
      backdrop-filter:blur(20px) saturate(1.4);
      -webkit-backdrop-filter:blur(20px) saturate(1.4);
      border-bottom:1px solid var(--border);
    }
    .topbar-inner{
      position:relative;max-width:var(--max-w);margin:0 auto;height:var(--header-h);
      padding:0 20px;display:flex;align-items:center;gap:14px;
    }
    .brand{display:flex;align-items:center;gap:10px;flex:1;min-width:0}
    .brand-logo{
      width:38px;height:38px;border-radius:10px;background:var(--grad-brand);display:grid;place-items:center;
      font-family:var(--font-d);font-size:17px;font-weight:800;letter-spacing:-0.03em;box-shadow:var(--shadow-v);
      flex-shrink:0;position:relative;overflow:hidden;
    }
    .brand-logo::after{content:'';position:absolute;inset:0;background:var(--grad-shine)}
    .brand-info{min-width:0}.brand-name{font-family:var(--font-d);font-size:16px;font-weight:800;letter-spacing:-0.02em;line-height:1;background:var(--grad-brand);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}.brand-sub{font-size:11px;color:var(--text-3);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .topbar-actions{display:flex;gap:8px;align-items:center}
    .icon-btn{width:40px;height:40px;border-radius:var(--r-sm);background:var(--bg-glass);border:1px solid var(--border);display:grid;place-items:center;font-size:16px;transition:background .2s var(--ease),border-color .2s,transform .15s var(--ease);flex-shrink:0}
    .icon-btn:hover{background:var(--bg-glass-2);border-color:var(--border-2);transform:translateY(-1px)}
    .icon-btn:active{transform:scale(0.94)}
    .page{max-width:var(--max-w);margin:0 auto;padding:calc(var(--header-h) + var(--safe-top) + 20px) 20px calc(40px + var(--safe-bottom));animation:page-in .35s var(--ease) both}
    @keyframes page-in{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:translateY(0)}}
    .hero-banner{position:relative;border-radius:var(--r-xl);overflow:hidden;margin-bottom:28px;min-height:360px;display:flex;align-items:flex-end;border:1px solid var(--border);background:var(--grad-card);box-shadow:var(--shadow-lg)}
    .hero-bg{position:absolute;inset:0;background-size:cover;background-position:center top;transition:transform .7s var(--ease),opacity .3s var(--ease);filter:saturate(1.08)}
    .hero-banner:hover .hero-bg{transform:scale(1.04)}
    .hero-grad{position:absolute;inset:0;background:linear-gradient(0deg,rgba(5,6,15,0.97) 0%,rgba(5,6,15,0.82) 28%,rgba(5,6,15,0.35) 60%,rgba(5,6,15,0.06) 100%),linear-gradient(90deg,rgba(5,6,15,0.88) 0%,rgba(5,6,15,0.55) 36%,transparent 80%)}
    .hero-body{position:relative;z-index:2;padding:32px;width:100%;display:flex;align-items:flex-end;justify-content:space-between;gap:24px;flex-wrap:wrap}
    .hero-copy{max-width:650px}
    .hero-eyebrow{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--cyan);margin-bottom:10px}
    .hero-eyebrow::before{content:'';width:6px;height:6px;border-radius:50%;background:var(--cyan);box-shadow:0 0 8px var(--cyan);animation:pdot 2s ease-in-out infinite}
    @keyframes pdot{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(.7)}}
    .hero-title{font-family:var(--font-d);font-size:clamp(28px,4vw,54px);font-weight:800;line-height:1.0;letter-spacing:-.03em;margin-bottom:10px;max-width:620px;text-wrap:balance}
    .hero-meta{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
    .pill{display:inline-flex;align-items:center;gap:6px;font-size:11px;color:var(--text-2);font-weight:700;background:var(--bg-glass-2);border:1px solid var(--border);padding:5px 10px;border-radius:999px;backdrop-filter:blur(6px)}
    .pill.live{color:#a7f3d0}.pill.score{color:#fcd34d}
    .hero-desc{color:var(--text-2);font-size:13px;line-height:1.65;max-width:560px;margin-bottom:16px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
    .hero-actions{display:flex;gap:10px;flex-wrap:wrap}
    .hero-cover{width:190px;aspect-ratio:2/3;border-radius:24px;overflow:hidden;border:1px solid var(--border);box-shadow:var(--shadow-md);background:var(--bg-float);flex-shrink:0}
    .hero-cover img{width:100%;height:100%;object-fit:cover}
    .btn{display:inline-flex;align-items:center;gap:8px;font-family:var(--font-b);font-size:14px;font-weight:700;border-radius:var(--r-sm);padding:0 18px;height:44px;border:1px solid var(--border);background:var(--bg-glass);color:var(--text);cursor:pointer;transition:all .2s var(--ease);white-space:nowrap;position:relative;overflow:hidden}
    .btn::after{content:'';position:absolute;inset:0;background:var(--grad-shine);opacity:0;transition:opacity .2s}
    .btn:hover{transform:translateY(-2px);background:var(--bg-glass-2);border-color:var(--border-2)}
    .btn:hover::after{opacity:1}.btn:active{transform:scale(0.97)}.btn:disabled{opacity:.5;pointer-events:none}
    .btn-primary{background:var(--grad-brand);border:none;box-shadow:var(--shadow-v);color:#fff}.btn-primary:hover{opacity:.95;box-shadow:0 12px 40px rgba(124,58,237,0.45)}
    .btn-sm{height:36px;padding:0 14px;font-size:13px;border-radius:var(--r-xs)}
    .btn-xs{height:30px;padding:0 10px;font-size:12px;border-radius:6px}
    .btn-full{width:100%;justify-content:center}
    .continue-wrap,.search-section,.section{margin-bottom:28px}
    .section-head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;margin-bottom:14px}
    .section-label{display:flex;align-items:center;gap:12px}.section-accent{width:6px;height:36px;border-radius:999px;background:var(--grad-brand);box-shadow:0 0 16px rgba(124,58,237,.45);flex-shrink:0}
    .section-title{font-family:var(--font-d);font-weight:800;font-size:26px;letter-spacing:-.03em;line-height:1}
    .section-count{color:var(--text-3);font-size:12px;margin-top:5px;font-weight:700}
    .cw-scroll{display:flex;gap:12px;overflow-x:auto;padding-bottom:6px;scrollbar-width:none}.cw-scroll::-webkit-scrollbar{display:none}
    .cw-card{flex-shrink:0;width:210px;border-radius:var(--r-md);overflow:hidden;background:var(--bg-raised);border:1px solid var(--border);cursor:pointer;transition:transform .22s var(--ease),border-color .22s}
    .cw-card:hover{transform:translateY(-4px);border-color:var(--border-v)}
    .cw-thumb{position:relative;aspect-ratio:16/9;background:var(--bg-float);overflow:hidden}.cw-thumb img{width:100%;height:100%;object-fit:cover;transition:transform .4s var(--ease)}
    .cw-card:hover .cw-thumb img{transform:scale(1.06)}
    .cw-prog-bar{position:absolute;bottom:0;left:0;right:0;height:3px;background:rgba(255,255,255,0.12)}
    .cw-prog-fill{height:100%;background:var(--grad-brand);border-radius:2px}
    .cw-badge{position:absolute;top:7px;right:7px;background:rgba(5,6,15,0.82);font-size:10px;font-weight:700;color:var(--text-2);padding:3px 7px;border-radius:6px;backdrop-filter:blur(6px)}
    .cw-info{padding:10px 12px 12px}.cw-title{font-size:13px;font-weight:800;display:-webkit-box;-webkit-line-clamp:1;-webkit-box-orient:vertical;overflow:hidden;margin-bottom:4px}.cw-sub{font-size:11px;color:var(--text-3)}
    .search-row{display:flex;gap:10px;align-items:stretch;margin-bottom:14px}
    .search-bar{flex:1;position:relative;display:flex;align-items:center;gap:12px;height:52px;background:var(--bg-raised);border:1px solid var(--border);border-radius:var(--r-md);padding:0 18px;transition:border-color .2s var(--ease),box-shadow .2s,background .2s}
    .search-bar:focus-within{border-color:var(--border-v);box-shadow:0 0 0 3px rgba(124,58,237,0.15);background:#101631}.search-ico{font-size:18px;color:var(--text-3)}.search-input{flex:1;font-size:15px;color:var(--text)}.search-input::placeholder{color:var(--text-3)}
    .chip-row{display:flex;gap:8px;flex-wrap:wrap}.chip{height:34px;padding:0 12px;border-radius:999px;background:var(--bg-glass);border:1px solid var(--border);color:var(--text-2);font-size:12px;font-weight:700;display:inline-flex;align-items:center;gap:6px;transition:all .2s var(--ease)}.chip:hover{transform:translateY(-1px);background:var(--bg-glass-2);color:var(--text);border-color:var(--border-2)}
    .card-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:16px}
    .manga-card{border-radius:22px;overflow:hidden;background:var(--bg-raised);border:1px solid var(--border);cursor:pointer;transition:transform .22s var(--ease),border-color .22s,box-shadow .22s;position:relative}
    .manga-card:hover{transform:translateY(-6px);border-color:var(--border-v);box-shadow:var(--shadow-md)}
    .card-thumb{position:relative;aspect-ratio:2/3;background:var(--bg-float);overflow:hidden}.card-thumb img{width:100%;height:100%;object-fit:cover;transition:transform .4s var(--ease),filter .2s}.manga-card:hover .card-thumb img{transform:scale(1.05)}
    .card-gradient{position:absolute;inset:0;background:linear-gradient(180deg,transparent 25%,rgba(5,6,15,.18) 50%,rgba(5,6,15,.75) 100%)}
    .card-badge{position:absolute;top:8px;left:8px;z-index:2;background:rgba(5,6,15,.8);color:var(--text-2);border:1px solid var(--border);backdrop-filter:blur(8px);padding:4px 8px;border-radius:999px;font-size:10px;font-weight:800;letter-spacing:.04em;text-transform:uppercase}.card-badge.adult{color:#fecdd3;border-color:rgba(244,63,94,.25)}
    .card-body{padding:12px}.card-title{font-size:14px;font-weight:800;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:36px}.card-meta{margin-top:7px;color:var(--text-3);font-size:12px;line-height:1.45}
    .page-panel{background:rgba(255,255,255,0.03);border:1px solid var(--border);border-radius:28px;overflow:hidden;box-shadow:var(--shadow-lg)}
    .detail-hero{position:relative;min-height:420px;overflow:hidden}.detail-bg{position:absolute;inset:0;background-size:cover;background-position:center top;filter:saturate(1.05);transform:scale(1.02)}.detail-grad{position:absolute;inset:0;background:linear-gradient(0deg,rgba(5,6,15,0.98) 0%,rgba(5,6,15,0.94) 26%,rgba(5,6,15,0.58) 60%,rgba(5,6,15,0.15) 100%),linear-gradient(90deg,rgba(5,6,15,0.98) 0%,rgba(5,6,15,0.84) 30%,rgba(5,6,15,0.3) 70%,transparent 100%)}
    .detail-inner{position:relative;z-index:2;display:grid;grid-template-columns:240px minmax(0,1fr);gap:28px;align-items:end;padding:32px;min-height:420px}
    .detail-cover{width:240px;aspect-ratio:2/3;border-radius:24px;overflow:hidden;background:var(--bg-float);border:1px solid var(--border);box-shadow:var(--shadow-lg)}.detail-cover img{width:100%;height:100%;object-fit:cover}
    .detail-title{font-family:var(--font-d);font-size:clamp(28px,4vw,50px);font-weight:800;letter-spacing:-.03em;line-height:1;margin-bottom:12px;text-wrap:balance}.detail-alt{color:var(--text-3);font-size:13px;line-height:1.5;margin-bottom:14px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.detail-meta,.genre-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}.detail-desc{color:var(--text-2);line-height:1.75;font-size:14px;max-width:880px;margin-top:8px}.detail-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
    .detail-body{padding:24px 24px 28px}.chapter-panel{background:var(--bg-raised);border:1px solid var(--border);border-radius:24px;padding:18px}.chapter-tools{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}.chapter-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(135px,1fr));gap:10px;margin-top:14px}.chapter-btn{min-height:54px;border-radius:16px;background:rgba(255,255,255,0.04);border:1px solid var(--border);color:var(--text);padding:10px 12px;text-align:left;transition:all .18s var(--ease)}.chapter-btn:hover{transform:translateY(-2px);background:rgba(255,255,255,0.06);border-color:var(--border-v)}.chapter-btn.read{border-color:rgba(34,197,94,.28);background:rgba(34,197,94,.10)}.chapter-n{font-size:13px;font-weight:800;line-height:1.2}.chapter-meta{font-size:11px;color:var(--text-3);margin-top:4px;line-height:1.35}
    .reader-shell{max-width:1000px;margin:0 auto}.reader-top{position:sticky;top:calc(var(--header-h) + var(--safe-top) + 8px);z-index:50;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 16px;border-radius:18px;margin-bottom:16px;background:rgba(8,11,24,.84);border:1px solid var(--border);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);box-shadow:var(--shadow-md);flex-wrap:wrap}.reader-info h1{font-family:var(--font-d);font-size:clamp(20px,2.5vw,30px);line-height:1;letter-spacing:-.03em;margin-bottom:6px}.reader-sub{color:var(--text-3);font-size:12px;font-weight:700}.reader-actions{display:flex;gap:8px;flex-wrap:wrap}.reader-images{display:grid;gap:14px;padding-bottom:24px}.reader-page{width:min(100%,960px);margin:0 auto;border-radius:22px;overflow:hidden;background:var(--bg-float);border:1px solid var(--border);box-shadow:var(--shadow-md);min-height:280px}.reader-page img{width:100%;height:auto;display:block}.reader-bottom-nav{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin-top:10px;padding-bottom:24px}
    .empty-state{border:1px solid var(--border);border-radius:24px;background:rgba(255,255,255,0.03);padding:28px;color:var(--text-2);text-align:center;min-height:120px;display:grid;place-items:center;line-height:1.7}
    .overlay{position:fixed;inset:0;z-index:160;display:flex;align-items:center;justify-content:center;background:rgba(5,6,15,.72);backdrop-filter:blur(16px) saturate(1.2);-webkit-backdrop-filter:blur(16px) saturate(1.2);transition:opacity .25s var(--ease),visibility .25s var(--ease);opacity:1;visibility:visible}.overlay.hidden{opacity:0;visibility:hidden;pointer-events:none}
    .loader-card{width:min(92vw,420px);padding:24px;border-radius:24px;background:rgba(13,17,40,.92);border:1px solid var(--border-2);box-shadow:var(--shadow-lg);text-align:center}.loader-orb{width:72px;height:72px;margin:0 auto 14px;border-radius:50%;background:var(--grad-brand);position:relative;box-shadow:0 0 40px rgba(124,58,237,.38);animation:orb 1.25s infinite alternate ease-in-out}.loader-orb::after{content:'📚';position:absolute;inset:0;display:grid;place-items:center;font-size:28px;filter:drop-shadow(0 3px 10px rgba(0,0,0,.35))}@keyframes orb{from{transform:translateY(0) scale(1)}to{transform:translateY(-6px) scale(1.04)}}.loader-title{font-family:var(--font-d);font-size:22px;font-weight:800;letter-spacing:-.03em}.loader-text{margin-top:8px;color:var(--text-2);line-height:1.6;font-size:13px}
    .toast{position:fixed;left:50%;bottom:calc(18px + var(--safe-bottom));transform:translateX(-50%) translateY(20px);z-index:170;min-width:min(90vw,340px);max-width:min(92vw,500px);padding:12px 14px;border-radius:16px;background:rgba(13,17,40,.95);border:1px solid var(--border-2);color:var(--text);box-shadow:var(--shadow-lg);opacity:0;pointer-events:none;transition:all .25s var(--ease);text-align:center;font-size:13px;line-height:1.45}.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
    @media (max-width:980px){.detail-inner{grid-template-columns:1fr;align-items:start}.detail-cover{width:210px}.hero-body{padding:24px}.hero-cover{width:150px}}
    @media (max-width:720px){.page{padding-left:14px;padding-right:14px}.topbar-inner{padding:0 14px}.hero-banner{min-height:420px}.hero-cover{display:none}.section-title{font-size:22px}.card-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.chapter-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.reader-top{top:calc(var(--header-h) + var(--safe-top) + 4px)}}
    @media (max-width:460px){.card-grid{gap:12px}.chapter-grid{grid-template-columns:1fr 1fr}.hero-title{font-size:30px}.detail-title{font-size:30px}.loader-card{padding:20px}}
  </style>
</head>
<body>
  <div class="app">
    <header class="topbar" id="topbar">
      <div class="topbar-inner">
        <button class="icon-btn" id="backBtn" title="Voltar">←</button>
        <div class="brand" id="brandTap">
          <div class="brand-logo">M</div>
          <div class="brand-info">
            <div class="brand-name">Mangas Baltigo</div>
            <div class="brand-sub" id="brandSub">Catálogo premium, leitura contínua e capítulos rápidos</div>
          </div>
        </div>
        <div class="topbar-actions">
          <button class="icon-btn" id="searchFocusBtn" title="Buscar">⌕</button>
          <button class="icon-btn" id="homeBtn" title="Início">⌂</button>
        </div>
      </div>
    </header>

    <main id="homePage" class="page">
      <section class="hero-banner" id="heroBanner">
        <div class="hero-bg" id="heroBg"></div>
        <div class="hero-grad"></div>
        <div class="hero-body">
          <div class="hero-copy">
            <div class="hero-eyebrow">Leitura em destaque</div>
            <h1 class="hero-title" id="heroTitle">Descubra mangás em uma experiência feita para Telegram.</h1>
            <div class="hero-meta" id="heroMeta">
              <span class="pill live">⚡ Busca rápida</span>
              <span class="pill">📚 Leitura contínua</span>
              <span class="pill">🆕 Capítulos recentes</span>
            </div>
            <p class="hero-desc" id="heroDesc">Pesquise obras, continue de onde parou, abra capítulos direto no leitor vertical e navegue sem fricção.</p>
            <div class="hero-actions">
              <button class="btn btn-primary" id="heroPrimaryBtn">Explorar agora</button>
              <button class="btn" id="heroSecondaryBtn">Capítulos recentes</button>
            </div>
          </div>
          <div class="hero-cover" id="heroCoverWrap">
            <img id="heroCover" alt="Destaque" />
          </div>
        </div>
      </section>

      <section class="continue-wrap" id="continueWrap">
        <div class="section-head">
          <div class="section-label">
            <div class="section-accent"></div>
            <div>
              <div class="section-title">Continue lendo</div>
              <div class="section-count">Seu progresso recente</div>
            </div>
          </div>
        </div>
        <div class="cw-scroll" id="continueList"></div>
      </section>

      <section class="search-section">
        <div class="search-row">
          <form class="search-bar" id="searchForm">
            <div class="search-ico">🔎</div>
            <input class="search-input" id="searchInput" type="search" placeholder="Busque um mangá pelo nome ou tag..." autocomplete="off" />
          </form>
          <button class="btn btn-primary" id="searchBtn">Buscar</button>
        </div>
        <div class="chip-row">
          <button class="chip" data-jump="featuredSection">Destaques</button>
          <button class="chip" data-jump="popularSection">Populares</button>
          <button class="chip" data-jump="recentTitlesSection">Recentes</button>
          <button class="chip" data-jump="latestSection">Atualizados</button>
          <button class="chip" data-jump="recentChaptersSection">Novos capítulos</button>
        </div>
      </section>

      <section class="section hidden" id="searchResultsSection">
        <div class="section-head">
          <div class="section-label">
            <div class="section-accent"></div>
            <div>
              <div class="section-title">Resultados</div>
              <div class="section-count" id="searchMeta">Buscando...</div>
            </div>
          </div>
          <button class="btn btn-sm" id="clearSearchBtn">Limpar</button>
        </div>
        <div id="searchGrid"></div>
      </section>

      <section class="section" id="featuredSection"><div class="section-head"><div class="section-label"><div class="section-accent"></div><div><div class="section-title">Destaques</div><div class="section-count" id="featuredCount">Carregando...</div></div></div></div><div id="featuredGrid"></div></section
```
