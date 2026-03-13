import httpx

_client: httpx.AsyncClient | None = None

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "Referer": "https://animefire.io/",
    "Origin": "https://animefire.io",
}


async def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            headers=_HEADERS,
            follow_redirects=True,
            timeout=30,
            http2=True,
        )
    return _client


async def http_get(url: str, params: dict | None = None) -> str:
    client = await get_http_client()
    r = await client.get(url, params=params)
    r.raise_for_status()
    return r.text


async def close_http_client():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
