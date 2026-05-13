import asyncio
import html
import json
import re
import time
from urllib.parse import parse_qs, quote, quote_plus, unquote, urljoin, urlsplit, urlunsplit

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
_PLAYER_LOCKS = {}

_SEARCH_CACHE_TTL = 1800
_DETAILS_CACHE_TTL = 21600
_EPISODES_CACHE_TTL = 21600
_PLAYER_CACHE_TTL = 180
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
        return None
    return item["data"]


def _cache_set(cache: dict, key: str, data) -> None:
    cache[key] = {"time": time.time(), "data": data}


def _cache_get_stale(cache: dict, key: str):
    item = cache.get(key)
    return item["data"] if item else None


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
    text = re.sub(r"\b(?:temporada|season)\s*\d+\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_variant_title(value: str | None) -> str:
    text = _clean(value)
    text = re.sub(r"\s*[–—-]\s*(?:Todos os Epis[oó]dios|AnimePlay\.Cloud).*$", "", text, flags=re.I)
    text = re.sub(r"\b(?:Dublado|Legendado)\b", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" -–—")
    return text or _clean(value)


def _group_key_for_item(item: dict) -> str:
    title = item.get("title") or item.get("raw_title") or item.get("id") or ""
    key = _normalize_text(_clean_variant_title(title))
    if not key:
        key = _normalize_text(item.get("id") or "")
    return key


def _query_match_score(query: str, item: dict) -> int:
    q_norm = _normalize_text(query)
    if not q_norm:
        return 1
    hay = _normalize_text(" ".join([
        item.get("title") or "",
        item.get("raw_title") or "",
        item.get("id") or "",
        " ".join(item.get("alt_titles") or []),
    ]))
    if not hay:
        return 0
    if q_norm == hay:
        return 100
    if q_norm in hay:
        return 90
    q_tokens = [token for token in q_norm.split() if token]
    hay_tokens = set(hay.split())
    if not q_tokens:
        return 0
    overlap = sum(1 for token in q_tokens if token in hay_tokens or token in hay)
    if len(q_tokens) > 1 and overlap < len(q_tokens):
        return 0
    return int((overlap / len(q_tokens)) * 70)


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


def _fallback_anime_id(anime_id: str) -> str:
    value = _normalize_anime_id(anime_id)
    candidates = [
        re.sub(r"-\d+(?:st|nd|rd|th)?-season.*$", "", value),
        re.sub(r"-season-\d+.*$", "", value),
        re.sub(r"-(?:part|cour)-\d+.*$", "", value),
        re.sub(r"-\d+$", "", value),
    ]
    for fallback in candidates:
        fallback = fallback.strip("-")
        if fallback and fallback != value:
            return fallback
    return ""


def _search_query_from_anime_id(anime_id: str) -> str:
    value = _normalize_anime_id(anime_id)
    value = re.sub(r"-(?:legendado|dublado|dub|sub)$", "", value, flags=re.I)
    value = re.sub(r"-todos-os-episodios$", "", value, flags=re.I)
    value = re.sub(r"-\d+(?:st|nd|rd|th)?-season.*$", "", value, flags=re.I)
    value = re.sub(r"-season-\d+.*$", "", value, flags=re.I)
    value = re.sub(r"-(?:part|cour)-\d+.*$", "", value, flags=re.I)
    value = re.sub(r"-\d+$", "", value)
    return re.sub(r"\s+", " ", value.replace("-", " ")).strip()


def _search_queries_from_anime_id(anime_id: str) -> list[str]:
    value = _normalize_anime_id(anime_id)
    raw = re.sub(r"-(?:legendado|dublado|dub|sub)$", "", value, flags=re.I)
    raw = re.sub(r"-todos-os-episodios$", "", raw, flags=re.I)
    raw = re.sub(r"\s+", " ", raw.replace("-", " ")).strip()
    cleaned = _search_query_from_anime_id(anime_id)
    queries = []
    for query in (raw, cleaned):
        if query and query not in queries:
            queries.append(query)
    return queries


async def _resolve_existing_anime_id(anime_id: str) -> str:
    queries = _search_queries_from_anime_id(anime_id)
    if not queries:
        return ""

    wanted_dub = _is_dubbed("", anime_id)
    normalized_original = _normalize_text(anime_id)
    desired_number = ""
    numbers = re.findall(r"(?:^|-)(\d+)(?=-|$)", _normalize_anime_id(anime_id))
    if numbers:
        desired_number = numbers[-1]
    first_candidate = ""
    for query in queries:
        try:
            results = await search_anime(query, limit=12)
        except Exception:
            continue

        normalized_query = _normalize_text(query)
        if desired_number:
            for item in results:
                candidates = [item, *(item.get("variants") or [])]
                for candidate in candidates:
                    candidate_id = candidate.get("id") or candidate.get("default_anime_id") or ""
                    if not candidate_id:
                        continue
                    if wanted_dub != _is_dubbed(candidate.get("title") or "", candidate_id):
                        continue
                    if re.search(rf"-{re.escape(desired_number)}(?:-(?:dublado|legendado|dub|sub))?$", candidate_id, re.I):
                        return candidate_id

        for item in results:
            candidates = [item, *(item.get("variants") or [])]
            for candidate in candidates:
                candidate_id = candidate.get("id") or candidate.get("default_anime_id") or ""
                if not candidate_id:
                    continue
                if not first_candidate:
                    first_candidate = candidate_id
                if wanted_dub != _is_dubbed(candidate.get("title") or "", candidate_id):
                    continue
                candidate_norm = _normalize_text(candidate_id)
                if candidate_norm and (
                    candidate_norm in normalized_original
                    or normalized_query in candidate_norm
                    or candidate_norm in normalized_query
                ):
                    return candidate_id

        for item in results:
            candidate_id = item.get("default_anime_id") or item.get("id") or ""
            if candidate_id and not first_candidate:
                first_candidate = candidate_id
    if first_candidate:
        return first_candidate
    return ""


def _parse_episode_ref(value: str) -> tuple[int, int]:
    raw = str(value or "").strip()
    match = re.search(r"^[sStT]?(\d+)[eE:.-](\d+)$", raw)
    if match:
        return int(match.group(1)), int(match.group(2))
    digits = re.search(r"\d+", raw)
    return 1, int(digits.group(0)) if digits else 1


def _episode_key(season: int, episode: int) -> str:
    return f"S{int(season)}E{int(episode)}"


def _episode_lookup_keys(season: int, episode: int) -> list[str]:
    season = int(season or 1)
    episode = int(episode or 1)
    return [
        f"{season}:{episode}",
        _episode_key(season, episode),
        f"S{season:02d}E{episode:02d}",
        f"T{season}E{episode}",
        f"T{season:02d}E{episode:02d}",
        str(episode),
        f"{episode:02d}",
    ]


def _find_episode_index(items: list[dict], by_episode: dict, season: int, episode: int) -> int | None:
    for key in _episode_lookup_keys(season, episode):
        index = by_episode.get(key)
        if index is not None:
            return index

    for index, item in enumerate(items):
        try:
            item_season = int(item.get("season") or 1)
            item_episode = int(item.get("episode_number") or item.get("number") or 0)
        except Exception:
            continue
        if item_season == int(season or 1) and item_episode == int(episode or 1):
            return index
    return None


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

    candidates = []
    for value in [title, _clean_variant_title(title), *(alt_titles or [])]:
        value = _clean(value)
        if value and value not in candidates:
            candidates.append(value)
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
        "score",
        "status",
        "format",
        "season",
        "season_year",
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

    if not merged.get("episodes") and anilist_data.get("episodes"):
        merged["episodes"] = anilist_data["episodes"]
    if anilist_data.get("cover_url"):
        merged["cover_url"] = anilist_data["cover_url"]
        merged["media_image_url"] = anilist_data.get("media_image_url") or anilist_data["cover_url"]
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


def _media_url_from_node(node) -> str:
    if not node:
        return ""
    candidates = []
    parent = getattr(node, "parent", None)
    grandparent = getattr(parent, "parent", None)
    for current in (node, parent, grandparent):
        if not current:
            continue
        if getattr(current, "get", None):
            candidates.extend([current.get("data-src"), current.get("data-lazy-src"), current.get("data-bg")])
            match = re.search(r"url\((['\"]?)(.*?)\1\)", current.get("style") or "", re.I)
            if match:
                candidates.append(match.group(2))
        if getattr(current, "select", None):
            for img in current.select("img"):
                candidates.extend([img.get("data-src"), img.get("data-lazy-src"), img.get("src")])
                srcset = img.get("srcset") or ""
                if srcset:
                    candidates.append(srcset.split(",", 1)[0].strip().split(" ", 1)[0])
            for media in current.select("[data-src], [data-lazy-src], [data-bg], [style*='background']"):
                candidates.extend([media.get("data-src"), media.get("data-lazy-src"), media.get("data-bg")])
                match = re.search(r"url\((['\"]?)(.*?)\1\)", media.get("style") or "", re.I)
                if match:
                    candidates.append(match.group(2))
    for value in candidates:
        value = _clean(str(value or "")).replace("\\/", "/")
        if value and re.search(r"\.(?:webp|jpe?g|png|gif|avif)(?:\?|$)", value, re.I):
            return urljoin(BASE_URL, value)
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

        container = anchor.find_parent(["article", "li", "div"])
        title = _clean(anchor.get_text(" ", strip=True))
        img = anchor.find("img") or (container.find("img") if container else None)
        cover_url = _media_url_from_node(anchor)
        if img:
            title = title or _clean(img.get("alt"))
        if not title or re.fullmatch(r"(?:TV|OVA|ONA|Filme|Movie|Anime)", title, re.I):
            if img and _clean(img.get("alt")):
                title = _clean(img.get("alt"))
            elif container:
                title_node = container.select_one(".data h3 a, .data h3, .data h2, h3, h2")
                if title_node:
                    title = _clean(title_node.get_text(" ", strip=True))

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

    items = list(found.values())
    if query:
        filtered = []
        for item in items:
            match_score = _query_match_score(query, item)
            if match_score <= 0:
                continue
            q_tokens = _normalize_text(query).split()
            title_tokens = _normalize_text(item.get("title") or item.get("id") or "").split()
            extra_tokens = max(0, len(title_tokens) - len(q_tokens))
            exact_bonus = 30 if _group_key_for_item(item) == _normalize_text(query) else 0
            dubbed_penalty = 8 if item.get("is_dubbed") and not re.search(r"\bdublado\b", query or "", re.I) else 0
            item["_score"] = match_score + exact_bonus - min(30, extra_tokens * 3) - dubbed_penalty
            filtered.append(item)
        items = filtered

    return sorted(items, key=lambda item: item.get("_score", 0), reverse=True)


def _group_search_results(items: list[dict], query: str = "") -> list[dict]:
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for item in items:
        key = _group_key_for_item(item)
        if not key:
            key = item.get("id") or item.get("title") or str(len(order))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)

    grouped: list[dict] = []
    for key in order:
        variants = groups[key]
        variants.sort(
            key=lambda item: (
                1 if _is_dubbed(item.get("title") or "", item.get("id") or "") else 0,
                -(item.get("_score") or 0),
                item.get("title") or "",
            )
        )
        wants_dubbed = bool(re.search(r"\bdublado\b", query or "", re.I))
        default = next(
            (
                item
                for item in variants
                if _is_dubbed(item.get("title") or "", item.get("id") or "") == wants_dubbed
            ),
            variants[0],
        )
        clean_title = _clean_variant_title(default.get("title") or "")
        shaped_variants = []
        for variant in variants:
            current = dict(variant)
            current["title"] = _clean(current.get("title") or current.get("id") or "")
            current["label"] = "Dublado" if _is_dubbed(current.get("title") or "", current.get("id") or "") else "Legendado"
            current["is_dubbed"] = current["label"] == "Dublado"
            current.pop("_score", None)
            shaped_variants.append(current)
        item = dict(default)
        item["title"] = clean_title or item.get("title") or item.get("id", "").replace("-", " ").title()
        item["display_title"] = item["title"]
        item["default_anime_id"] = default.get("id")
        item["variants"] = shaped_variants
        item["is_grouped"] = len(shaped_variants) > 1
        item["has_dubbed"] = any(variant.get("is_dubbed") for variant in shaped_variants)
        item["has_subbed"] = any(not variant.get("is_dubbed") for variant in shaped_variants)
        item["prefix"] = "DUB" if item["has_dubbed"] and not item["has_subbed"] else "LEG"
        grouped.append(item)
    return grouped


