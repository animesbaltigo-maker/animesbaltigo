import asyncio
import html as html_lib
import random
import re
import time
import unicodedata
from urllib.parse import quote, unquote, urljoin

import httpx
from bs4 import BeautifulSoup

from core.http_client import get_http_client

BASE_URL = "https://animefire.io"
ANILIST_API_URL = "https://graphql.anilist.co"

PRIMARY_LIGHTSPEED_SERVERS = ["s6", "s7", "s5"]
SECONDARY_LIGHTSPEED_SERVERS = ["s4", "s8", "s3", "s2", "s1", "s9"]

ENABLE_ANILIST = True

_SEARCH_CACHE = {}
_DETAILS_CACHE = {}
_EPISODES_CACHE = {}
_VIDEO_CACHE = {}
_ANILIST_CACHE = {}
_HTML_CACHE = {}
_PLAYER_CACHE = {}

_INFLIGHT_SEARCH = {}
_INFLIGHT_DETAILS = {}
_INFLIGHT_EPISODES = {}
_INFLIGHT_VIDEO = {}
_INFLIGHT_ANILIST = {}
_INFLIGHT_HTML = {}
_INFLIGHT_PLAYER = {}

_SEARCH_CACHE_TTL = 1800
_DETAILS_CACHE_TTL = 43200
_EPISODES_CACHE_TTL = 180
_VIDEO_CACHE_TTL = 21600
_ANILIST_CACHE_TTL = 86400
_HTML_CACHE_TTL = 1800
_PLAYER_CACHE_TTL = 21600
_EMPTY_EPISODES_CACHE_TTL = 45

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": BASE_URL,
}

_ANILIST_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

HTTP_SEMAPHORE = asyncio.Semaphore(25)
VIDEO_CHECK_SEMAPHORE = asyncio.Semaphore(8)

GENRE_ALIASES = {
    "acao": ["acao", "ação", "action"],
    "romance": ["romance", "romantico", "romântico", "shoujo", "shojo"],
    "comedia": ["comedia", "comédia", "comedy"],
    "terror": ["terror", "horror", "sobrenatural"],
    "misterio": ["misterio", "mistério", "mystery", "suspense"],
    "fantasia": ["fantasia", "fantasy", "aventura"],
    "esportes": ["esporte", "esportes", "sports"],
    "drama": ["drama"],
}


def clear_search_cache():
    _SEARCH_CACHE.clear()
    _INFLIGHT_SEARCH.clear()


def _drop_matching_cache_entries(cache: dict, predicate) -> None:
    for key in list(cache.keys()):
        if predicate(key):
            cache.pop(key, None)


def _cancel_matching_inflight(inflight: dict, predicate) -> None:
    for key in list(inflight.keys()):
        if not predicate(key):
            continue

        task = inflight.pop(key, None)
        if task and not task.done():
            task.cancel()


def invalidate_episode_caches(anime_id: str, episode: str) -> None:
    normalized_anime_id = _normalize_slug_for_page(anime_id)
    normalized_base_slug = _normalize_episode_slug(anime_id)
    normalized_episode = str(episode or "").strip()

    if not normalized_episode:
        return

    player_prefix = f"{normalized_anime_id}|{normalized_episode}|"
    video_keys = {
        f"{normalized_base_slug}|{normalized_episode}",
        f"{normalized_anime_id}|{normalized_episode}",
    }
    episode_page_urls = {
        f"{BASE_URL}/animes/{normalized_base_slug}/{normalized_episode}",
        f"{BASE_URL}/animes/{normalized_anime_id}/{normalized_episode}",
    }

    _drop_matching_cache_entries(_PLAYER_CACHE, lambda key: str(key).startswith(player_prefix))
    _cancel_matching_inflight(_INFLIGHT_PLAYER, lambda key: str(key).startswith(player_prefix))

    _drop_matching_cache_entries(_VIDEO_CACHE, lambda key: str(key) in video_keys)
    _cancel_matching_inflight(_INFLIGHT_VIDEO, lambda key: str(key) in video_keys)

    _drop_matching_cache_entries(_HTML_CACHE, lambda key: str(key).rstrip("/") in episode_page_urls)
    _cancel_matching_inflight(_INFLIGHT_HTML, lambda key: str(key).rstrip("/") in episode_page_urls)
    invalidate_anime_episode_cache(anime_id)


def _related_episode_cache_keys(anime_id: str) -> set[str]:
    normalized_anime_id = _normalize_slug_for_page(anime_id)
    normalized_base_slug = _normalize_episode_slug(anime_id)
    keys = {normalized_anime_id}

    if normalized_base_slug:
        keys.add(normalized_base_slug)
        keys.add(f"{normalized_base_slug}-todos-os-episodios")

    return {key for key in keys if key}


def invalidate_anime_episode_cache(anime_id: str) -> None:
    cache_keys = _related_episode_cache_keys(anime_id)
    if not cache_keys:
        return

    _drop_matching_cache_entries(_EPISODES_CACHE, lambda key: str(key) in cache_keys)
    _cancel_matching_inflight(_INFLIGHT_EPISODES, lambda key: str(key) in cache_keys)

    anime_page_urls = {f"{BASE_URL}/animes/{key}" for key in cache_keys}
    _drop_matching_cache_entries(_HTML_CACHE, lambda key: str(key).rstrip("/") in anime_page_urls)
    _cancel_matching_inflight(_INFLIGHT_HTML, lambda key: str(key).rstrip("/") in anime_page_urls)


def _cache_get(cache: dict, key: str, ttl: int):
    item = cache.get(key)
    if not item:
        return None

    effective_ttl = int(item.get("ttl", ttl) or ttl)
    if time.time() - item["time"] > effective_ttl:
        cache.pop(key, None)
        return None

    return item["data"]


def _cache_set(cache: dict, key: str, data, ttl: int | None = None):
    item = {
        "time": time.time(),
        "data": data,
    }
    if ttl is not None:
        item["ttl"] = ttl
    cache[key] = item


async def _dedup_fetch(cache: dict, inflight: dict, key: str, ttl: int, coro_factory):
    cached = _cache_get(cache, key, ttl)
    if cached is not None:
        return cached

    task = inflight.get(key)
    if task:
        return await task

    async def _runner():
        return await coro_factory()

    task = asyncio.create_task(_runner())
    inflight[key] = task

    try:
        data = await task
        _cache_set(cache, key, data)
        return data
    finally:
        inflight.pop(key, None)


