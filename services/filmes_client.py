import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from core.http_client import get_http_client


BASE_URL = "https://animefire.io"

LIST_TYPES = [
    "lista-de-filmes-legendados",
    "lista-de-filmes-dublados",
]

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": BASE_URL,
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


async def _get(url: str) -> str:
    client = await get_http_client()
    r = await client.get(url, headers=_HTTP_HEADERS)
    r.raise_for_status()
    return r.text


def _extract_slug(href: str) -> str:
    href = (href or "").strip()
    m = re.search(r"/animes/([^/]+?)(?:/)?$", href)
    return m.group(1).strip() if m else ""


def _extract_movies_from_html(page_html: str) -> list[dict]:
    soup = BeautifulSoup(page_html, "html.parser")
    found = {}

    for a in soup.select("a[href*='/animes/']"):
        href = (a.get("href") or "").strip()
        slug = _extract_slug(href)
        if not slug:
            continue

        title = _clean(a.get_text(" ", strip=True))

        if not title:
            img = a.find("img")
            if img:
                title = _clean(img.get("alt"))

        if not title:
            title = slug.replace("-", " ").title()

        full_url = urljoin(BASE_URL, href)

        found[slug] = {
            "id": slug,
            "title": title,
            "url": full_url,
        }

    return list(found.values())


def _extract_last_page(page_html: str, slug: str) -> int:
    soup = BeautifulSoup(page_html, "html.parser")
    max_page = 1

    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()

        m = re.search(rf"/{re.escape(slug)}/(\d+)(?:/)?$", href)
        if m:
            page_num = int(m.group(1))
            if page_num > max_page:
                max_page = page_num

    return max_page


async def _collect_paginated_list(slug: str) -> list[dict]:
    all_items = {}

    first_url = f"{BASE_URL}/{slug}"
    try:
        first_html = await _get(first_url)
    except Exception as e:
        print(f"[FILMES] erro ao abrir {first_url}: {repr(e)}")
        return []

    total_pages = _extract_last_page(first_html, slug)
    print(f"[FILMES] {slug}: {total_pages} página(s) detectada(s)")

    for item in _extract_movies_from_html(first_html):
        all_items[item["id"]] = item

    for page in range(2, total_pages + 1):
        page_url = f"{BASE_URL}/{slug}/{page}"
        try:
            page_html = await _get(page_url)
        except Exception as e:
            print(f"[FILMES] erro ao abrir {page_url}: {repr(e)}")
            continue

        for item in _extract_movies_from_html(page_html):
            all_items[item["id"]] = item

    return list(all_items.values())


async def get_all_movies() -> list[dict]:
    all_movies = {}

    for slug in LIST_TYPES:
        items = await _collect_paginated_list(slug)
        for item in items:
            all_movies[item["id"]] = item

    return sorted(all_movies.values(), key=lambda x: x["title"].lower())