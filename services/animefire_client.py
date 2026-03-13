import re
from urllib.parse import quote

from bs4 import BeautifulSoup

from config import SOURCE_SITE_BASE
from core.http_client import http_get

BASE_URL = SOURCE_SITE_BASE.rstrip("/")

_SEARCH_CACHE = {}
_DETAILS_CACHE = {}
_EPISODES_CACHE = {}
_VIDEO_CACHE = {}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _slugify(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text


async def _get(url: str):
    return await http_get(url)


def _extract_slug(url: str):
    if "/animes/" not in url:
        return None

    slug = url.split("/animes/")[-1]
    slug = slug.strip("/")

    if "/" in slug:
        return None

    return slug


# =========================
# SEARCH
# =========================

async def search_anime(query: str):
    key = query.lower().strip()

    if key in _SEARCH_CACHE:
        return _SEARCH_CACHE[key]

    slug_query = _slugify(query)

    urls = [
        f"{BASE_URL}/pesquisar/{slug_query}",
        f"{BASE_URL}/?s={quote(query)}"
    ]

    results = []

    for url in urls:
        try:
            html = await _get(url)
            soup = BeautifulSoup(html, "html.parser")

            found = {}

            for a in soup.select("a[href]"):
                href = a.get("href", "")

                slug = _extract_slug(href)

                if not slug:
                    continue

                title = _clean(a.get_text())

                if not title:
                    img = a.find("img")
                    if img and img.get("alt"):
                        title = _clean(img["alt"])

                if not title:
                    title = slug.replace("-", " ").title()

                found[slug] = {
                    "id": slug,
                    "title": title
                }

            results = list(found.values())

            if results:
                break

        except Exception:
            continue

    _SEARCH_CACHE[key] = results
    return results


# =========================
# DETAILS
# =========================

async def get_anime_details(slug: str):
    if slug in _DETAILS_CACHE:
        return _DETAILS_CACHE[slug]

    url = f"{BASE_URL}/animes/{slug}"

    html = await _get(url)
    soup = BeautifulSoup(html, "html.parser")

    title = soup.select_one("h1")

    title = _clean(title.text) if title else slug.replace("-", " ").title()

    poster = None

    img = soup.select_one("img")

    if img and img.get("src"):
        poster = img["src"]

    data = {
        "id": slug,
        "title": title,
        "poster": poster
    }

    _DETAILS_CACHE[slug] = data
    return data


# =========================
# EPISODES
# =========================

async def get_anime_episodes(slug: str):
    if slug in _EPISODES_CACHE:
        return _EPISODES_CACHE[slug]

    url = f"{BASE_URL}/animes/{slug}"

    html = await _get(url)
    soup = BeautifulSoup(html, "html.parser")

    episodes = []

    for a in soup.select("a[href]"):
        href = a.get("href", "")

        if "/episodio/" not in href:
            continue

        ep_text = _clean(a.text)

        match = re.search(r"(\d+)", ep_text)

        number = int(match.group(1)) if match else None

        episodes.append({
            "id": href.split("/episodio/")[-1].strip("/"),
            "number": number,
            "title": ep_text
        })

    episodes = sorted(
        episodes,
        key=lambda x: x["number"] if x["number"] is not None else 0
    )

    _EPISODES_CACHE[slug] = episodes
    return episodes


# =========================
# VIDEO
# =========================

async def get_episode_player(ep_slug: str):
    if ep_slug in _VIDEO_CACHE:
        return _VIDEO_CACHE[ep_slug]

    url = f"{BASE_URL}/episodio/{ep_slug}"

    html = await _get(url)
    soup = BeautifulSoup(html, "html.parser")

    iframe = soup.select_one("iframe")

    video = None

    if iframe and iframe.get("src"):
        video = iframe["src"]

    data = {
        "video": video
    }

    _VIDEO_CACHE[ep_slug] = data
    return data

# =========================
# COMPATIBILIDADE COM CALLBACKS
# =========================

async def get_episodes(slug: str):
    return await get_anime_episodes(slug)


async def get_episode(slug: str):
    return await get_episode_player(slug)