async def _request_text(url: str, headers: dict | None = None) -> str:
    client = await get_http_client()
    merged_headers = dict(_HTTP_HEADERS)
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
            await asyncio.sleep(0.5 * (attempt + 1))

    if last_exc:
        raise last_exc
    raise RuntimeError("Falha inesperada ao buscar texto.")


async def _get(url: str) -> str:
    return await _dedup_fetch(
        _HTML_CACHE,
        _INFLIGHT_HTML,
        url,
        _HTML_CACHE_TTL,
        lambda: _request_text(url, headers=_HTTP_HEADERS),
    )


async def _post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    client = await get_http_client()

    merged_headers = dict(_ANILIST_HEADERS)
    if headers:
        merged_headers.update(headers)

    last_exc = None

    for attempt in range(3):
        try:
            async with HTTP_SEMAPHORE:
                response = await client.post(url, json=payload, headers=merged_headers)
                response.raise_for_status()
                return response.json()
        except (httpx.PoolTimeout, httpx.ReadTimeout, httpx.ConnectTimeout) as error:
            last_exc = error
            await asyncio.sleep(0.5 * (attempt + 1))

    if last_exc:
        raise last_exc
    raise RuntimeError("Falha inesperada ao fazer POST JSON.")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _normalize_slug_for_page(anime_id: str) -> str:
    return (anime_id or "").strip().strip("/")


def _normalize_episode_slug(slug: str) -> str:
    slug = (slug or "").strip().strip("/")
    slug = slug.replace("-todos-os-episodios", "")
    return slug


def _normalize_text(text: str) -> str:
    text = (text or "").lower().strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _search_path_term(query: str) -> str:
    text = _normalize_text(query)
    text = text.replace(" ", "-")
    text = re.sub(r"-+", "-", text).strip("-")
    return text


def _extract_server_name(url: str) -> str:
    value = (url or "").lower()

    if "blogger.com/video.g" in value:
        return "BLOGGER"

    if "googlevideo.com" in value:
        return "GOOGLEVIDEO"

    if ".m3u8" in value:
        return "HLS"

    match = re.search(r"lightspeedst\.net/(s\d+)", value)
    return match.group(1).upper() if match else "S6"


def _extract_quality_name(url: str) -> str:
    value = (url or "").lower()

    if "fmt=37" in value or "1080p" in value:
        return "FULLHD"
    if "fmt=22" in value or "720p" in value or "/hd/" in value:
        return "HD"
    if "fmt=18" in value or "480p" in value or "/sd/" in value:
        return "SD"
    if "blogger.com/video.g" in value:
        return "HD"
    if ".m3u8" in value:
        if "1080" in value:
            return "FULLHD"
        if "720" in value:
            return "HD"
        if "480" in value or "360" in value:
            return "SD"
        return "HD"

    return "HD"


def _normalize_quality_label(value: str) -> str:
    value = (value or "").upper().strip()

    if value in {"FULLHD", "FHD", "1080P"}:
        return "FULLHD"
    if value in {"HD", "720P"}:
        return "HD"
    if value in {"SD", "480P", "360P"}:
        return "SD"

    return ""


def _extract_local_genres(soup: BeautifulSoup) -> list[str]:
    genres = []
    seen = set()

    for anchor in soup.select("a[href*='/genero/']"):
        text = _clean(anchor.get_text(" ", strip=True))
        if not text:
            continue

        key = text.lower()
        if key in seen:
            continue

        seen.add(key)
        genres.append(text)

    return genres