async def search_anime(query: str, limit: int | None = None):
    key = (query or "").strip().lower()
    if not key:
        return []

    cached = _cache_get(_SEARCH_CACHE, key, _SEARCH_CACHE_TTL)
    if cached is not None:
        return cached[:limit] if limit else cached

    html_doc = await _request_text(f"{BASE_URL}/?s={quote_plus(query)}")
    found = _group_search_results(_extract_anime_cards(html_doc, query=query), query=query)
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
        "legendado",
        "animes dublado",
        "animes dublados",
        "dublado",
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
        if not slug:
            continue
        li = anchor.find_parent("li")
        season = 1
        episode = None
        numerando = _clean(li.select_one(".numerando").get_text(" ", strip=True) if li and li.select_one(".numerando") else "")
        match = re.search(r"^\s*(\d+)\s*-\s*(\d+(?:[.,]\d+)?)", numerando)
        if match:
            season = int(match.group(1))
            episode = int(float(match.group(2).replace(",", ".")))
        else:
            match = re.search(r"^(.+)-episodio-(\d+)$", slug, re.I)
            if not match:
                continue
            episode = int(match.group(2))
        title = _clean(anchor.get_text(" ", strip=True))
        title = re.sub(r"^Epis[oó]dio\s*\d+\s*-\s*", "", title, flags=re.I).strip()
        thumb = _media_url_from_node(anchor)
        key = f"{season}:{episode}:{href}"
        by_episode[key] = {
            "episode": _episode_key(season, episode),
            "number": str(episode),
            "episode_number": episode,
            "season": season,
            "title": title,
            "thumb": thumb,
            "image": thumb,
            "url": href,
            "base_slug": anime_id,
            "label": str(episode),
        }
    return sorted(by_episode.values(), key=lambda item: (int(item.get("season") or 1), int(item.get("episode_number") or 0), item.get("url") or ""))


