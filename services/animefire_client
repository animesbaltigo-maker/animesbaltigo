import asyncio
import re
from urllib.parse import unquote, urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup


BASE_URL = "https://animefire.io"
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
    async with httpx.AsyncClient(
        timeout=20,
        headers=_HTTP_HEADERS,
        follow_redirects=True,
    ) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.text


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _normalize_slug_for_page(anime_id: str) -> str:
    return (anime_id or "").strip().strip("/")


def _normalize_text(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[\(\)\[\]\-_:,.!/?]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_server_name(url: str) -> str:
    m = re.search(r"lightspeedst\.net/(s\d+)", url)
    return m.group(1) if m else "s6"


def _extract_quality_name(url: str) -> str:
    if "720p" in url:
        return "720p"
    if "/hd/" in url:
        return "hd"
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
        # para busca com várias palavras, todas devem aparecer
        for w in q_words:
            if w not in t and w not in s:
                return -9999

    for w in q_words:
        if w in t:
            score += 80
        if w in s:
            score += 45

    if "episodio" in t or "episódio" in t:
        score -= 500

    # títulos menores tendem a ser mais "obra" do que ruído
    score += max(0, 50 - len(t))
    return score


def _extract_animefire_slug_from_url(url: str) -> str:
    if not url:
        return ""

    parsed = urlparse(url)

    # links do duckduckgo vêm via redirect
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        uddg = qs.get("uddg", [])
        if uddg:
            return _extract_animefire_slug_from_url(unquote(uddg[0]))

    if "animefire.io" not in parsed.netloc:
        return ""

    path = parsed.path.strip("/")
    if not path.startswith("animes/"):
        return ""

    slug = path[len("animes/"):].strip("/")
    if not slug or "/" in slug:
        return ""

    return slug


def _title_from_slug(slug: str) -> str:
    return slug.replace("-", " ").title()


async def _search_duckduckgo(query: str) -> list[dict]:
    # consultas simples e amplas
    search_queries = [
        f'site:animefire.io/animes "{query}"',
        f"site:animefire.io/animes {query}",
    ]

    found: dict[str, dict] = {}

    for q in search_queries:
        html = await _get(DUCK_URL, params={"q": q})
        soup = BeautifulSoup(html, "html.parser")

        for a in soup.select("a[href]"):
            href = (a.get("href") or "").strip()
            slug = _extract_animefire_slug_from_url(href)
            if not slug:
                continue

            title = _clean(a.get_text()) or _title_from_slug(slug)
            score = _score_candidate(query, title, slug)
            if score <= -9999:
                continue

            item = {"id": slug, "title": title, "_score": score}
            prev = found.get(slug)
            if not prev or item["_score"] > prev["_score"]:
                found[slug] = item

    ordered = sorted(found.values(), key=lambda x: (-x["_score"], x["title"].lower()))
    return [{"id": x["id"], "title": x["title"]} for x in ordered[:20]]


async def _search_site_fallback(query: str) -> list[dict]:
    # fallback simples caso o DDG falhe
    html = await _get(f"{BASE_URL}/?s={query}")
    soup = BeautifulSoup(html, "html.parser")

    found: dict[str, dict] = {}

    for a in soup.select("a[href*='/animes/']"):
        href = (a.get("href") or "").strip()
        if "/animes/" not in href:
            continue

        slug = href.split("/animes/")[-1].strip("/")
        if not slug or "/" in slug:
            continue

        title = _clean(a.get_text())
        if not title:
            img = a.find("img")
            if img:
                title = _clean(img.get("alt"))
        if not title:
            title = _title_from_slug(slug)

        score = _score_candidate(query, title, slug)
        if score <= -9999:
            continue

        item = {"id": slug, "title": title, "_score": score}
        prev = found.get(slug)
        if not prev or item["_score"] > prev["_score"]:
            found[slug] = item

    ordered = sorted(found.values(), key=lambda x: (-x["_score"], x["title"].lower()))
    return [{"id": x["id"], "title": x["title"]} for x in ordered[:20]]


async def search_anime(query: str):
    key = (query or "").strip().lower()
    if key in _SEARCH_CACHE:
        return _SEARCH_CACHE[key]

    results = []
    try:
        results = await _search_duckduckgo(query)
    except Exception:
        results = []

    if not results:
        try:
            results = await _search_site_fallback(query)
        except Exception:
            results = []

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
    p = soup.find("p")
    if p:
        description = _clean(p.get_text())

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

            base_slug = m.group(1)
            ep = m.group(2)

            episodes.append({
                "episode": ep,
                "base_slug": base_slug,
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
        r = await client.head(url)
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
        candidates.append(f"{base}/mp4_temp/{base_slug}/{episode}/720p.mp4")
        candidates.append(f"{base}/mp4/{base_slug}/hd/{episode}.mp4")
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
        base_slug = anime_id.replace("-todos-os-episodios", "")

    video = await _resolve_video_url(base_slug, episode)
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
