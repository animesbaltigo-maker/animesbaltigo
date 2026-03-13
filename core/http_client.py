import httpx


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}


async def http_get(url: str, params: dict | None = None) -> str:
    async with httpx.AsyncClient(
        headers=_HEADERS,
        follow_redirects=True,
        timeout=30,
    ) as client:

        r = await client.get(url, params=params)

        r.raise_for_status()

        return r.text