async def get_anime_details(anime_id: str):
    anime_id = _normalize_anime_id(anime_id)
    cached = _cache_get(_DETAILS_CACHE, anime_id, _DETAILS_CACHE_TTL)
    if cached is not None:
        return cached

    url = _anime_url(anime_id)
    try:
        html_doc = await _request_text(url)
    except httpx.HTTPStatusError as error:
        status = error.response.status_code if error.response is not None else 0
        fallback_id = _fallback_anime_id(anime_id)
        if status == 404:
            resolved_id = await _resolve_existing_anime_id(anime_id)
            if resolved_id and resolved_id != anime_id:
                data = await get_anime_details(resolved_id)
                _cache_set(_DETAILS_CACHE, anime_id, data)
                if _cache_get_stale(_EPISODES_CACHE, resolved_id):
                    _cache_set(_EPISODES_CACHE, anime_id, _cache_get_stale(_EPISODES_CACHE, resolved_id))
                return data
        if status == 404 and fallback_id:
            try:
                data = await get_anime_details(fallback_id)
                _cache_set(_DETAILS_CACHE, anime_id, data)
                if _cache_get_stale(_EPISODES_CACHE, fallback_id):
                    _cache_set(_EPISODES_CACHE, anime_id, _cache_get_stale(_EPISODES_CACHE, fallback_id))
                return data
            except httpx.HTTPStatusError as fallback_error:
                fallback_status = fallback_error.response.status_code if fallback_error.response is not None else 0
                if fallback_status != 404:
                    raise
            except Exception:
                pass
        raise
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
        "status": "",
        "format": "TV",
        "episodes": len(episodes) or None,
        "season": "",
        "season_year": year,
        "genres": _parse_genres(soup),
        "studio": "AnimePlay",
        "source": "animeplay",
        "seasons": sorted({int(item.get("season") or 1) for item in episodes}) or [1],
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

    stale_episodes = _cache_get_stale(_EPISODES_CACHE, anime_id) or []
    if not episodes and stale_episodes:
        episodes = stale_episodes
        data["episodes"] = len(episodes)
        data["seasons"] = sorted({int(item.get("season") or 1) for item in episodes}) or [1]

    if episodes:
        _cache_set(_DETAILS_CACHE, anime_id, data)
        _cache_set(_EPISODES_CACHE, anime_id, episodes)
    elif stale_episodes:
        _cache_set(_DETAILS_CACHE, anime_id, data)
    return data


