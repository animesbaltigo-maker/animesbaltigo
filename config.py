import os
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_csv(name: str) -> list[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _clean_public_url(value: str) -> str:
    return (value or "").strip().rstrip("/")


def _join_public_url(base: str, path: str) -> str:
    base = _clean_public_url(base)
    path = (path or "").strip()

    if not path:
        return base

    if path.startswith(("http://", "https://")):
        return path.rstrip("/")

    if not base:
        return path

    return f"{base}/{path.lstrip('/')}"


def _origin_from_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, "", "", "")).rstrip("/")


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

SOURCE_SITE_BASE = _clean_public_url(os.getenv("SOURCE_SITE_BASE", "https://animefire.io"))

REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@Centraldeanimes_Baltigo").strip()
REQUIRED_CHANNEL_URL = os.getenv("REQUIRED_CHANNEL_URL", "https://t.me/Centraldeanimes_Baltigo").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "AnimesBaltigo_Bot").strip()
CANAL_POSTAGEM = os.getenv("CANAL_POSTAGEM", "@Centraldeanimes_Baltigo").strip()
DOWNLOAD_ARCHIVE_CHANNEL = os.getenv("DOWNLOAD_ARCHIVE_CHANNEL", "-1003776313014").strip()
OFFLINE_REFERRAL_REQUIRED_CLICKS = _env_int("OFFLINE_REFERRAL_REQUIRED_CLICKS", 3)
OFFLINE_DOWNLOAD_MAX_BYTES = _env_int("OFFLINE_DOWNLOAD_MAX_MB", 2048) * 1024 * 1024
STICKER_DIVISOR = os.getenv(
    "STICKER_DIVISOR",
    "CAACAgQAAx0CbKkU-AACFJtps_kRLpeUt2Gvd7mT4d0gS1vyCgACOhUAAqDAiFJSU5pkUMltvzoE",
).strip()

ADMIN_IDS = [int(x) for x in _env_csv("ADMIN_IDS") if x.isdigit()] or [1852596083]

SEARCH_LIMIT = _env_int("SEARCH_LIMIT", 10)
EPISODES_PER_PAGE = _env_int("EPISODES_PER_PAGE", 12)
EPISODE_LOOKUP_LIMIT = _env_int("EPISODE_LOOKUP_LIMIT", 400)
ANTI_FLOOD_SECONDS = _env_float("ANTI_FLOOD_SECONDS", 1.2)
API_CACHE_TTL_SECONDS = _env_int("API_CACHE_TTL_SECONDS", 900)
HTTP_TIMEOUT = _env_int("HTTP_TIMEOUT", 45)
NEW_EPISODES_POLL_SECONDS = _env_int("NEW_EPISODES_POLL_SECONDS", 120)
REFERRAL_CHECK_SECONDS = _env_int("REFERRAL_CHECK_SECONDS", 3600)
CHANNEL_MEMBERSHIP_CACHE_TTL = _env_int("CHANNEL_MEMBERSHIP_CACHE_TTL", 600)
GROUP_AI_HTTP_TIMEOUT = _env_int("GROUP_AI_HTTP_TIMEOUT", HTTP_TIMEOUT)

BOT_BRAND = os.getenv("BOT_BRAND", "BALTIGO").strip() or "BALTIGO"
WEBAPP_BASE_URL = _clean_public_url(
    os.getenv("WEBAPP_BASE_URL", "https://rough-double-remarkable-north.trycloudflare.com")
)
WEBAPP_APP_PATH = os.getenv("WEBAPP_APP_PATH", "/app").strip() or "/app"
PEDIDO_WEBAPP_URL = _clean_public_url(os.getenv("PEDIDO_WEBAPP_URL", ""))
BALTIGOFLIX_WEBAPP_URL = _clean_public_url(os.getenv("BALTIGOFLIX_WEBAPP_URL", ""))
WEBAPP_ADMIN_TOKEN = os.getenv("WEBAPP_ADMIN_TOKEN", "").strip()
BOT_PRIVATE_URL = _clean_public_url(
    os.getenv(
        "BOT_PRIVATE_URL",
        f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "",
    )
)

_allowed_origins = _env_csv("WEBAPP_ALLOWED_ORIGINS")
if not _allowed_origins:
    default_origin = _origin_from_url(WEBAPP_BASE_URL)
    if default_origin:
        _allowed_origins = [default_origin]
WEBAPP_ALLOWED_ORIGINS = _allowed_origins


def build_webapp_url(path: str = "", params: dict[str, str | int | float | None] | None = None) -> str:
    target = _join_public_url(WEBAPP_BASE_URL, path or WEBAPP_APP_PATH)
    if not params:
        return target

    filtered = {
        str(key): str(value)
        for key, value in params.items()
        if value not in (None, "")
    }
    if not filtered:
        return target

    separator = "&" if "?" in target else "?"
    return f"{target}{separator}{urlencode(filtered)}"


def build_default_webapp_url(params: dict[str, str | int | float | None] | None = None) -> str:
    return build_webapp_url(WEBAPP_APP_PATH, params=params)


def resolve_pedido_webapp_url() -> str:
    return PEDIDO_WEBAPP_URL or build_default_webapp_url({"view": "pedido"})


def resolve_baltigoflix_webapp_url() -> str:
    return BALTIGOFLIX_WEBAPP_URL or build_default_webapp_url({"view": "baltigoflix"})
