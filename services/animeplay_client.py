import asyncio
import html
import json
import re
import time
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from core.http_client import get_http_client

BASE_URL = "https://animeplay.cloud"
ANILIST_API_URL = "https://graphql.anilist.co"

_SEARCH_CACHE = {}
_DETAILS_CACHE = {}
_EPISODES_CACHE = {}
_PLAYER_CACHE = {}
_ANILIST_CACHE = {}
_ANILIST_DISABLED_UNTIL = 0.0

_SEARCH_CACHE_TTL = 1800
_DETAILS_CACHE_TTL = 21600
_EPISODES_CACHE_TTL = 600
_PLAYER_CACHE_TTL = 21600
_ANILIST_CACHE_TTL = 86400

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": f"{BASE_URL}/",
    "Origin": BASE_URL,
}

_ANILIST_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

HTTP_SEMAPHORE = asyncio.Semaphore(20)


def _cache_get(cache: dict, key: str, ttl: int):
    item = cache.get(key)
    if not item:
        return None
    if time.time() - item["time"] > ttl:
        cache.pop(key, None)
        return None
    return item["data"]


def _cache_set(cache: dict, key: str, data) -> None:
    cache[key] = {"time": time.time(), "data": data}


def _clean(value: str | None) -> str:
    text = html.unescape(str(value or ""))
    if "Ã" in text:
        try:
            text = text.encode("latin-1").decode("utf-8")
        except UnicodeError:
            pass
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _strip_html_tags(value: str | None) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return _clean(text)