async def get_episodes(anime_id: str, offset: int = 0, limit: int = 3000):
    anime_id = _normalize_anime_id(anime_id)
    items = _cache_get(_EPISODES_CACHE, anime_id, _EPISODES_CACHE_TTL)
    if items is None:
        stale = _cache_get_stale(_EPISODES_CACHE, anime_id) or []
        try:
            await get_anime_details(anime_id)
            items = _cache_get(_EPISODES_CACHE, anime_id, _EPISODES_CACHE_TTL) or stale
        except Exception:
            if stale:
                items = stale
            else:
                raise
        if not items and stale:
            items = stale

    total = len(items)
    page = items[offset: offset + limit] if limit else items[offset:]
    by_episode = {}
    for index, item in enumerate(items):
        season = int(item.get("season") or 1)
        episode_number = int(item.get("episode_number") or 0)
        keys = {
            str(item.get("episode") or ""),
            str(item.get("number") or ""),
            str(episode_number or ""),
            f"{season}:{episode_number}",
            *_episode_lookup_keys(season, episode_number),
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


def _is_direct_video_url(url: str) -> bool:
    value = str(url or "")
    if re.search(r"\.(?:mp4|m3u8|webm)(?:\?|$)", value, re.I):
        return True
    try:
        query = parse_qs(urlsplit(value).query)
        for key in ("f", "file", "source", "src", "url", "video"):
            for candidate in query.get(key) or []:
                if re.search(r"\.(?:mp4|m3u8|webm)(?:\?|$)", candidate, re.I):
                    return True
    except Exception:
        pass
    return bool(re.search(r"(?:^https?://[^/]*googlevideo\.com/|/)(?:videoplayback)(?:\?|$)", value, re.I))


def _is_trusted_embed_url(url: str) -> bool:
    try:
        parsed = urlsplit(str(url or ""))
    except Exception:
        return False
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    return host in {"blogger.com", "www.blogger.com"} and path.endswith("/video.g")


def _make_absolute_url(url: str, base_url: str) -> str:
    url = html.unescape(str(url or "")).replace("\\/", "/").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return urljoin(base_url, url)


def _normalize_media_url(url: str) -> str:
    url = html.unescape(str(url or "")).replace("\\/", "/").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        path = quote(unquote(parts.path), safe="/%:@")
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))
    except Exception:
        return url.replace(" ", "%20")


