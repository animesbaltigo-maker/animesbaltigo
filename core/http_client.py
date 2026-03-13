import httpx
from config import HTTP_TIMEOUT

_client = None


async def get_http_client():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=20, read=HTTP_TIMEOUT, write=HTTP_TIMEOUT, pool=20),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _client


async def close_http_client():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