def _normalize_text(value: str | None) -> str:
    text = _clean(value).lower()
    text = re.sub(r"\b(?:dublado|legendado|todos os episodios|todos os episódios|online|hd|gratis|grátis)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _slug_from_url(url: str) -> str:
    path = urlsplit(url).path.strip("/")
    if path.startswith("anime/"):
        return path.split("/", 1)[1].strip("/")
    if path.startswith("episodio/"):
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
    return f"{BASE_URL}/anime/{quote_plus(_normalize_anime_id(anime_id)).replace('+', '-')}"


def _parse_episode_ref(value: str) -> tuple[int, int]:
    raw = str(value or "").strip()
    match = re.search(r"^[sS]?(\d+)[eE:.-](\d+)$", raw)
    if match:
        return int(match.group(1)), int(match.group(2))
    digits = re.search(r"\d+", raw)
    return 1, int(digits.group(0)) if digits else 1


def _episode_key(season: int, episode: int) -> str:
    return f"S{int(season)}E{int(episode)}"


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


async def _post_json(url: str, data: dict, *, referer: str) -> dict:
    client = await get_http_client()
    headers = dict(_HTTP_HEADERS)
    headers.update(
        {
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json,text/javascript,*/*;q=0.01",
        }
    )
    async with HTTP_SEMAPHORE:
        response = await client.post(url, data=data, headers=headers)
        response.raise_for_status()
        return response.json()


async def _post_graphql(url: str, payload: dict) -> dict:
    client = await get_http_client()
    async with HTTP_SEMAPHORE:
        response = await client.post(url, json=payload, headers=_ANILIST_HEADERS)
        response.raise_for_status()
        return response.json()


def _best_title_from_anilist(media: dict) -> str:
    title = media.get("title") or {}
    return (
        title.get("userPreferred")
        or title.get("romaji")
        or title.get("english")
        or title.get("native")
        or ""
    )


def _anilist_status_label(status: str) -> str:
    return {
        "FINISHED": "Finalizado",
        "RELEASING": "Em lançamento",
        "NOT_YET_RELEASED": "Não lançado",
        "CANCELLED": "Cancelado",
        "HIATUS": "Em hiato",
    }.get((status or "").strip().upper(), status or "")


def _anilist_format_label(fmt: str) -> str:
    return {
        "TV": "TV",
        "TV_SHORT": "TV Short",
        "MOVIE": "Filme",
        "SPECIAL": "Especial",
        "OVA": "OVA",
        "ONA": "ONA",
        "MUSIC": "Music",
    }.get((fmt or "").strip().upper(), fmt or "")


def _anilist_score(local_title: str, media: dict) -> int:
    local_norm = _normalize_text(local_title)
    title = media.get("title") or {}
    candidates = [
        title.get("romaji"),
        title.get("english"),
        title.get("native"),
        title.get("userPreferred"),
        *(media.get("synonyms") or []),
    ]
    normalized = [_normalize_text(value) for value in candidates if value]
    if local_norm and local_norm in normalized:
        return 100
    if any(local_norm and (local_norm in value or value in local_norm) for value in normalized):
        return 90
    local_tokens = set(local_norm.split())
    best = 0
    for value in normalized:
        tokens = set(value.split())
        if not tokens:
            continue
        overlap = len(local_tokens & tokens)
        best = max(best, int((overlap / max(len(local_tokens), len(tokens))) * 80))
    return best


async def _search_anilist_by_title(title: str, alt_titles: list[str] | None = None) -> dict | None:
    global _ANILIST_DISABLED_UNTIL

    if time.time() < _ANILIST_DISABLED_UNTIL:
        return None

    candidates = [title, *(alt_titles or [])]
    search_title = next((_clean(item) for item in candidates if _clean(item)), "")
    cache_key = _normalize_text(search_title)
    if not cache_key:
        return None

    cached = _cache_get(_ANILIST_CACHE, cache_key, _ANILIST_CACHE_TTL)
    if cached is not None:
        return cached

    query = """
    query ($search: String) {
      Page(page: 1, perPage: 5) {
        media(search: $search, type: ANIME) {
          id
          siteUrl
          title {
            romaji
            english
            native
            userPreferred
          }
          synonyms
          description(asHtml: false)
          averageScore
          status
          format
          episodes
          season
          seasonYear
          genres
          bannerImage
          trailer {
            site
            id
          }
          coverImage {
            extraLarge
            large
            medium
          }
          studios(isMain: true) {
            nodes {
              name
            }
          }
        }
      }
    }
    """

    best_media = None
    best_score = -1
    for candidate in candidates:
        candidate = _clean(candidate)
        if not candidate:
            continue
        try:
            data = await _post_graphql(
                ANILIST_API_URL,
                {"query": query, "variables": {"search": candidate}},
            )
        except httpx.HTTPStatusError as error:
            if error.response is not None and error.response.status_code == 429:
                _ANILIST_DISABLED_UNTIL = time.time() + 300
                print("[ANILIST] rate_limited; usando fallback local por 300s")
                return None
            print(f"[ANILIST] animeplay_search_error={repr(error)}")
            continue
        except Exception as error:
            print(f"[ANILIST] animeplay_search_error={repr(error)}")
            continue

        media_items = (((data or {}).get("data") or {}).get("Page") or {}).get("media") or []
        for media in media_items:
            score = _anilist_score(title, media)
            if score > best_score:
                best_score = score
                best_media = media

        if best_score >= 90:
            break

    if not best_media:
        _cache_set(_ANILIST_CACHE, cache_key, None)
        return None

    studios = (((best_media.get("studios") or {}).get("nodes")) or [])
    studio_name = studios[0].get("name") if studios else ""
    cover = best_media.get("coverImage") or {}
    result = {
        "anilist_id": best_media.get("id"),
        "anilist_url": best_media.get("siteUrl") or "",
        "title_romaji": ((best_media.get("title") or {}).get("romaji")) or "",
        "title_english": ((best_media.get("title") or {}).get("english")) or "",
        "title_native": ((best_media.get("title") or {}).get("native")) or "",
        "title": _best_title_from_anilist(best_media),
        "description": _strip_html_tags(best_media.get("description") or ""),
        "score": best_media.get("averageScore"),
        "status": _anilist_status_label(best_media.get("status") or ""),
        "format": _anilist_format_label(best_media.get("format") or ""),
        "episodes": best_media.get("episodes"),
        "season": best_media.get("season") or "",
        "season_year": best_media.get("seasonYear"),
        "genres": best_media.get("genres") or [],
        "studio": studio_name,
        "banner_url": best_media.get("bannerImage") or "",
        "cover_url": cover.get("extraLarge") or cover.get("large") or cover.get("medium") or "",
        "media_image_url": cover.get("large") or cover.get("medium") or "",
        "trailer_id": ((best_media.get("trailer") or {}).get("id")) or "",
        "trailer_site": ((best_media.get("trailer") or {}).get("site")) or "",
    }
    _cache_set(_ANILIST_CACHE, cache_key, result)
    return result


def _merge_anilist_data(local_data: dict, anilist_data: dict | None) -> dict:
    if not anilist_data:
        return local_data

    merged = dict(local_data)
    local_episode_count = local_data.get("episodes")

    for key in (
        "title",
        "score",
        "status",
        "format",
        "episodes",
        "season",
        "season_year",
        "genres",
        "studio",
        "anilist_id",
        "anilist_url",
        "title_romaji",
        "title_english",
        "title_native",
        "media_image_url",
        "trailer_id",
        "trailer_site",
    ):
        if anilist_data.get(key) not in (None, "", []):
            merged[key] = anilist_data[key]

    if anilist_data.get("description"):
        merged["description"] = anilist_data["description"]
    if anilist_data.get("cover_url"):
        merged["cover_url"] = anilist_data["cover_url"]
    if anilist_data.get("banner_url"):
        merged["banner_url"] = anilist_data["banner_url"]

    merged["source"] = "animeplay"
    merged["local_episodes"] = local_episode_count
    return merged


def _meta_content(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return _clean(tag.get("content"))
    return ""


def _is_dubbed(title: str, anime_id: str = "") -> bool:
    return bool(re.search(r"\bdublado\b", f"{title} {anime_id}", re.I))


def _clean_title_from_page(title: str) -> str:
    title = re.sub(r"\s*[–|-]\s*AnimePlay\.Cloud.*$", "", title or "", flags=re.I)
    return _clean(title)


def _extract_anime_cards(html_doc: str, query: str = "") -> list[dict]:
    soup = BeautifulSoup(html_doc, "html.parser")
    found = {}
    for anchor in soup.select("a[href*='/anime/']"):
        href = (anchor.get("href") or "").strip()
        anime_id = _normalize_anime_id(href)
        if not anime_id:
            continue

        title = _clean(anchor.get_text(" ", strip=True))
        img = anchor.find("img")
        cover_url = ""
        if img:
            cover_url = img.get("data-src") or img.get("src") or ""
            title = title or _clean(img.get("alt"))

        title = re.sub(r"\b(?:TV|OVA|ONA|Filme)\b\s*$", "", title, flags=re.I).strip()
        title = title or anime_id.replace("-", " ").title()
        dubbed = _is_dubbed(title, anime_id)
        score = 100
        if dubbed and not re.search(r"\bdublado\b", query or "", re.I):
            score -= 10

        item = {
            "id": anime_id,
            "title": title,
            "raw_title": title,
            "alt_titles": [],
            "is_dubbed": dubbed,
            "url": urljoin(BASE_URL, href),
            "cover_url": cover_url,
            "image_url": cover_url,
            "_score": score,
        }
        previous = found.get(anime_id)
        if not previous or item["_score"] > previous["_score"] or (item["cover_url"] and not previous.get("cover_url")):
            found[anime_id] = item

    return sorted(found.values(), key=lambda item: item.get("_score", 0), reverse=True)


async def search_anime(query: str, limit: int | None = None):
    key = (query or "").strip().lower()
    if not key:
        return []

    cached = _cache_get(_SEARCH_CACHE, key, _SEARCH_CACHE_TTL)
    if cached is not None:
        return cached[:limit] if limit else cached

    html_doc = await _request_text(f"{BASE_URL}/?s={quote_plus(query)}")
    found = _extract_anime_cards(html_doc, query=query)
    _cache_set(_SEARCH_CACHE, key, found)
    return found[:limit] if limit else found


def _parse_description(soup: BeautifulSoup) -> str:
    content = soup.select_one(".wp-content")
    if content:
        text = _clean(content.get_text(" ", strip=True))
        match = re.search(r"Sinopse:\s*(.*?)(?:Ano de Lan[cç]amento:|$)", text, re.I)
        if match:
            return _clean(match.group(1))
        if len(text) > 30:
            return text
    return _meta_content(soup, "og:description", "description")


def _parse_alt_titles(soup: BeautifulSoup) -> list[str]:
    content = soup.select_one(".wp-content")
    if not content:
        return []
    text = _clean(content.get_text(" ", strip=True))
    match = re.search(r"T[ií]tulo Alternativo:\s*(.*?)(?:Sinopse:|Ano de Lan[cç]amento:|$)", text, re.I)
    if not match:
        return []
    values = []
    for part in re.split(r"[,/;]", match.group(1)):
        value = _clean(part)
        if value and value not in values:
            values.append(value)
    return values[:8]


def _parse_genres(soup: BeautifulSoup) -> list[str]:
    genres = []
    seen = set()
    skipped = {
        "animes legendados",
        "animes dublado",
        "animes dublados",
        "manhwa",
        "donghua",
        "tokusatsus",
        "hentai (+18)",
    }
    for anchor in soup.select(".sgeneros a, a[href*='/genre/'], a[href*='/tipo/']"):
        text = _clean(anchor.get_text(" ", strip=True))
        if not text or re.search(r"letra\s+[a-z]|hentai|manhwa|donghua|tokusatsu", text, re.I):
            continue
        key = text.lower()
        if key in skipped:
            continue
        if key not in seen:
            seen.add(key)
            genres.append(text)
    return genres


def _parse_episodes_from_detail(soup: BeautifulSoup, anime_id: str) -> list[dict]:
    by_episode = {}
    for anchor in soup.select("a[href*='/episodio/']"):
        href = urljoin(BASE_URL, anchor.get("href") or "")
        slug = _slug_from_url(href)
        match = re.search(r"^(.+)-episodio-(\d+)$", slug, re.I)
        if not match:
            continue
        base_slug = match.group(1)
        if base_slug != anime_id:
            continue
        episode = int(match.group(2))
        title = _clean(anchor.get_text(" ", strip=True))
        title = re.sub(r"^Epis[oó]dio\s*\d+\s*-\s*", "", title, flags=re.I).strip()
        by_episode[episode] = {
            "episode": _episode_key(1, episode),
            "number": str(episode),
            "episode_number": episode,
            "season": 1,
            "title": title,
            "url": href,
            "base_slug": anime_id,
            "label": str(episode),
        }
    return [by_episode[key] for key in sorted(by_episode)]


async def get_anime_details(anime_id: str):
    anime_id = _normalize_anime_id(anime_id)
    cached = _cache_get(_DETAILS_CACHE, anime_id, _DETAILS_CACHE_TTL)
    if cached is not None:
        return cached

    url = _anime_url(anime_id)
    html_doc = await _request_text(url)
    soup = BeautifulSoup(html_doc, "html.parser")
    title = ""
    h1 = soup.select_one(".data h1, h1")
    if h1:
        title = _clean(h1.get_text(" ", strip=True))
        title = re.sub(r"^Home\s+Animes\s+", "", title, flags=re.I)
    title = title or _clean_title_from_page(_meta_content(soup, "og:title")) or anime_id.replace("-", " ").title()
    cover = _meta_content(soup, "og:image")
    description = _parse_description(soup)
    episodes = _parse_episodes_from_detail(soup, anime_id)
    text = _clean(soup.get_text(" ", strip=True))

    score = ""
    match = re.search(r"Your rating:\s*\d+\s*([0-9]+(?:[.,][0-9]+)?)\s+\d+\s+vote", text, re.I)
    if match:
        score = f"{match.group(1).replace(',', '.')}/10"

    year = ""
    match = re.search(r"Ano de Lan[cç]amento:\s*(\d{4})", text, re.I)
    if match:
        year = match.group(1)
    if not year:
        match = re.search(r"\b(20\d{2}|19\d{2})\b", text)
        if match:
            year = match.group(1)

    alt_titles = _parse_alt_titles(soup)
    data = {
        "id": anime_id,
        "title": title,
        "raw_title": title,
        "alt_titles": alt_titles,
        "description": description,
        "url": url,
        "cover_url": cover,
        "banner_url": cover,
        "media_image_url": cover,
        "score": score,
        "status": "N/A",
        "format": "TV",
        "episodes": len(episodes) or None,
        "season": "",
        "season_year": year,
        "genres": _parse_genres(soup),
        "studio": "AnimePlay",
        "source": "animeplay",
        "seasons": [1],
        "is_dubbed": _is_dubbed(title, anime_id),
    }
    anilist_data = await _search_anilist_by_title(title, alt_titles)
    data = _merge_anilist_data(data, anilist_data)

    final_alt_titles = []
    seen_alt = set()
    for value in [
        *alt_titles,
        data.get("title_romaji", ""),
        data.get("title_english", ""),
        data.get("title_native", ""),
        title,
    ]:
        value = _clean(value)
        key = value.lower()
        if value and key not in seen_alt and value != data.get("title"):
            seen_alt.add(key)
            final_alt_titles.append(value)
    data["alt_titles"] = final_alt_titles[:10]

    _cache_set(_DETAILS_CACHE, anime_id, data)
    _cache_set(_EPISODES_CACHE, anime_id, episodes)
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
            str(item.get("number") or ""),
            str(item.get("episode_number") or ""),
            f"1:{item.get('episode_number')}",
        }
        for key in keys:
            if key:
                by_episode[key] = index

    return {
        "items": page,
        "total": total,
        "by_episode": by_episode,
        "all_items": items,
        "seasons": [1],
    }


def _extract_direct_url(embed_url: str) -> str:
    embed_url = html.unescape(str(embed_url or "")).replace("\\/", "/").strip()
    if not embed_url:
        return ""
    parsed = urlsplit(embed_url)
    query = parse_qs(parsed.query)
    source = (query.get("source") or [""])[0]
    if source:
        return unquote(source).strip()
    if re.search(r"\.(?:mp4|m3u8)(?:\?|$)", embed_url, re.I):
        return embed_url
    return ""


async def _resolve_player_options(post_id: str, episode_url: str, options: list[dict]) -> dict[str, str]:
    videos = {}
    ordered_options = sorted(options, key=lambda item: 0 if int(item.get("nume") or 0) == 2 else int(item.get("nume") or 99))
    for option in ordered_options:
        try:
            data = await _post_json(
                f"{BASE_URL}/wp-admin/admin-ajax.php",
                {
                    "action": "doo_player_ajax",
                    "post": post_id,
                    "nume": str(option.get("nume")),
                    "type": option.get("type") or "tv",
                },
                referer=episode_url,
            )
        except Exception as error:
            print(f"[ANIMEPLAY] player_option_error={repr(error)}")
            continue

        direct_url = _extract_direct_url(data.get("embed_url") or "")
        if not direct_url:
            continue

        label = str(option.get("label") or "").upper()
        quality = "HD"
        if "MOBILE" in label or "CELULAR" in label:
            quality = "SD"
        elif "FULL" in label or "FHD" in label:
            quality = "HD"
        videos.setdefault(quality, direct_url)

    return videos


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
    index = by_episode.get(f"{season}:{episode_number}") or by_episode.get(_episode_key(season, episode_number)) or by_episode.get(str(episode_number))
    if index is None:
        raise RuntimeError(f"Episodio nao encontrado no AnimePlay: T{season}E{episode_number}")

    item = items[index]
    episode_url = item.get("url")
    page_html = await _request_text(episode_url, referer=_anime_url(anime_id))
    soup = BeautifulSoup(page_html, "html.parser")

    options = []
    for li in soup.select(".dooplay_player_option[data-post][data-nume]"):
        title_node = li.select_one(".title")
        options.append(
            {
                "post": str(li.get("data-post") or ""),
                "nume": int(li.get("data-nume") or 0),
                "type": str(li.get("data-type") or "tv"),
                "label": _clean(title_node.get_text(" ", strip=True) if title_node else ""),
            }
        )

    if not options:
        raise RuntimeError("Nao encontrei servidores do player no AnimePlay.")

    post_id = str(options[0].get("post") or "")
    videos = await _resolve_player_options(post_id, episode_url, options)
    if not videos:
        raise RuntimeError("AnimePlay nao retornou MP4/HLS direto para esse episodio.")

    preferred = "SD" if str(preferred_quality or "").upper() == "SD" else "HD"
    selected_quality = preferred if preferred in videos else ("HD" if "HD" in videos else next(iter(videos.keys())))
    video = videos.get(selected_quality) or ""

    prev_episode = str(items[index - 1].get("episode")) if index > 0 else None
    next_episode = str(items[index + 1].get("episode")) if index + 1 < len(items) else None
    data = {
        "video": video,
        "videos": videos,
        "base_slug": anime_id,
        "server": "ANIMEPLAY",
        "quality": selected_quality,
        "available_qualities": list(videos.keys()),
        "prev_episode": prev_episode,
        "next_episode": next_episode,
        "total_episodes": len(items),
        "season": 1,
        "episode_number": episode_number,
        "episode_title": item.get("title") or "",
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
        raise RuntimeError("Nenhum anime encontrado no AnimePlay.")
    return await get_anime_details(results[0]["id"])