def _extract_embed_url(embed_url: str) -> str:
    embed_url = html.unescape(str(embed_url or "")).replace("\\/", "/").strip()
    if not embed_url:
        return ""
    direct_url = _extract_direct_url(embed_url)
    if direct_url:
        return direct_url
    if embed_url.startswith(("http://", "https://", "//")):
        return "https:" + embed_url if embed_url.startswith("//") else embed_url
    soup = BeautifulSoup(embed_url, "html.parser")
    iframe = soup.find("iframe")
    if iframe:
        return _make_absolute_url(iframe.get("src") or "", BASE_URL)
    return ""


def _extract_direct_video_urls(page_html: str, base_url: str = "") -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def push(value: str):
        value = html.unescape(str(value or "")).replace("\\/", "/").strip()
        if not value:
            return
        if base_url:
            value = _make_absolute_url(value, base_url)
        value = _normalize_media_url(value)
        if not value.startswith(("http://", "https://")):
            return
        if not _is_direct_video_url(value):
            return
        if value in seen:
            return
        seen.add(value)
        candidates.append(value)

    patterns = [
        r'https?://[^\s"\'<>\\]*googlevideo\.com/videoplayback[^\s"\'<>\\]*',
        r'https?://[^\s"\'<>\\]+\.m3u8(?:\?[^\s"\'<>\\]*)?',
        r'https?://[^\s"\'<>\\]+\.mp4(?:\?[^\s"\'<>\\]*)?',
        r'https?:\\/\\/[^\s"\'<>]*googlevideo\.com\\/videoplayback[^\s"\'<>]*',
        r'https?:\\/\\/[^\s"\'<>]+\.m3u8(?:\?[^\s"\'<>]*)?',
        r'https?:\\/\\/[^\s"\'<>]+\.mp4(?:\?[^\s"\'<>]*)?',
    ]
    for pattern in patterns:
        for match in re.findall(pattern, page_html or "", flags=re.I):
            push(match)

    if "<" in (page_html or ""):
        soup = BeautifulSoup(page_html or "", "html.parser")
        for tag in soup.find_all(["video", "source"]):
            for attr in ("src", "data-src", "data-video-src"):
                push(tag.get(attr) or "")

    for pattern in (
        r'''["'](?:file|src|video|stream|url|hls|playlist|play_url)["']\s*:\s*["']([^"']+)["']''',
        r"""(?:file|src|video|stream|url|hls|playlist|play_url)\s*=\s*["']([^"']+)["']""",
    ):
        for match in re.findall(pattern, page_html or "", flags=re.I):
            push(match)

    return candidates


