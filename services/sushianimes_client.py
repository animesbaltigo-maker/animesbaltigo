import asyncio
import html
import json
import re
import time
from urllib.parse import quote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from core.http_client import get_http_client

BASE_URL = "https://sushianimes.com.br"

_SEARCH_CACHE = {}
_DETAILS_CACHE = {}
_EPISODES_CACHE = {}
_PLAYER_CACHE = {}

_SEARCH_CACHE_TTL = 1800
_DETAILS_CACHE_TTL = 21600
_EPISODES_CACHE_TTL = 600
_PLAYER_CACHE_TTL = 21600

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": f"{BASE_URL}/",
    "Origin": BASE_URL,
}

HTTP_SEMAPHORE = asyncio.Semaphore(20)


def _now() -> float:
    return time.time()


def _cache_get(cache: dict, key: str, ttl: int):
    item = cache.get(key)
    if not item:
        return None
    if _now() - item["time"] > ttl:
        cache.pop(key, None)
        return None
    return item["data"]


def _cache_set(cache: dict, key: str, data) -> None:
    cache[key] = {"time": _now(), "data": data}


def _clean(value: str | None) -> str:
    text = html.unescape(str(value or ""))
    if "Ã" in text or "Â" in text:
        try:
            text = text.encode("latin-1").decode("utf-8")
        except UnicodeError:
            pass
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _slug_from_url(url: str) -> str:
    path = urlsplit(url).path.strip("/")
    if path.startswith("anime/"):
        return path.split("/", 1)[1].strip("/")
    return path.rsplit("/", 1)[-1].strip("/")


def _normalize_anime_id(anime_id: str) -> str:
    value = str(anime_id or "").strip().strip("/")
    if value.startswith("http://") or value.startswith("https://"):
        value = _slug_from_url(value)
    if value.startswith("anime/"):
        value = value.split("/", 1)[1].strip("/")
    return value


def _anime_url(anime_id: str) -> str:
    return f"{BASE_URL}/anime/{quote(_normalize_anime_id(anime_id), safe='-')}"


def _parse_episode_ref(value: str) -> tuple[int, int]:
    raw = str(value or "").strip()
    match = re.search(r"^[sS]?(\d+)[eE:.-](\d+)$", raw)
    if match:
        return int(match.group(1)), int(match.group(2))

    match = re.search(r"^(\d+)-season-(\d+)-episode$", raw)
    if match:
        return int(match.group(1)), int(match.group(2))

    digits = re.search(r"\d+", raw)
    return 1, int(digits.group(0)) if digits else 1


def _episode_key(season: int, episode: int) -> str:
    return f"S{int(season)}E{int(episode)}"


def _episode_label(item: dict) -> str:
    season = int(item.get("season") or 1)
    episode = int(item.get("episode_number") or 0)
    if season > 1:
        return f"T{season:02d}E{episode:02d}"
    return str(episode)


async def _request_text(url: str, *, referer: str | None = None, headers: dict | None = None) -> str:
    client = await get_http_client()
    merged_headers = dict(_HTTP_HEADERS)
    if referer:
        merged_headers["Referer"] = referer
    if headers:
        merged_headers.update(headers)

    last_exc = None
    for attempt in range(3):
        try:
            async with HTTP_SEMAPHORE:
                response = await client.get(url, headers=merged_headers)
                response.raise_for_status()
                return response.text
        except (httpx.PoolTimeout, httpx.ReadTimeout, httpx.ConnectTimeout) as error:
            last_exc = error
            await asyncio.sleep(0.4 * (attempt + 1))
    if last_exc:
        raise last_exc
    raise RuntimeError(f"Falha ao acessar {url}")


async def _post_text(url: str, data: dict, *, referer: str) -> str:
    client = await get_http_client()
    headers = dict(_HTTP_HEADERS)
    headers.update(
        {
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "*/*",
        }
    )
    async with HTTP_SEMAPHORE:
        response = await client.post(url, data=data, headers=headers)
        response.raise_for_status()
        return response.text


