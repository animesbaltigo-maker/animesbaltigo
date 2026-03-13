import asyncio
import re
import unicodedata
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from config import SOURCE_SITE_BASE
from core.http_client import get_http_client


BASE_URL = SOURCE_SITE_BASE.rstrip("/")
BASE_NETLOC = urlparse(BASE_URL).netloc.lower()
DUCK_URL = "https://html.duckduckgo.com/html/"
LIGHTSPEED_SERVERS = ["s2", "s3", "s4", "s5", "s6", "s7"]

_SEARCH_CACHE = {}
_DETAILS_CACHE = {}
_EPISODES_CACHE = {}
_VIDEO_CACHE = {}

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": BASE_URL,
}


async def _get(url: str, *, params: dict | None = None) -> str:
    client = await get_http_client()
    response = await client.get(url, params=params, headers=_HTTP_HEADERS)
    response.raise_for_status()
    return response.text


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _strip_accents(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _slugify_query(text: str) -> str:
    text = _strip_accents((text or "").strip().lower())
    text = re.sub(r"[^\w\s-]", " ", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text


def _normalize_slug_for_page(anime_id: str) -> str:
    return (anime_id or "").strip().strip("/")


def _normalize_base_slug(slug: str) -> str:
    slug = _normalize_slug_for_page(slug)
    slug = re.sub(r"-todos-os-episodios$", "", slug)
    return slug


def _normalize_text(text: str) -> str:
    text = _strip_accents((text or "").lower())
    text = re.sub(r"[\(\)\[\]\-_:,.!/?]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_server_name(url: str) -> str:
    m = re.search(r"lightspeedst\.net/(s\d+)", url)
    return m.group(1) if m else "s6"


def _extract_quality_name(url: str) -> str:
    u = (url or "").lower()
    if "1080" in u or "fullhd" in u:
        return "fullhd"
    if "720" in u:
        return "720p"
    if "/hd/" in u:
        return "hd"
    if "/sd/" in u or "480" in u or "360" in u:
        return "sd"
    return "hd"


def _score_candidate(query: str, title: str, slug: str) -> float:
    q = _normalize_text(query)
    t = _normalize_text(title)
    s = _normalize_text(slug.replace("-", " "))

    if not q:
        return -9999

    q_words = [w for w in q.split() if len(w) > 1]
    if not q_words:
        return -9999

    score = 0.0

    if q == t:
        score += 1000
    if q == s:
        score += 900
    if q in t:
        score += 500
    if q in s:
        score += 350

    if len(q_words) == 1:
        w = q_words[0]
        if w not in t and w not in s:
            return -9999
        if t.startswith(w):
            score += 120
        if s.startswith(w):
            score += 90
    else:
        for w in q_words:
            if w not in t and w not in s:
                return -9999

    for w in q_words:
        if w in t:
            score += 80
        if w in s:
            score += 45

    if "episodio" in t or "episodio" in s:
        score -= 500

    score += max(0, 50 - len(t))
    return score


def _title_from_slug(slug: str) -> str:
    return slug.replace("-", " ").title()


def _extract_slug_from_anime_link(url: str) -> str:
    if not url:
        return ""

    parsed = urlparse(url)
    path = parsed.path.strip("/")

    if not path.startswith("animes/"):
        return ""

    slug = path[len("animes/"):].strip("/")
    if not slug or "/" in slug:
        return ""

    return slug


def _extract_site_slug_from_url(url: str) -> str:
    if not url:
        return ""

    parsed = urlparse(url)

    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        uddg = qs.get("uddg", [])
        if uddg:
            return _extract_site_slug_from_url(unquote(uddg[0]))

    if parsed.netloc and BASE_NETLOC not in parsed.netloc.lower():
        return ""

    return _extract_slug_from_anime_link(url)


def _is_episode_like(title: str, slug: str) -> bool:
    t = _normalize_text(title)
    s = _normalize_text(slug.replace("-", " "))

    if "episodio" in t or "episodio" in s:
        return True

    if re.search(r"\bepisodio\s+\d+\b", t):
        return True

    return False


def _merge_result(found: dict[str, dict], query: str, slug: str, title: str):
    title = _clean(title) or _title_from_slug(slug)

    if _is_episode_like(title, slug):
        return

    score = _score_candidate(query, title, slug)
    if score <= -9999:
        return

    item = {"id": slug, "title": title, "_score": score}
    prev = found.get(slug)
    if not prev or item["_score"] > prev["_score"]:
        found[slug] = item


def _results_from_found(found: dict[str, dict]) -> list[dict]:
    ordered = sorted(found.values(), key=lambda x: (-x["_score"], x["title"].lower()))
    return [{"id": x["id"], "title": x["title"]} for x in ordered[:20]]


async def _search_site_direct(query: str) -> list[dict]:
    found: dict[str, dict] = {}
    slug_query = _slugify_query(query)

    urls = [
        f"{BASE_URL}/pesquisar/{slug_query}",
        f"{BASE_URL}/?s={quote(query)}",
    ]

    for url in urls:
        try:
            html = await _get(url)
        except Exception:
            continue

        soup = BeautifulSoup(html, "html.parser")

        for a in soup.select("a[href]"):
            href = (a.get("href") or "").strip()
            slug = _extract_slug_from_anime_link(href)
            if not slug:
                continue

            title = _clean(a.get_text())
            if not title:
                img = a.find("img")
                if img and img.get("alt"):
                    title = _clean(img.get("alt"))

            _merge_result(found, query, slug, title)

        if found:
            return _results_from_found(found)

    return []


async def _search_duckduckgo(query: str) -> list[dict]:
    search_queries = [
        f'site:{BASE_NETLOC}/animes "{query}"',
        f"site:{BASE_NETLOC}/animes {query}",
    ]

    found: dict[str, dict] = {}

    for q in search_queries:
        try:
            html = await _get(DUCK_URL, params={"q": q})
        except Exception:
            continue

        soup = BeautifulSoup(html, "html.parser")

        for a in soup.select("a[href]"):
            href = (a.get("href") or "").strip()
            slug = _extract_site_slug_from_url(href)
            if not slug:
                continue

            title = _clean(a.get_text()) or _title_from_slug(slug)
            _merge_result(found, query, slug, title)

    return _results_from_found(found)


async def search_anime(query: str):
    key = (query or "").strip().lower()
    if not key:
        return []

    if key in _SEARCH_CACHE:
        return _SEARCH_CACHE[key]

    results = []

    try:
        results = await _search_site_direct(query)
    except Exception:
        results = []

    if not results:
        try:
            results = await _search_duckduckgo(query)
        except Exception:
            results = []

    if results:
        _SEARCH_CACHE[key] = results

    return results


async def get_anime_details(anime_id: str):
    anime_id = _normalize_slug_for_page(anime_id)

    if anime_id in _DETAILS_CACHE:
        return _DETAILS_CACHE[anime_id]

    url = f"{BASE_URL}/animes/{anime_id}"
    html = await _get(url)
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.find("h1")
    title = title_el.get_text(strip=True) if title_el else anime_id.replace("-", " ").title()

    description = ""
    for p in soup.find_all("p"):
        text = _clean(p.get_text())
        if text and len(text) > 80:
            description = text
            break

    cover_url = ""
    og_img = soup.find("meta", attrs={"property": "og:image"})
    if og_img and og_img.get("content"):
        cover_url = og_img["content"].strip()

    if not cover_url:
        img = soup.find("img")
        if img and img.get("src"):
            cover_url = img["src"].strip()

    data = {
        "id": anime_id,
        "title": title,
        "description": description,
        "url": url,
        "cover_url": cover_url,
    }

    _DETAILS_CACHE[anime_id] = data
    return data


async def get_episodes(anime_id: str, offset: int = 0, limit: int = 3000):
    anime_id = _normalize_slug_for_page(anime_id)

    if anime_id not in _EPISODES_CACHE:
        url = f"{BASE_URL}/animes/{anime_id}"
        html = await _get(url)
        soup = BeautifulSoup(html, "html.parser")

        episodes = []
        pattern = re.compile(r"/animes/([^/]+)/(\d+)(?:/)?$")

        for a in soup.select("a[href*='/animes/']"):
            href = (a.get("href") or "").strip()
            m = pattern.search(href)
            if not m:
                continue

            page_slug = m.group(1)
            ep = m.group(2)

            episodes.append({
                "episode": ep,
                "base_slug": _normalize_base_slug(page_slug),
            })

        unique = {}
        for e in episodes:
            unique[e["episode"]] = e

        items = sorted(unique.values(), key=lambda x: int(x["episode"]))
        _EPISODES_CACHE[anime_id] = items

    items = _EPISODES_CACHE[anime_id]
    total = len(items)
    page = items[offset: offset + limit]

    return {
        "items": page,
        "total": total,
    }


async def _url_exists_with_client(client: httpx.AsyncClient, url: str) -> bool:
    try:
        r = await client.head(url, headers=_HTTP_HEADERS)
        if r.status_code == 200:
            content_type = (r.headers.get("content-type") or "").lower()
            if "video" in content_type or "mp4" in content_type or content_type == "":
                return True
    except Exception:
        pass

    try:
        r = await client.get(url, headers={**_HTTP_HEADERS, "Range": "bytes=0-0"})
        if r.status_code in (200, 206):
            content_type = (r.headers.get("content-type") or "").lower()
            if "video" in content_type or "mp4" in content_type or "octet-stream" in content_type:
                return True
    except Exception:
        pass

    return False


async def _check_candidate(client: httpx.AsyncClient, url: str):
    ok = await _url_exists_with_client(client, url)
    return url if ok else None


def _build_candidate_urls(base_slug: str, episode: str) -> list[str]:
    candidates = []
    for server in LIGHTSPEED_SERVERS:
        base = f"https://lightspeedst.net/{server}"
        candidates.append(f"{base}/mp4_temp/{base_slug}/{episode}/1080p.mp4")
        candidates.append(f"{base}/mp4_temp/{base_slug}/{episode}/720p.mp4")
        candidates.append(f"{base}/mp4/{base_slug}/fullhd/{episode}.mp4")
        candidates.append(f"{base}/mp4/{base_slug}/hd/{episode}.mp4")
        candidates.append(f"{base}/mp4/{base_slug}/sd/{episode}.mp4")
    return candidates


async def _resolve_video_url(base_slug: str, episode: str) -> str:
    cache_key = f"{base_slug}|{episode}"
    if cache_key in _VIDEO_CACHE:
        return _VIDEO_CACHE[cache_key]

    candidates = _build_candidate_urls(base_slug, episode)

    async with httpx.AsyncClient(
        timeout=12,
        headers=_HTTP_HEADERS,
        follow_redirects=True,
    ) as client:
        tasks = [asyncio.create_task(_check_candidate(client, url)) for url in candidates]

        try:
            for task in asyncio.as_completed(tasks):
                result = await task
                if result:
                    for pending in tasks:
                        if not pending.done():
                            pending.cancel()
                    _VIDEO_CACHE[cache_key] = result
                    return result
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    fallback = f"https://lightspeedst.net/s6/mp4/{base_slug}/hd/{episode}.mp4"
    _VIDEO_CACHE[cache_key] = fallback
    return fallback


async def get_episode_player(anime_id: str, episode: str):
    anime_id = _normalize_slug_for_page(anime_id)

    payload = await get_episodes(anime_id, 0, 3000)
    items = payload.get("items", [])

    base_slug = None
    index = None

    for i, item in enumerate(items):
        if str(item.get("episode")) == str(episode):
            base_slug = item.get("base_slug")
            index = i
            break

    if not base_slug:
        base_slug = _normalize_base_slug(anime_id)

    video = await _resolve_video_url(base_slug, str(episode))
    server = _extract_server_name(video)
    quality = _extract_quality_name(video)

    prev_episode = None
    next_episode = None

    if index is not None:
        if index > 0:
            prev_episode = str(items[index - 1]["episode"])
        if index + 1 < len(items):
            next_episode = str(items[index + 1]["episode"])

    return {
        "video": video,
        "base_slug": base_slug,
        "server": server,
        "quality": quality,
        "prev_episode": prev_episode,
        "next_episode": next_episode,
        "total_episodes": len(items),
    }