def _extract_iframe_sources(page_html: str, base_url: str = "") -> list[str]:
    soup = BeautifulSoup(page_html or "", "html.parser")
    results: list[str] = []
    seen: set[str] = set()
    for iframe in soup.find_all("iframe"):
        src = _make_absolute_url(iframe.get("src") or "", base_url or BASE_URL)
        if not src or src in seen:
            continue
        seen.add(src)
        results.append(src)
    return results


async def _resolve_embed_to_direct_urls(url: str, referer: str = "", depth: int = 0, visited: set[str] | None = None) -> list[str]:
    if not url or depth > 2:
        return []
    if visited is None:
        visited = set()

    url = _make_absolute_url(url, referer or BASE_URL)
    if not url or url in visited:
        return []
    visited.add(url)

    if _is_direct_video_url(url):
        return [_normalize_media_url(url)]

    try:
        page_html = await _request_text(url, referer=referer or BASE_URL)
    except Exception as error:
        print(f"[ANIMEPLAY] embed_resolve_error={repr(error)} url={url}")
        return []

    direct_urls = _extract_direct_video_urls(page_html, base_url=url)
    if direct_urls:
        return direct_urls

    for iframe_url in _extract_iframe_sources(page_html, base_url=url):
        resolved = await _resolve_embed_to_direct_urls(
            iframe_url,
            referer=url,
            depth=depth + 1,
            visited=visited,
        )
        if resolved:
            return resolved
    return []


def _quality_from_label_or_url(label: str, url: str) -> str:
    value = f"{label or ''} {url or ''}".upper()
    if "MOBILE" in value or "CELULAR" in value or "480" in value or "360" in value:
        return "SD"
    return "HD"