async def search_anime(query: str, limit: int | None = None):
    key = (query or "").strip().lower()
    if not key:
        return []

    cached = _cache_get(_SEARCH_CACHE, key, _SEARCH_CACHE_TTL)
    if cached is not None:
        return cached[:limit] if limit else cached

    url = f"{BASE_URL}/ajax/posts?q={quote(query)}"
    text = await _request_text(
        url,
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json,text/plain,*/*"},
    )
    payload = json.loads(text)
    found = []

    for raw in payload.get("data") or []:
        if str(raw.get("type") or "").lower() != "anime":
            continue
        item_url = str(raw.get("url") or "").strip()
        anime_id = _normalize_anime_id(item_url or raw.get("id"))
        title = _clean(raw.get("name")) or anime_id.replace("-", " ").title()
        is_dubbed = bool(re.search(r"\bdublado\b", f"{title} {anime_id}", re.I))
        score = 100
        if is_dubbed and not re.search(r"\bdublado\b", query or "", re.I):
            score -= 10
        cover_url = str(raw.get("image") or "").replace("\\/", "/").strip()
        found.append(
            {
                "id": anime_id,
                "title": title,
                "raw_title": title,
                "alt_titles": [],
                "is_dubbed": is_dubbed,
                "url": item_url or _anime_url(anime_id),
                "cover_url": cover_url,
                "image_url": cover_url,
                "_score": score,
            }
        )

    found.sort(key=lambda item: item.get("_score", 0), reverse=True)
    _cache_set(_SEARCH_CACHE, key, found)
    return found[:limit] if limit else found


def _meta_content(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return _clean(tag.get("content"))
    return ""


def _parse_title(soup: BeautifulSoup, fallback: str) -> str:
    candidates = [
        soup.select_one("h1"),
        soup.select_one(".anime-title"),
        soup.select_one(".title"),
    ]
    for tag in candidates:
        title = _clean(tag.get_text(" ", strip=True) if tag else "")
        if title:
            return title

    og_title = _meta_content(soup, "og:title")
    og_title = re.sub(r"(?i)^ass?isitir\s+", "", og_title)
    og_title = re.sub(r"(?i)\s*[–-]\s*todos os episodios online.*$", "", og_title).strip()
    return _clean(og_title) or fallback.replace("-", " ").title()


def _parse_description(soup: BeautifulSoup) -> str:
    for block in soup.select(".detail-attr"):
        label = _clean(block.select_one(".attr").get_text(" ", strip=True) if block.select_one(".attr") else "")
        if re.search(r"sinopse", label, re.I):
            text_node = block.select_one(".text-content") or block.select_one(".text")
            text = _clean(text_node.get_text(" ", strip=True) if text_node else "")
            if text and len(text) > 30:
                return text

    for selector in (".sinopse", ".synopsis", ".description", "[itemprop='description']"):
        node = soup.select_one(selector)
        text = _clean(node.get_text(" ", strip=True) if node else "")
        if text and len(text) > 30:
            return text

    text = _meta_content(soup, "og:description", "description")
    text = re.sub(r"(?i)^assista todas as temporadas e episódios de .*?online,\s*", "", text)
    return _clean(text)


def _style_url(value: str | None) -> str:
    match = re.search(r"url\((['\"]?)(.*?)\1\)", value or "", re.I)
    return _clean(match.group(2) if match else "")


def _parse_genres(soup: BeautifulSoup) -> list[str]:
    genres: list[str] = []
    seen: set[str] = set()
    for selector in (".category-list a", "a[href*='/genero/']", "a[href*='/categoria/']", ".genres a", ".genre a"):
        for anchor in soup.select(selector):
            text = _clean(anchor.get_text(" ", strip=True))
            if not text:
                continue
            key = text.lower()
            if key not in seen:
                seen.add(key)
                genres.append(text)
    return genres


def _parse_episodes_from_detail(soup: BeautifulSoup, anime_id: str) -> list[dict]:
    by_key = {}
    for anchor in soup.select("a[href*='-season-'][href*='-episode']"):
        href = urljoin(BASE_URL, anchor.get("href") or "")
        slug = _slug_from_url(href)
        match = re.search(r"^(.+)-(\d+)-season-(\d+)-episode$", slug)
        if not match:
            continue

        base_slug = match.group(1)
        season = int(match.group(2))
        episode = int(match.group(3))
        if base_slug != _normalize_anime_id(anime_id):
            continue

        text = _clean(anchor.get_text(" ", strip=True))
        text = re.sub(r"(?i)^continuar\s*", "", text).strip()
        desc_node = anchor.select_one(".epx-desc")
        title_node = anchor.select_one(".epx-title")
        title = _clean(desc_node.get_text(" ", strip=True) if desc_node else "")
        if not title:
            title = re.sub(r"^\d+\D*\s*epis[oó]dio\s*", "", text, flags=re.I).strip()
        episode_label = _clean(title_node.get_text(" ", strip=True) if title_node else "")
        if not episode_label:
            episode_label = _clean(anchor.get("title") or "") or _episode_label({"season": season, "episode_number": episode})
        thumb_node = anchor.select_one(".epx-thumb")
        thumb = _style_url(thumb_node.get("style") if thumb_node else "")
        key = (season, episode)
        by_key[key] = {
            "episode": _episode_key(season, episode),
            "number": _episode_key(season, episode) if season > 1 else str(episode),
            "episode_number": episode,
            "season": season,
            "title": title,
            "episode_label": episode_label,
            "thumb": thumb,
            "image": thumb,
            "url": href,
            "base_slug": base_slug,
            "label": _episode_label({"season": season, "episode_number": episode}),
        }

    return [by_key[key] for key in sorted(by_key)]


async def get_anime_details(anime_id: str):
    anime_id = _normalize_anime_id(anime_id)
    cached = _cache_get(_DETAILS_CACHE, anime_id, _DETAILS_CACHE_TTL)
    if cached is not None:
        return cached

    url = _anime_url(anime_id)
    html_doc = await _request_text(url)
    soup = BeautifulSoup(html_doc, "html.parser")
    title = _parse_title(soup, anime_id)
    cover = _meta_content(soup, "og:image")
    description = _parse_description(soup)
    episodes_payload = _parse_episodes_from_detail(soup, anime_id)
    seasons = sorted({int(item.get("season") or 1) for item in episodes_payload}) or [1]

    text = _clean(soup.get_text(" ", strip=True))
    score = ""
    match = re.search(r"Score\s*([0-9]+(?:[.,][0-9]+)?/10)", text, re.I)
    if match:
        score = match.group(1).replace(",", ".")
    year = ""
    match = re.search(r"Data de lan[cç]amento\s*(\d{4})", text, re.I)
    if match:
        year = match.group(1)
    status = "Em Progresso" if re.search(r"Em Progresso", text, re.I) else ""
    if re.search(r"\bCompleto\b", text, re.I):
        status = "Completo"
    season_name = ""
    match = re.search(r"Temporada\s+([A-Za-zÀ-ÿ]+)", text, re.I)
    if match:
        season_name = match.group(1).strip().lower()

    data = {
        "id": anime_id,
        "title": title,
        "raw_title": title,
        "alt_titles": [],
        "description": description,
        "url": url,
        "cover_url": cover,
        "banner_url": cover,
        "media_image_url": cover,
        "score": score,
        "status": status or "N/A",
        "format": "TV",
        "episodes": len(episodes_payload) or None,
        "season": season_name,
        "season_year": year,
        "genres": _parse_genres(soup),
        "studio": "SushiAnimes",
        "source": "sushianimes",
        "seasons": seasons,
    }
    _cache_set(_DETAILS_CACHE, anime_id, data)
    _cache_set(_EPISODES_CACHE, anime_id, episodes_payload)
    return data


async def get_episodes(anime_id: str, offset: int = 0, limit: int = 3000):
    anime_id = _normalize_anime_id(anime_id)
    items = _cache_get(_EPISODES_CACHE, anime_id, _EPISODES_CACHE_TTL)
    if items is None:
        await get_anime_details(anime_id)
        items = _cache_get(_EPISODES_CACHE, anime_id, _EPISODES_CACHE_TTL) or []

    total = len(items)
    page = items[offset: offset + limit] if limit else items[offset:]
    by_episode = {}
    for index, item in enumerate(items):
        keys = {
            str(item.get("episode") or ""),
            str(item.get("episode_number") or ""),
            f"{item.get('season')}:{item.get('episode_number')}",
            f"S{item.get('season')}E{item.get('episode_number')}",
        }
        for key in keys:
            if key:
                by_episode[key] = index

    return {
        "items": page,
        "total": total,
        "by_episode": by_episode,
        "all_items": items,
        "seasons": sorted({int(item.get("season") or 1) for item in items}) or [1],
    }


async def get_seasons(anime_id: str) -> list[int]:
    payload = await get_episodes(anime_id, 0, 3000)
    return payload.get("seasons") or [1]


def _decode_js_string(value: str) -> str:
    value = value.strip()
    try:
        return json.loads(value)
    except Exception:
        return value.strip('"').replace("\\/", "/").replace("\\u0026", "&")


def _normalize_player_url(value: str, base_url: str = "") -> str:
    value = html.unescape(_decode_js_string(str(value or "").strip()))
    value = value.replace("\\/", "/").strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if base_url:
        value = urljoin(base_url, value)
    return value


def _is_video_url(value: str) -> bool:
    return bool(re.search(r"\.(mp4|webm|m3u8)(?:\?|$)", value or "", flags=re.IGNORECASE))


def _extract_player_urls_from_html(raw_html: str, base_url: str = "") -> list[str]:
    if not raw_html:
        return []

    found: list[str] = []
    seen: set[str] = set()

    def push(value: str) -> None:
        url = _normalize_player_url(value, base_url)
        if not url or url in seen:
            return
        if _is_video_url(url) or re.search(r"/(embed|player|watch)/|iframe|blogger\.com/video", url, flags=re.IGNORECASE):
            seen.add(url)
            found.append(url)

    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup.find_all(["source", "video", "iframe"]):
        for attr in ("src", "data-src", "data-video", "data-file"):
            value = tag.get(attr)
            if value:
                push(value)

    patterns = [
        r"playerEmbed\s*=\s*(\"(?:\\.|[^\"])+\"|'(?:\\.|[^'])+')",
        r'''(?:file|src|video|stream|url|hls|playlist)\s*[:=]\s*["']([^"']+)["']''',
        r'''https?:\\/\\/[^\s"'<>]+?\.(?:mp4|webm|m3u8)(?:\?[^\s"'<>]*)?''',
        r'''https?://[^\s"'<>]+?\.(?:mp4|webm|m3u8)(?:\?[^\s"'<>]*)?''',
        r'''src=["']([^"']+)["']''',
    ]
    for pattern in patterns:
        for match in re.findall(pattern, raw_html, flags=re.IGNORECASE):
            push(match if isinstance(match, str) else next((m for m in match if m), ""))

    return sorted(found, key=lambda url: 0 if _is_video_url(url) else 1)


async def _resolve_episode_embed(episode_url: str, embed_id: str) -> str:
    text = await _post_text(f"{BASE_URL}/ajax/embed", {"id": embed_id}, referer=episode_url)
    urls = _extract_player_urls_from_html(text, base_url=episode_url)
    if urls:
        return urls[0]
    raise RuntimeError("Sushi nao retornou uma URL de player reproduzivel.")


async def get_episode_player(anime_id: str, episode: str, preferred_quality: str = "HD"):
    anime_id = _normalize_anime_id(anime_id)
    season, episode_number = _parse_episode_ref(episode)
    cache_key = f"{anime_id}|{season}|{episode_number}|{preferred_quality}"
    cached = _cache_get(_PLAYER_CACHE, cache_key, _PLAYER_CACHE_TTL)
    if cached is not None:
        return cached

    payload = await get_episodes(anime_id, 0, 3000)
    items = payload.get("all_items") or []
    by_episode = payload.get("by_episode") or {}
    index = by_episode.get(f"{season}:{episode_number}")
    if index is None:
        index = by_episode.get(_episode_key(season, episode_number))
    if index is None:
        index = by_episode.get(str(episode_number))
    if index is None:
        raise RuntimeError(f"Episodio nao encontrado no Sushi: T{season}E{episode_number}")

    item = items[index]
    episode_url = item.get("url")
    page_html = await _request_text(episode_url, referer=_anime_url(anime_id))
    soup = BeautifulSoup(page_html, "html.parser")
    direct_urls = _extract_player_urls_from_html(page_html, base_url=episode_url)
    embed_ids: list[str] = []
    seen_embed_ids: set[str] = set()

    def push_embed(value: str) -> None:
        value = str(value or "").strip()
        if value and value not in seen_embed_ids:
            seen_embed_ids.add(value)
            embed_ids.append(value)

    for tag in soup.select(".play-btn[data-embed], .play-btn[data-id], [data-embed], [data-id]"):
        push_embed(tag.get("data-embed") or tag.get("data-id") or "")
    for match in re.findall(r'''data-(?:embed|id)=["']([^"']+)["']''', page_html, flags=re.IGNORECASE):
        push_embed(match)

    video = direct_urls[0] if direct_urls else ""
    if not video:
        last_error: Exception | None = None
        for embed_id in embed_ids:
            try:
                video = (await _resolve_episode_embed(episode_url, embed_id)).strip()
            except Exception as error:
                last_error = error
                continue
            if video:
                break
        if not video:
            raise last_error or RuntimeError("Nao encontrei player reproduzivel no Sushi.")

    quality = "HD" if (preferred_quality or "").upper() in {"HD", "FULLHD", "FHD"} else "SD"
    videos = {"HD": video, "SD": video}

    prev_episode = None
    next_episode = None
    if index > 0:
        prev_episode = str(items[index - 1].get("episode"))
    if index + 1 < len(items):
        next_episode = str(items[index + 1].get("episode"))

    data = {
        "video": video,
        "videos": videos,
        "base_slug": anime_id,
        "server": "SUSHI",
        "player_type": "video" if _is_video_url(video) else "iframe",
        "quality": quality,
        "available_qualities": ["HD", "SD"],
        "prev_episode": prev_episode,
        "next_episode": next_episode,
        "total_episodes": len(items),
        "season": int(item.get("season") or season),
        "episode_number": int(item.get("episode_number") or episode_number),
        "episode_title": item.get("title") or "",
        "title": item.get("title") or "",
        "thumb": item.get("thumb") or item.get("image") or "",
    }
    _cache_set(_PLAYER_CACHE, cache_key, data)
    return data


def invalidate_episode_caches(anime_id: str, episode: str) -> None:
    anime_id = _normalize_anime_id(anime_id)
    season, episode_number = _parse_episode_ref(episode)
    prefix = f"{anime_id}|{season}|{episode_number}|"
    for key in list(_PLAYER_CACHE.keys()):
        if str(key).startswith(prefix):
            _PLAYER_CACHE.pop(key, None)


async def get_random_anime_by_genre(genre_key: str, exclude_anime_id: str | None = None) -> dict:
    results = await search_anime(genre_key or "anime")
    if exclude_anime_id:
        results = [item for item in results if item.get("id") != exclude_anime_id]
    if not results:
        raise RuntimeError("Nenhum anime encontrado no Sushi.")
    return await get_anime_details(results[0]["id"])
