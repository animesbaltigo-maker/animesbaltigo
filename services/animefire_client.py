import asyncio
import re
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup


BASE_URL = "https://animefire.io"
LIGHTSPEED_SERVERS = ["s2", "s3", "s4", "s5", "s6", "s7"]

_SEARCH_CACHE = {}
_DETAILS_CACHE = {}
_EPISODES_CACHE = {}
_VIDEO_CACHE = {}

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": BASE_URL,
}


async def _get(url: str) -> str:
    async with httpx.AsyncClient(
        timeout=20,
        headers=_HTTP_HEADERS,
        follow_redirects=True,
    ) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.text


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _normalize_slug_for_page(anime_id: str) -> str:
    return (anime_id or "").strip().strip("/")


def _normalize_text(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^\w\s-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _search_path_term(query: str) -> str:
    """
    one piece -> one-piece
    Mahou Shoujo Tai Arusu -> mahou-shoujo-tai-arusu
    """
    text = _normalize_text(query)
    text = text.replace(" ", "-")
    text = re.sub(r"-+", "-", text).strip("-")
    return text


def _extract_server_name(url: str) -> str:
    m = re.search(r"lightspeedst\.net/(s\d+)", url)
    return m.group(1) if m else "s6"


def _extract_quality_name(url: str) -> str:
    url = url.lower()

    if "1080p" in url:
        return "FULLHD"
    if "720p" in url:
        return "HD"
    if "/hd/" in url:
        return "HD"
    if "/sd/" in url or "480p" in url:
        return "SD"

    return "HD"


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

    if "episodio" in t or "episódio" in t:
        score -= 500

    score += max(0, 50 - len(t))
    return score


async def search_anime(query: str):
    key = (query or "").strip().lower()
    if key in _SEARCH_CACHE:
        return _SEARCH_CACHE[key]

    search_term = _search_path_term(query)
    url = f"{BASE_URL}/pesquisar/{quote(search_term)}"

    print(f"[BUSCA] query={query!r}")
    print(f"[BUSCA] BASE_URL={BASE_URL!r}")
    print(f"[BUSCA] url={url}")

    try:
        html = await _get(url)
        print(f"[BUSCA] html_len={len(html)}")
        print(f"[BUSCA] html_inicio={html[:300]!r}")
    except Exception as e:
        print(f"[BUSCA] erro_no_get={repr(e)}")
        raise

    soup = BeautifulSoup(html, "html.parser")
    links = soup.select("a[href*='/animes/']")
    print(f"[BUSCA] links_encontrados={len(links)}")

    found = {}

    for a in links:
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
            title = slug.replace("-", " ").title()

        score = _score_candidate(query, title, slug)
        if score <= -9999:
            continue

        item = {
            "id": slug,
            "title": title,
            "_score": score,
        }

        prev = found.get(slug)
        if not prev or item["_score"] > prev["_score"]:
            found[slug] = item

    ordered = sorted(found.values(), key=lambda x: (-x["_score"], x["title"].lower()))
    results = [{"id": x["id"], "title": x["title"]} for x in ordered[:20]]

    print(f"[BUSCA] resultados={len(results)}")

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
            if (
                "video" in content_type
                or "mp4" in content_type
                or "octet-stream" in content_type
            ):
                return True
    except Exception:
        pass

    return False


async def _check_candidate(client: httpx.AsyncClient, url: str):
    ok = await _url_exists_with_client(client, url)
    return url if ok else None


def _build_candidate_urls(base_slug: str, episode: str) -> list[str]:
    """
    Ordem correta de qualidade:
    1. FULLHD
    2. HD
    3. SD

    Só cai para a menor se a maior não existir.
    """
    candidates = []

    # 1) FULLHD em todos os servidores
    for server in LIGHTSPEED_SERVERS:
        base = f"https://lightspeedst.net/{server}"
        candidates.append(f"{base}/mp4_temp/{base_slug}/{episode}/1080p.mp4")

    # 2) HD em todos os servidores
    for server in LIGHTSPEED_SERVERS:
        base = f"https://lightspeedst.net/{server}"
        candidates.append(f"{base}/mp4_temp/{base_slug}/{episode}/720p.mp4")
        candidates.append(f"{base}/mp4/{base_slug}/hd/{episode}.mp4")

    # 3) SD em todos os servidores
    for server in LIGHTSPEED_SERVERS:
        base = f"https://lightspeedst.net/{server}"
        candidates.append(f"{base}/mp4_temp/{base_slug}/{episode}/480p.mp4")
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

    fallback = f"https://lightspeedst.net/s6/mp4/{base_slug}/sd/{episode}.mp4"
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