async def _video_url_looks_playable(url: str, referer: str = "") -> bool:
    url = _normalize_media_url(url)
    if not _is_direct_video_url(url):
        return False

    headers = {
        "User-Agent": _HTTP_HEADERS["User-Agent"],
        "Accept": "*/*",
        "Accept-Language": _HTTP_HEADERS["Accept-Language"],
        "Referer": referer or BASE_URL,
        "Range": "bytes=0-1",
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        for method in ("HEAD", "GET"):
            try:
                async with HTTP_SEMAPHORE:
                    response = await client.request(method, url, headers=headers)
                try:
                    if response.status_code in (200, 206):
                        content_type = (response.headers.get("content-type") or "").lower()
                        if (
                            "video" in content_type
                            or "mpegurl" in content_type
                            or "octet-stream" in content_type
                            or url.lower().split("?", 1)[0].endswith((".mp4", ".m3u8", ".webm"))
                        ):
                            return True
                    if response.status_code in (403, 405) and method == "HEAD":
                        continue
                    if response.status_code >= 500:
                        print(f"[ANIMEPLAY] skipping_dead_video status={response.status_code} url={url}")
                        return False
                finally:
                    await response.aclose()
            except Exception as error:
                print(f"[ANIMEPLAY] video_probe_error={repr(error)} url={url}")
                if method == "HEAD":
                    continue
                return False

    return False


async def _resolve_player_options(post_id: str, episode_url: str, options: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    videos = {}
    trusted_embeds = {}
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

        label = str(option.get("label") or "").upper()
        embed_url = _extract_embed_url(data.get("embed_url") or "")
        if embed_url and _is_trusted_embed_url(embed_url):
            quality = _quality_from_label_or_url(label, embed_url)
            trusted_embeds.setdefault(quality, embed_url)

        direct_urls = []
        if _is_direct_video_url(embed_url):
            direct_urls = [embed_url]
        elif embed_url:
            direct_urls = await _resolve_embed_to_direct_urls(embed_url, referer=episode_url)

        if not direct_urls:
            direct_urls = _extract_direct_video_urls(json.dumps(data, ensure_ascii=False), base_url=episode_url)

        for direct_url in direct_urls:
            direct_url = _normalize_media_url(direct_url)
            if not await _video_url_looks_playable(direct_url, referer=episode_url):
                continue
            quality = _quality_from_label_or_url(label, direct_url)
            videos.setdefault(quality, direct_url)

        if not direct_urls and embed_url and not _is_trusted_embed_url(embed_url):
            print(f"[ANIMEPLAY] ignored_embed_without_direct_video label={label!r} url={embed_url}")

    return videos, trusted_embeds


async def get_episode_player(anime_id: str, episode: str, preferred_quality: str = "HD"):
    anime_id = _normalize_anime_id(anime_id)
    season, episode_number = _parse_episode_ref(episode)
    cache_key = f"{anime_id}|{season}|{episode_number}|{preferred_quality}"
    lock = _PLAYER_LOCKS.setdefault(cache_key, asyncio.Lock())
    async with lock:
        cached = _cache_get(_PLAYER_CACHE, cache_key, _PLAYER_CACHE_TTL)
        if cached is not None:
            return cached

        payload = await get_episodes(anime_id, 0, 3000)
        items = payload.get("all_items") or []
        by_episode = payload.get("by_episode") or {}
        index = _find_episode_index(items, by_episode, season, episode_number)
        if index is None:
            _DETAILS_CACHE.pop(anime_id, None)
            _EPISODES_CACHE.pop(anime_id, None)
            payload = await get_episodes(anime_id, 0, 3000)
            items = payload.get("all_items") or []
            by_episode = payload.get("by_episode") or {}
            index = _find_episode_index(items, by_episode, season, episode_number)
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
        videos, trusted_embeds = await _resolve_player_options(post_id, episode_url, options)
        using_embed_fallback = False
        if not videos and trusted_embeds:
            videos = trusted_embeds
            using_embed_fallback = True
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
            "player_type": "iframe" if using_embed_fallback else "video",
            "base_slug": anime_id,
            "server": "BLOGGER" if using_embed_fallback else "ANIMEPLAY",
            "quality": selected_quality,
            "available_qualities": list(videos.keys()),
            "prev_episode": prev_episode,
            "next_episode": next_episode,
            "total_episodes": len(items),
            "season": season,
            "episode_number": episode_number,
            "episode_title": item.get("title") or "",
            "thumb": item.get("thumb") or item.get("image") or "",
            "image": item.get("image") or item.get("thumb") or "",
        }
        _cache_set(_PLAYER_CACHE, cache_key, data)
        return data


def invalidate_episode_caches(anime_id: str, episode: str) -> None:
    anime_id = _normalize_anime_id(anime_id)
    _DETAILS_CACHE.pop(anime_id, None)
    _EPISODES_CACHE.pop(anime_id, None)
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