def _extract_alternative_titles(soup: BeautifulSoup, main_title: str = "") -> list[str]:
    titles = []
    seen = set()

    def _add(text: str):
        text = _clean(text)
        if not text or len(text) < 2:
            return

        low = text.lower()

        if main_title and low == main_title.lower():
            return

        if low in seen:
            return

        seen.add(low)
        titles.append(text)

    for el in soup.select("h6"):
        _add(el.get_text(" ", strip=True))

    meta = soup.find("meta", attrs={"property": "og:title"})
    if meta and meta.get("content"):
        raw = meta["content"]
        raw = re.sub(r"^Assistir\s+", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*-\s*AnimeFire.*$", "", raw, flags=re.IGNORECASE)
        _add(raw)

    return titles


def _is_dubbed_text(text: str) -> bool:
    text = _normalize_text(text)
    if not text:
        return False

    dubbed_terms = [
        "dublado",
        "dub",
        "pt br",
        "ptbr",
        "portugues",
        "português",
    ]
    return any(term in text for term in dubbed_terms)


def _clean_display_title(title: str) -> str:
    title = _clean(title)

    title = re.sub(r"\[.*?\]", " ", title)
    title = re.sub(r"\((?:tv|movie|filme|ova|ona|special)\)", " ", title, flags=re.IGNORECASE)

    title = re.sub(
        r"\b(dublado|legendado|dub|tv|movie|filme|ova|ona|special|online|hd|fullhd)\b",
        " ",
        title,
        flags=re.IGNORECASE,
    )

    title = re.sub(r"\s{2,}", " ", title).strip(" -–|")
    return title or "Sem título"


def _normalize_display_for_final_dedupe(title: str) -> str:
    value = _normalize_text(title)

    value = re.sub(r"\b(tv|movie|filme|ova|ona|special)\b", " ", value)
    value = re.sub(r"\(\s*\d{4}\s*\)", " ", value)
    value = re.sub(r"\b\d{4}\b", " ", value)

    value = re.sub(
        r"\b(dublado|legendado|dub|dual audio|audio dual|pt br|ptbr|portugues|português)\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(r"\s+", " ", value).strip(" -–|")
    return value


def _base_title_for_grouping(title: str, slug: str = "", alt_titles: list[str] | None = None) -> str:
    candidates = [title, slug.replace("-", " ")]

    if alt_titles:
        candidates.extend(alt_titles)

    best = ""

    for candidate in candidates:
        value = _normalize_text(candidate)
        if not value:
            continue

        value = re.sub(r"\[.*?\]", " ", value)
        value = re.sub(r"\((?:tv|movie|filme|ova|ona|special)\)", " ", value, flags=re.IGNORECASE)

        value = re.sub(
            r"\b(dublado|legendado|dub|dual audio|audio dual|pt br|ptbr|portugues|português|online|hd|fullhd|tv|movie|filme|ova|ona|special)\b",
            " ",
            value,
            flags=re.IGNORECASE,
        )

        value = re.sub(r"\s+", " ", value).strip(" -–|")
        if not value:
            continue

        if not best or len(value) < len(best):
            best = value

    return best or _normalize_text(title) or _normalize_text(slug)


def _pick_group_display_title(variants: list[dict]) -> str:
    if not variants:
        return "Sem título"

    def _title_score(item: dict):
        title = _clean(item.get("title") or "")
        normalized = _normalize_text(title)

        score = 0

        if not item.get("is_dubbed"):
            score += 100

        if "dublado" not in normalized:
            score += 30
        if "legendado" not in normalized:
            score += 20
        if "[" not in title and "]" not in title:
            score += 15
        if "(" not in title and ")" not in title:
            score += 10

        score -= len(title) * 0.1

        return score

    best = max(variants, key=_title_score)
    return _clean_display_title(best.get("title") or "Sem título")


def _score_candidate(query: str, title: str, slug: str, alt_titles: list[str] | None = None) -> float:
    q = _normalize_text(query)

    if not q:
        return -9999

    q_words = [w for w in q.split() if len(w) > 1]
    if not q_words:
        return -9999

    candidates = [
        title,
        slug.replace("-", " "),
    ]

    if alt_titles:
        candidates.extend(alt_titles)

    best_score = -9999

    for candidate_text in candidates:
        t = _normalize_text(candidate_text)
        if not t:
            continue

        score = 0.0

        if q == t:
            score += 1200
        if q in t:
            score += 600

        if len(q_words) == 1:
            word = q_words[0]
            if word not in t:
                score -= 500
            elif t.startswith(word):
                score += 140
        else:
            missing_words = 0
            for word in q_words:
                if word not in t:
                    missing_words += 1
            if missing_words >= max(1, len(q_words) // 2):
                score -= 400

        for word in q_words:
            if word in t:
                score += 80
            else:
                score -= 20

        if "episodio" in t or "episódio" in t:
            score -= 500

        score += max(0, 50 - len(t))

        if score > best_score:
            best_score = score

    return best_score


def _best_title_from_anilist(media: dict) -> str:
    title = media.get("title") or {}
    return (
        title.get("userPreferred")
        or title.get("romaji")
        or title.get("english")
        or title.get("native")
        or "Sem título"
    )


def _strip_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")


def _anilist_status_label(status: str) -> str:
    mapping = {
        "FINISHED": "Finalizado",
        "RELEASING": "Em lançamento",
        "NOT_YET_RELEASED": "Não lançado",
        "CANCELLED": "Cancelado",
        "HIATUS": "Em hiato",
    }
    return mapping.get((status or "").upper(), status or "")


def _anilist_format_label(fmt: str) -> str:
    mapping = {
        "TV": "TV",
        "TV_SHORT": "TV Short",
        "MOVIE": "Filme",
        "SPECIAL": "Especial",
        "OVA": "OVA",
        "ONA": "ONA",
        "MUSIC": "Music",
    }
    return mapping.get((fmt or "").upper(), fmt or "")


def _is_bad_description(text: str) -> bool:
    text = (text or "").strip().lower()
    if not text:
        return True

    bad_fragments = [
        "este site não hospeda nenhum vídeo em seu servidor",
        "todo conteúdo é provido de terceiros",
        "conteúdo é provido de terceiros",
        "assista",
        "baixar",
    ]

    return any(fragment in text for fragment in bad_fragments)


def _extract_description_from_page(soup: BeautifulSoup) -> str:
    text = soup.get_text("\n", strip=True)

    match = re.search(
        r"Sinopse:\s*(.+?)(?:\n[A-ZÁÉÍÓÚÂÊÔÃÕÇ][^\n]{0,60}:|\Z)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        description = _clean(match.group(1))
        if description and not _is_bad_description(description):
            return description

    paragraphs = []
    for paragraph in soup.find_all("p"):
        candidate = _clean(paragraph.get_text(" ", strip=True))
        if len(candidate) >= 80 and not _is_bad_description(candidate):
            paragraphs.append(candidate)

    if paragraphs:
        paragraphs.sort(key=len, reverse=True)
        return paragraphs[0]

    return ""


def _extract_blogger_iframe(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for iframe in soup.find_all("iframe"):
        src = (iframe.get("src") or "").strip()
        if "blogger.com/video.g" in src:
            return src

    match = re.search(r'https://www\.blogger\.com/video\.g\?token=[^"\']+', html)
    if match:
        return match.group(0)

    return ""


def _extract_googlevideo_url(html: str) -> str:
    match = re.search(r'https://[^"\']*googlevideo\.com/videoplayback[^"\']+', html)
    return match.group(0) if match else ""


def _decode_possible_escaped_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""

    value = html_lib.unescape(value)
    value = value.replace("\\/", "/")
    value = value.replace("\\u0026", "&")
    value = value.replace("\\x26", "&")
    value = value.replace("&amp;", "&")
    value = value.strip(" '\"")
    return value


def _make_absolute_url(url: str, base_url: str) -> str:
    url = _decode_possible_escaped_url(url)
    if not url:
        return ""
    return urljoin(base_url, url)


def _is_direct_video_url(url: str) -> bool:
    value = (url or "").lower()
    return any(
        token in value
        for token in (
            ".m3u8",
            ".mp4",
            "googlevideo.com/videoplayback",
            "/videoplayback?",
        )
    )


def _looks_like_embed_url(url: str) -> bool:
    value = (url or "").lower()
    return any(
        token in value
        for token in (
            "blogger.com/video.g",
            "/embed/",
            "player",
            "iframe",
        )
    )


def _extract_direct_video_urls(html: str, base_url: str = "") -> list[str]:
    if not html:
        return []

    candidates = []
    seen = set()

    def _push(url: str):
        url = _decode_possible_escaped_url(url)
        if not url:
            return

        if base_url:
            url = _make_absolute_url(url, base_url)

        if not url.startswith(("http://", "https://")):
            return

        if not _is_direct_video_url(url):
            return

        if url in seen:
            return

        seen.add(url)
        candidates.append(url)

    patterns = [
        r'https?://[^\s"\'<>\\]+\.m3u8(?:\?[^\s"\'<>\\]*)?',
        r'https?://[^\s"\'<>\\]+\.mp4(?:\?[^\s"\'<>\\]*)?',
        r'https?://[^\s"\'<>\\]*googlevideo\.com/videoplayback[^\s"\'<>\\]*',
        r'https?:\\/\\/[^\s"\'<>]+\.m3u8(?:\?[^\s"\'<>]*)?',
        r'https?:\\/\\/[^\s"\'<>]+\.mp4(?:\?[^\s"\'<>]*)?',
        r'https?:\\/\\/[^\s"\'<>]*googlevideo\.com\\/videoplayback[^\s"\'<>]*',
    ]

    for pattern in patterns:
        for match in re.findall(pattern, html, flags=re.IGNORECASE):
            _push(match)

    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(["source", "video"]):
        for attr in ("src", "data-src"):
            value = (tag.get(attr) or "").strip()
            if value:
                _push(value)

    for tag in soup.find_all(attrs={"data-video": True}):
        value = (tag.get("data-video") or "").strip()
        if value:
            _push(value)

    attr_patterns = [
        r'''["'](?:file|src|video|stream|url|hls|playlist)["']\s*:\s*["']([^"']+)["']''',
        r"""(?:file|src|video|stream|url|hls|playlist)\s*=\s*["']([^"']+)["']""",
    ]

    for pattern in attr_patterns:
        for match in re.findall(pattern, html, flags=re.IGNORECASE):
            _push(match)

    return candidates


def _extract_iframe_sources(html: str, base_url: str = "") -> list[str]:
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()

    for iframe in soup.find_all("iframe"):
        src = (iframe.get("src") or "").strip()
        src = _make_absolute_url(src, base_url) if base_url else _decode_possible_escaped_url(src)
        if not src:
            continue
        if src in seen:
            continue
        seen.add(src)
        results.append(src)

    return results


def _map_quality_urls(urls: list[str]) -> dict[str, str]:
    quality_map = {}

    for url in urls:
        quality = _normalize_quality_label(_extract_quality_name(url)) or "HD"
        quality_map.setdefault(quality, url)

    if "HD" not in quality_map:
        if "FULLHD" in quality_map:
            quality_map["HD"] = quality_map["FULLHD"]
        elif "SD" in quality_map:
            quality_map["HD"] = quality_map["SD"]

    return quality_map


async def _fetch_remote_html(url: str, referer: str = "") -> str:
    headers = dict(_HTTP_HEADERS)
    headers["Referer"] = referer or BASE_URL
    return await _request_text(url, headers=headers)


async def _resolve_embed_to_direct_urls(url: str, referer: str = "", depth: int = 0, visited: set[str] | None = None) -> list[str]:
    if not url or depth > 2:
        return []

    if visited is None:
        visited = set()

    normalized_url = _decode_possible_escaped_url(url)
    if normalized_url in visited:
        return []

    visited.add(normalized_url)

    if _is_direct_video_url(normalized_url):
        return [normalized_url]

    try:
        html = await _fetch_remote_html(normalized_url, referer=referer or BASE_URL)
    except Exception as error:
        print(f"[EMBED] erro_ao_buscar_embed={repr(error)} url={normalized_url}")
        return []

    direct_urls = _extract_direct_video_urls(html, base_url=normalized_url)
    if direct_urls:
        return direct_urls

    iframe_urls = _extract_iframe_sources(html, base_url=normalized_url)
    for iframe_url in iframe_urls:
        resolved = await _resolve_embed_to_direct_urls(
            iframe_url,
            referer=normalized_url,
            depth=depth + 1,
            visited=visited,
        )
        if resolved:
            return resolved

    return []


async def _get_episode_page_html(base_slug: str, episode: str) -> str:
    safe_slug = _normalize_episode_slug(base_slug)
    url = f"{BASE_URL}/animes/{safe_slug}/{episode}"
    return await _get(url)


def _merge_anime_data(local_data: dict, anilist_data: dict | None) -> dict:
    if not anilist_data:
        return local_data

    merged = dict(local_data)

    local_description = (local_data.get("description") or "").strip()
    anilist_description = (anilist_data.get("description") or "").strip()

    if not local_description and anilist_description:
        merged["description"] = anilist_data["description"]

    if anilist_data.get("cover_url"):
        merged["cover_url"] = anilist_data["cover_url"]

    if anilist_data.get("banner_url"):
        merged["banner_url"] = anilist_data["banner_url"]

    for key in (
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

    return merged


async def _search_anilist_by_title(title: str) -> dict | None:
    if not ENABLE_ANILIST:
        return None

    cache_key = _normalize_text(title)
    if not cache_key:
        return None

    async def _fetch():
        query = """
        query ($search: String) {
          Media(search: $search, type: ANIME) {
            id
            siteUrl
            title {
              romaji
              english
              native
              userPreferred
            }
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
        """

        payload = {
            "query": query,
            "variables": {"search": title},
        }

        try:
            data = await _post_json(ANILIST_API_URL, payload, headers=_ANILIST_HEADERS)
            media = ((data or {}).get("data") or {}).get("Media")
            if not media:
                return None

            studios = (((media.get("studios") or {}).get("nodes")) or [])
            studio_name = studios[0].get("name") if studios else ""

            description = _clean(_strip_html_tags(media.get("description") or ""))

            return {
                "anilist_id": media.get("id"),
                "anilist_url": media.get("siteUrl") or "",
                "title_romaji": ((media.get("title") or {}).get("romaji")) or "",
                "title_english": ((media.get("title") or {}).get("english")) or "",
                "title_native": ((media.get("title") or {}).get("native")) or "",
                "title": _best_title_from_anilist(media),
                "description": description,
                "score": media.get("averageScore"),
                "status": _anilist_status_label(media.get("status") or ""),
                "format": _anilist_format_label(media.get("format") or ""),
                "episodes": media.get("episodes"),
                "season": media.get("season") or "",
                "season_year": media.get("seasonYear"),
                "genres": media.get("genres") or [],
                "studio": studio_name,
                "banner_url": media.get("bannerImage") or "",
                "cover_url": (
                    ((media.get("coverImage") or {}).get("extraLarge"))
                    or ((media.get("coverImage") or {}).get("large"))
                    or ((media.get("coverImage") or {}).get("medium"))
                    or ""
                ),
                "media_image_url": f"https://img.anili.st/media/{media.get('id')}" if media.get("id") else "",
                "trailer_id": ((media.get("trailer") or {}).get("id")) or "",
                "trailer_site": ((media.get("trailer") or {}).get("site")) or "",
            }
        except Exception as error:
            print(f"[ANILIST] erro_na_busca={repr(error)}")
            return None

    return await _dedup_fetch(
        _ANILIST_CACHE,
        _INFLIGHT_ANILIST,
        cache_key,
        _ANILIST_CACHE_TTL,
        _fetch,
    )


async def search_anime(query: str):
    key = (query or "").strip().lower()

    async def _fetch():
        search_term = _search_path_term(query)
        url = f"{BASE_URL}/pesquisar/{quote(search_term)}"

        try:
            html_doc = await _get(url)
        except Exception as error:
            print(f"[BUSCA] erro_no_get={repr(error)}")
            raise

        soup = BeautifulSoup(html_doc, "html.parser")
        links = soup.select("a[href*='/animes/']")
        found = {}

        for anchor in links:
            href = (anchor.get("href") or "").strip()
            if "/animes/" not in href:
                continue

            slug = href.split("/animes/")[-1].strip("/")
            if not slug or "/" in slug:
                continue

            raw_title = _clean(anchor.get_text())
            if not raw_title:
                img = anchor.find("img")
                if img:
                    raw_title = _clean(img.get("alt"))

            if not raw_title:
                raw_title = slug.replace("-", " ").title()

            is_dubbed = _is_dubbed_text(raw_title) or _is_dubbed_text(slug)
            title = _clean_display_title(raw_title)

            score = _score_candidate(query, title, slug, alt_titles=[])
            if score <= -9999:
                continue

            item = {
                "id": slug,
                "title": title,
                "raw_title": raw_title,
                "alt_titles": [],
                "is_dubbed": is_dubbed,
                "_score": score,
            }
            previous = found.get(slug)
            if not previous or item["_score"] > previous["_score"]:
                found[slug] = item

        if len(found) < 5:
            extra_candidates = list(found.values())[:10]

            for item in extra_candidates:
                try:
                    details = await get_anime_details(item["id"])
                    alt_titles = details.get("alt_titles", [])

                    new_score = _score_candidate(
                        query,
                        details.get("title", item["title"]),
                        item["id"],
                        alt_titles=alt_titles,
                    )

                    if new_score > item["_score"]:
                        item["_score"] = new_score
                        item["title"] = details.get("title", item["title"])
                        item["alt_titles"] = alt_titles
                        item["is_dubbed"] = item.get("is_dubbed", False) or _is_dubbed_text(details.get("title", ""))

                    if alt_titles and not item.get("alt_titles"):
                        item["alt_titles"] = alt_titles

                except Exception:
                    pass

        ordered = sorted(found.values(), key=lambda x: (-x["_score"], x["title"].lower()))

        grouped = {}

        for item in ordered[:80]:
            base_key = _base_title_for_grouping(
                title=item.get("title", ""),
                slug=item.get("id", ""),
                alt_titles=item.get("alt_titles", []),
            )

            if not base_key:
                base_key = _normalize_text(item.get("title", "")) or item.get("id", "")

            group = grouped.get(base_key)
            if not group:
                group = {
                    "_group_key": base_key,
                    "_best_score": item.get("_score", 0),
                    "variants": [],
                    "has_dubbed": False,
                    "has_subbed": False,
                }
                grouped[base_key] = group
            else:
                if item.get("_score", 0) > group["_best_score"]:
                    group["_best_score"] = item.get("_score", 0)

            variant_payload = {
                "id": item["id"],
                "title": _clean_display_title(item.get("title") or "Sem título"),
                "raw_title": item.get("raw_title", item.get("title", "")),
                "alt_titles": item.get("alt_titles", []),
                "is_dubbed": bool(item.get("is_dubbed", False)),
            }

            existing_ids = {v["id"] for v in group["variants"]}
            if variant_payload["id"] in existing_ids:
                continue

            normalized_variant_title = _normalize_display_for_final_dedupe(variant_payload["title"])
            already_same_title = any(
                _normalize_display_for_final_dedupe(v.get("title", "")) == normalized_variant_title
                and bool(v.get("is_dubbed")) == bool(variant_payload["is_dubbed"])
                for v in group["variants"]
            )
            if already_same_title:
                continue

            group["variants"].append(variant_payload)

            if variant_payload["is_dubbed"]:
                group["has_dubbed"] = True
            else:
                group["has_subbed"] = True

        ordered_groups = sorted(
            grouped.values(),
            key=lambda g: (-g["_best_score"], _pick_group_display_title(g["variants"]).lower()),
        )

        used_display_titles = {}
        merged_final = []

        for group in ordered_groups:
            variants = group["variants"]
            if not variants:
                continue

            variants.sort(
                key=lambda v: (
                    1 if v.get("is_dubbed") else 0,
                    len(_clean(v.get("title") or "")),
                    _clean(v.get("title") or "").lower(),
                )
            )

            default_variant = next((v for v in variants if not v.get("is_dubbed")), None)
            if not default_variant:
                default_variant = variants[0]

            display_title = _pick_group_display_title(variants)
            normalized_display_title = _normalize_display_for_final_dedupe(display_title)

            item_payload = {
                "id": default_variant["id"],
                "default_anime_id": default_variant["id"],
                "title": display_title,
                "raw_title": default_variant.get("raw_title", display_title),
                "alt_titles": default_variant.get("alt_titles", []),
                "is_dubbed": default_variant.get("is_dubbed", False),
                "variants": variants[:],
                "has_dubbed": group["has_dubbed"],
                "has_subbed": group["has_subbed"],
                "_best_score": group["_best_score"],
            }

            existing_index = used_display_titles.get(normalized_display_title)

            if existing_index is None:
                used_display_titles[normalized_display_title] = len(merged_final)
                merged_final.append(item_payload)
                continue

            existing_item = merged_final[existing_index]

            existing_variants = existing_item.get("variants") or []
            existing_ids = {v["id"] for v in existing_variants}

            for variant in item_payload["variants"]:
                if variant["id"] not in existing_ids:
                    existing_variants.append(variant)
                    existing_ids.add(variant["id"])

            existing_item["variants"] = existing_variants
            existing_item["has_dubbed"] = existing_item["has_dubbed"] or item_payload["has_dubbed"]
            existing_item["has_subbed"] = existing_item["has_subbed"] or item_payload["has_subbed"]
            existing_item["_best_score"] = max(existing_item.get("_best_score", 0), item_payload.get("_best_score", 0))

        final_items = []

        merged_final.sort(key=lambda item: (-item.get("_best_score", 0), item.get("title", "").lower()))

        for item in merged_final:
            variants = item["variants"]

            deduped_variants = []
            seen_variant_titles = set()
            seen_variant_ids = set()

            for variant in variants:
                variant_id = variant.get("id")
                if variant_id in seen_variant_ids:
                    continue

                variant_title_key = (
                    _normalize_display_for_final_dedupe(variant.get("title", "")),
                    bool(variant.get("is_dubbed")),
                )
                if variant_title_key in seen_variant_titles:
                    continue

                seen_variant_ids.add(variant_id)
                seen_variant_titles.add(variant_title_key)
                deduped_variants.append(variant)

            deduped_variants.sort(
                key=lambda v: (
                    1 if v.get("is_dubbed") else 0,
                    len(_clean(v.get("title") or "")),
                    _clean(v.get("title") or "").lower(),
                )
            )

            default_variant = next((v for v in deduped_variants if not v.get("is_dubbed")), None)
            if not default_variant:
                default_variant = deduped_variants[0]

            item["variants"] = deduped_variants
            item["id"] = default_variant["id"]
            item["default_anime_id"] = default_variant["id"]
            item["title"] = _pick_group_display_title(deduped_variants)
            item["raw_title"] = default_variant.get("raw_title", item["title"])
            item["alt_titles"] = default_variant.get("alt_titles", [])
            item["is_dubbed"] = default_variant.get("is_dubbed", False)
            item.pop("_best_score", None)

            final_items.append(item)

            if len(final_items) >= 20:
                break

        return final_items

    return await _dedup_fetch(
        _SEARCH_CACHE,
        _INFLIGHT_SEARCH,
        key,
        _SEARCH_CACHE_TTL,
        _fetch,
    )


async def get_anime_details(anime_id: str):
    anime_id = _normalize_slug_for_page(anime_id)

    async def _fetch():
        url = f"{BASE_URL}/animes/{anime_id}"
        html_doc = await _get(url)
        soup = BeautifulSoup(html_doc, "html.parser")

        title_el = soup.find("h1")
        title = title_el.get_text(strip=True) if title_el else anime_id.replace("-", " ").title()

        alt_titles = _extract_alternative_titles(soup, title)
        description = _extract_description_from_page(soup)

        cover_url = ""
        og_img = soup.find("meta", attrs={"property": "og:image"})
        if og_img and og_img.get("content"):
            cover_url = og_img["content"].strip()

        if not cover_url:
            img = soup.find("img")
            if img and img.get("src"):
                cover_url = img["src"].strip()

        local_genres = _extract_local_genres(soup)

        local_data = {
            "id": anime_id,
            "title": title,
            "alt_titles": alt_titles,
            "description": description,
            "url": url,
            "cover_url": cover_url,
            "banner_url": "",
            "media_image_url": "",
            "score": None,
            "status": "",
            "format": "",
            "episodes": None,
            "season": "",
            "season_year": None,
            "genres": local_genres,
            "studio": "",
            "anilist_id": None,
            "anilist_url": "",
            "title_romaji": "",
            "title_english": "",
            "title_native": "",
            "trailer_id": "",
            "trailer_site": "",
        }

        anilist_data = await _search_anilist_by_title(title)
        merged = _merge_anime_data(local_data, anilist_data)

        final_alt_titles = []
        seen_alt = set()

        def _push_alt(value: str):
            value = _clean(value)
            if not value:
                return
            low = value.lower()
            if low == _clean(merged.get("title", "")).lower():
                return
            if low in seen_alt:
                return
            seen_alt.add(low)
            final_alt_titles.append(value)

        for item in local_data.get("alt_titles", []):
            _push_alt(item)

        for key_name in ("title_romaji", "title_english", "title_native"):
            _push_alt(merged.get(key_name, ""))

        merged["alt_titles"] = final_alt_titles
        return merged

    return await _dedup_fetch(
        _DETAILS_CACHE,
        _INFLIGHT_DETAILS,
        anime_id,
        _DETAILS_CACHE_TTL,
        _fetch,
    )


async def get_episodes(anime_id: str, offset: int = 0, limit: int = 3000):
    anime_id = _normalize_slug_for_page(anime_id)

    async def _fetch():
        url = f"{BASE_URL}/animes/{anime_id}"
        pattern = re.compile(r"/animes/([^/]+)/(\d+)(?:/)?$")

        def _extract_unique_episodes(html_doc: str) -> dict[str, dict]:
            soup = BeautifulSoup(html_doc, "html.parser")
            unique = {}

            for anchor in soup.select("a[href*='/animes/']"):
                href = (anchor.get("href") or "").strip()
                match = pattern.search(href)
                if not match:
                    continue

                base_slug = match.group(1)
                ep = match.group(2)

                unique[ep] = {
                    "episode": ep,
                    "base_slug": base_slug,
                }

            return unique

        html_doc = await _get(url)
        unique = _extract_unique_episodes(html_doc)

        if len(unique) <= 1:
            try:
                fresh_html_doc = await _request_text(url, headers=_HTTP_HEADERS)
                fresh_unique = _extract_unique_episodes(fresh_html_doc)
                if len(fresh_unique) > len(unique):
                    unique = fresh_unique
                    _cache_set(_HTML_CACHE, url, fresh_html_doc, ttl=_HTML_CACHE_TTL)
            except Exception as error:
                print(f"[EPISODES] refresh_html_error={repr(error)}")

        if len(unique) <= 1:
            try:
                from services.recent_episodes_client import get_recent_episodes

                related_ids = _related_episode_cache_keys(anime_id)
                recent_items = await get_recent_episodes(limit=60)

                for recent in recent_items:
                    recent_anime_id = _normalize_slug_for_page(recent.get("anime_id") or "")
                    if recent_anime_id not in related_ids:
                        continue

                    recent_episode = str(recent.get("episode") or "").strip()
                    if not recent_episode:
                        continue

                    unique.setdefault(
                        recent_episode,
                        {
                            "episode": recent_episode,
                            "base_slug": recent_anime_id or _normalize_episode_slug(anime_id),
                        },
                    )
            except Exception as error:
                print(f"[EPISODES] fallback_recent_error={repr(error)}")

        items = sorted(unique.values(), key=lambda x: int(x["episode"]))

        by_episode = {}
        for idx, item in enumerate(items):
            by_episode[str(item["episode"])] = idx

        return {
            "items": items,
            "total": len(items),
            "by_episode": by_episode,
        }

    payload = _cache_get(_EPISODES_CACHE, anime_id, _EPISODES_CACHE_TTL)
    if payload is None:
        task = _INFLIGHT_EPISODES.get(anime_id)
        if task is None:
            task = asyncio.create_task(_fetch())
            _INFLIGHT_EPISODES[anime_id] = task

        try:
            payload = await task
            ttl = _EMPTY_EPISODES_CACHE_TTL if payload.get("total", 0) <= 0 else _EPISODES_CACHE_TTL
            _cache_set(_EPISODES_CACHE, anime_id, payload, ttl=ttl)
        finally:
            _INFLIGHT_EPISODES.pop(anime_id, None)

    items = payload["items"]
    total = payload["total"]
    page = items[offset: offset + limit]

    return {
        "items": page,
        "total": total,
        "by_episode": payload["by_episode"],
        "all_items": items,
    }


async def _url_exists_with_client(client, url: str) -> bool:
    async with VIDEO_CHECK_SEMAPHORE:
        try:
            response = await client.head(url, follow_redirects=True)
            if response.status_code == 200:
                content_type = (response.headers.get("content-type") or "").lower()
                if (
                    "video" in content_type
                    or "mp4" in content_type
                    or "mpegurl" in content_type
                    or ".m3u8" in url.lower()
                    or content_type == ""
                ):
                    return True
        except Exception:
            pass

        try:
            response = await client.get(url, headers={"Range": "bytes=0-0"}, follow_redirects=True)
            if response.status_code in (200, 206):
                content_type = (response.headers.get("content-type") or "").lower()
                if (
                    "video" in content_type
                    or "mp4" in content_type
                    or "mpegurl" in content_type
                    or "octet-stream" in content_type
                    or ".m3u8" in url.lower()
                ):
                    return True
        except Exception:
            pass

    return False


async def _check_candidate(url: str):
    client = await get_http_client()
    ok = await _url_exists_with_client(client, url)
    return url if ok else None


def _build_candidate_urls(base_slug: str, episode: str, servers: list[str]):
    qualities = {
        "HD": [],
        "SD": [],
        "FULLHD": [],
    }

    for server in servers:
        base = f"https://lightspeedst.net/{server}"

        qualities["HD"].append(f"{base}/mp4_temp/{base_slug}/{episode}/720p.mp4")
        qualities["HD"].append(f"{base}/mp4/{base_slug}/hd/{episode}.mp4")

        qualities["SD"].append(f"{base}/mp4_temp/{base_slug}/{episode}/480p.mp4")
        qualities["SD"].append(f"{base}/mp4/{base_slug}/sd/{episode}.mp4")

        qualities["FULLHD"].append(f"{base}/mp4_temp/{base_slug}/{episode}/1080p.mp4")

    return qualities


async def _find_first_valid_url_in_batches(urls: list[str], batch_size: int = 3) -> str:
    if not urls:
        return ""

    for i in range(0, len(urls), batch_size):
        batch = urls[i:i + batch_size]
        tasks = [asyncio.create_task(_check_candidate(url)) for url in batch]

        try:
            for task in asyncio.as_completed(tasks):
                result = await task
                if result:
                    for pending in tasks:
                        if pending is not task and not pending.done():
                            pending.cancel()
                    return result
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    return ""


async def _try_lightspeed_urls(base_slug: str, episode: str):
    quality_map = {}

    primary_candidates = _build_candidate_urls(base_slug, episode, PRIMARY_LIGHTSPEED_SERVERS)

    for quality in ("HD", "SD", "FULLHD"):
        found = await _find_first_valid_url_in_batches(primary_candidates.get(quality, []), batch_size=3)
        if found:
            quality_map[quality] = found

    if not quality_map:
        secondary_candidates = _build_candidate_urls(base_slug, episode, SECONDARY_LIGHTSPEED_SERVERS)
        for quality in ("HD", "SD", "FULLHD"):
            found = await _find_first_valid_url_in_batches(secondary_candidates.get(quality, []), batch_size=3)
            if found:
                quality_map[quality] = found

    return quality_map


async def _try_blogger_or_googlevideo(base_slug: str, episode: str) -> dict[str, str]:
    quality_map: dict[str, str] = {}

    try:
        episode_html = await _get_episode_page_html(base_slug, episode)
        episode_url = f"{BASE_URL}/animes/{_normalize_episode_slug(base_slug)}/{episode}"
        fallback_embed = ""

        direct_from_page = _extract_direct_video_urls(episode_html, base_url=episode_url)
        if direct_from_page:
            quality_map.update(_map_quality_urls(direct_from_page))
            if quality_map:
                return quality_map

        direct_googlevideo = _extract_googlevideo_url(episode_html)
        if direct_googlevideo:
            quality = _normalize_quality_label(_extract_quality_name(direct_googlevideo)) or "HD"
            quality_map[quality] = direct_googlevideo
            return quality_map

        blogger_iframe = _extract_blogger_iframe(episode_html)
        if blogger_iframe:
            fallback_embed = _decode_possible_escaped_url(blogger_iframe)
            resolved_urls = await _resolve_embed_to_direct_urls(
                blogger_iframe,
                referer=episode_url,
            )
            if resolved_urls:
                quality_map.update(_map_quality_urls(resolved_urls))
                if quality_map:
                    return quality_map

        iframe_sources = _extract_iframe_sources(episode_html, base_url=episode_url)
        for iframe_src in iframe_sources:
            if not _looks_like_embed_url(iframe_src) and not _is_direct_video_url(iframe_src):
                continue

            if not fallback_embed and _looks_like_embed_url(iframe_src):
                fallback_embed = _decode_possible_escaped_url(iframe_src)

            resolved_urls = await _resolve_embed_to_direct_urls(
                iframe_src,
                referer=episode_url,
            )
            if resolved_urls:
                quality_map.update(_map_quality_urls(resolved_urls))
                if quality_map:
                    return quality_map

        if fallback_embed:
            quality_map["HD"] = fallback_embed

    except Exception as error:
        print(f"[BLOGGER] erro_na_extracao={repr(error)}")

    return quality_map


async def _resolve_video_map(base_slug: str, episode: str, anime_id: str | None = None):
    cache_key = f"{base_slug}|{episode}"

    async def _fetch():
        safe_base_slug = _normalize_episode_slug(base_slug)
        safe_anime_id = _normalize_episode_slug(anime_id or "")
        target_slug = safe_base_slug or safe_anime_id

        # Prefer the source declared on the episode page before guessing
        # lightspeed URLs from the slug.
        quality_map = await _try_blogger_or_googlevideo(target_slug, episode)

        if not quality_map:
            quality_map = await _try_lightspeed_urls(target_slug, episode)

        return quality_map

    return await _dedup_fetch(
        _VIDEO_CACHE,
        _INFLIGHT_VIDEO,
        cache_key,
        _VIDEO_CACHE_TTL,
        _fetch,
    )


async def get_episode_player(anime_id: str, episode: str, preferred_quality: str = "HD"):
    anime_id = _normalize_slug_for_page(anime_id)
    preferred_quality = _normalize_quality_label(preferred_quality) or "HD"
    player_cache_key = f"{anime_id}|{episode}|{preferred_quality}"

    async def _fetch():
        payload = await get_episodes(anime_id, 0, 3000)
        items = payload.get("all_items", [])
        by_episode = payload.get("by_episode", {})

        index = by_episode.get(str(episode))
        base_slug = None

        if index is not None:
            base_slug = items[index].get("base_slug")

        if not base_slug:
            base_slug = anime_id.replace("-todos-os-episodios", "")

        quality_map = await _resolve_video_map(base_slug, episode, anime_id=anime_id)
        available_qualities = [q for q in ("FULLHD", "HD", "SD") if q in quality_map]

        if preferred_quality in quality_map:
            selected_quality = preferred_quality
        elif preferred_quality == "FULLHD":
            if "HD" in quality_map:
                selected_quality = "HD"
            elif "SD" in quality_map:
                selected_quality = "SD"
            else:
                selected_quality = "FULLHD"
        elif preferred_quality == "HD":
            if "FULLHD" in quality_map:
                selected_quality = "FULLHD"
            elif "SD" in quality_map:
                selected_quality = "SD"
            else:
                selected_quality = "HD"
        else:
            if "HD" in quality_map:
                selected_quality = "HD"
            elif "FULLHD" in quality_map:
                selected_quality = "FULLHD"
            else:
                selected_quality = "SD"

        video = (quality_map.get(selected_quality) or "").strip()

        if not video:
            for fallback_quality in ("FULLHD", "HD", "SD"):
                fallback_video = (quality_map.get(fallback_quality) or "").strip()
                if fallback_video:
                    selected_quality = fallback_quality
                    video = fallback_video
                    break

        server = _extract_server_name(video)
        quality = _extract_quality_name(video) if video else selected_quality

        prev_episode = None
        next_episode = None

        if index is not None:
            if index > 0:
                prev_episode = str(items[index - 1]["episode"])
            if index + 1 < len(items):
                next_episode = str(items[index + 1]["episode"])

        return {
            "video": video,
            "videos": quality_map,
            "base_slug": base_slug,
            "server": server,
            "quality": quality,
            "available_qualities": available_qualities,
            "prev_episode": prev_episode,
            "next_episode": next_episode,
            "total_episodes": len(items),
        }

    return await _dedup_fetch(
        _PLAYER_CACHE,
        _INFLIGHT_PLAYER,
        player_cache_key,
        _PLAYER_CACHE_TTL,
        _fetch,
    )


def _extract_anime_links_from_listing(html_doc: str) -> list[dict]:
    soup = BeautifulSoup(html_doc, "html.parser")
    found = {}

    for anchor in soup.select("a[href*='/animes/']"):
        href = (anchor.get("href") or "").strip()
        if "/animes/" not in href:
            continue

        slug = href.split("/animes/")[-1].strip("/")
        if not slug or "/" in slug:
            continue

        title = _clean(anchor.get_text())
        if not title:
            img = anchor.find("img")
            if img:
                title = _clean(img.get("alt"))

        if not title:
            title = slug.replace("-", " ").title()

        found[slug] = {
            "id": slug,
            "title": title,
        }

    return list(found.values())


async def _get_genre_listing_candidates(genre_key: str) -> list[dict]:
    aliases = GENRE_ALIASES.get((genre_key or "").strip().lower(), [])
    if not aliases:
        return []

    items = {}

    for alias in aliases:
        alias = alias.strip().lower()

        possible_urls = [
            f"{BASE_URL}/genero/{quote(alias)}",
            f"{BASE_URL}/animes/genero/{quote(alias)}",
            f"{BASE_URL}/categoria/{quote(alias)}",
            f"{BASE_URL}/{quote(alias)}",
        ]

        for url in possible_urls:
            try:
                html_doc = await _get(url)
                found = _extract_anime_links_from_listing(html_doc)
                for item in found:
                    items[item["id"]] = item

                if len(items) >= 20:
                    return list(items.values())
            except Exception:
                continue

    return list(items.values())


async def get_random_anime_by_genre(genre_key: str, exclude_anime_id: str | None = None) -> dict:
    found = await _get_genre_listing_candidates(genre_key)

    if not found:
        raise RuntimeError(f"Nenhum anime encontrado para o gênero {genre_key}.")

    if exclude_anime_id:
        filtered = [item for item in found if item["id"] != exclude_anime_id]
        if filtered:
            found = filtered

    chosen = random.choice(found)
    return await get_anime_details(chosen["id"])


def warmup_popular_anime_ids() -> list[str]:
    return [
        "one-piece",
        "naruto",
        "solo-leveling",
        "jujutsu-kaisen",
        "boku-no-hero-academia",
        "kimetsu-no-yaiba",
        "black-clover",
        "bleach",
    ]


async def preload_popular_cache():
    for anime_id in warmup_popular_anime_ids():
        try:
            await get_anime_details(anime_id)
            await get_episodes(anime_id, 0, 3000)
        except Exception as error:
            print(f"[WARMUP] erro_em_{anime_id}={repr(error)}")
