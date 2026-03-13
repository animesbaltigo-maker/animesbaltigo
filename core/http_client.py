import httpx

_client: httpx.AsyncClient | None = None


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}


async def _get_client() -> httpx.AsyncClient:
    global _client

    if _client is None:
        _client = httpx.AsyncClient(
            headers=_HEADERS,
            follow_redirects=True,
            timeout=30,
        )

    return _client


async def http_get(url: str, params: dict | None = None) -> str:
    client = await _get_client()

    r = await client.get(url, params=params)

    r.raise_for_status()

    return r.text


async def close_http_client():
    global _client

    if _client:
        await _client.aclose()
        _client = None
