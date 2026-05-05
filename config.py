import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID = int(os.getenv("API_ID", "0") or "0")
API_HASH = os.getenv("API_HASH", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "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").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "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").strip()

SOURCE_SITE_BASE = os.getenv("SOURCE_SITE_BASE", "https://sushianimes.com.br").strip().rstrip("/")
ANIME_SOURCE = os.getenv("ANIME_SOURCE", "").strip().lower()
if not ANIME_SOURCE:
    ANIME_SOURCE = "sushi" if "sushianimes" in SOURCE_SITE_BASE.lower() else "animefire"

REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@Centraldeanimes_Baltigo").strip()
REQUIRED_CHANNEL_URL = os.getenv("REQUIRED_CHANNEL_URL", "t.me/Centraldeanimes_Baltigo").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "AnimesBaltigo_Bot").strip()
CANAL_POSTAGEM = os.getenv("CANAL_POSTAGEM", "@Centraldeanimes_Baltigo").strip()
STICKER_DIVISOR = os.getenv(
    "STICKER_DIVISOR",
    "CAACAgQAAx0CbKkU-AACFJtps_kRLpeUt2Gvd7mT4d0gS1vyCgACOhUAAqDAiFJSU5pkUMltvzoE",
).strip()

ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "1852596083,987654321").split(",")
    if x.strip().isdigit()
]

SEARCH_LIMIT = int(os.getenv("SEARCH_LIMIT", "10"))
EPISODES_PER_PAGE = int(os.getenv("EPISODES_PER_PAGE", "12"))
EPISODE_LOOKUP_LIMIT = int(os.getenv("EPISODE_LOOKUP_LIMIT", "400"))
ANTI_FLOOD_SECONDS = float(os.getenv("ANTI_FLOOD_SECONDS", "1.2"))
API_CACHE_TTL_SECONDS = int(os.getenv("API_CACHE_TTL_SECONDS", "900"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "45"))
UPSTREAM_PROXY_URL = (
    os.getenv("UPSTREAM_PROXY_URL", "").strip()
    or os.getenv("SCRAPER_PROXY_URL", "").strip()
    or os.getenv("HTTPS_PROXY", "").strip()
    or os.getenv("HTTP_PROXY", "").strip()
    or os.getenv("ALL_PROXY", "").strip()
)
VIDEO_DOWNLOAD_QUEUE_LIMIT = int(os.getenv("VIDEO_DOWNLOAD_QUEUE_LIMIT", "20"))
VIDEO_DOWNLOAD_WORKERS = int(os.getenv("VIDEO_DOWNLOAD_WORKERS", "2"))
VIDEO_DOWNLOAD_CACHE_DIR = os.getenv(
    "VIDEO_DOWNLOAD_CACHE_DIR",
    str(DATA_DIR / "video_cache"),
).strip()
VIDEO_DOWNLOAD_MAX_MB = int(os.getenv("VIDEO_DOWNLOAD_MAX_MB", "1900"))
VIDEO_DOWNLOAD_TRUST_ENV = os.getenv("VIDEO_DOWNLOAD_TRUST_ENV", "0").strip().lower() in {"1", "true", "yes", "on", "sim"}
VIDEO_DOWNLOAD_CHUNK_MB = int(os.getenv("VIDEO_DOWNLOAD_CHUNK_MB", "4"))
VIDEO_DOWNLOAD_PART_MB = int(os.getenv("VIDEO_DOWNLOAD_PART_MB", "8"))
VIDEO_DOWNLOAD_PARALLEL = os.getenv("VIDEO_DOWNLOAD_PARALLEL", "0").strip().lower() in {"1", "true", "yes", "on", "sim"}
VIDEO_DOWNLOAD_PARALLEL_WORKERS = int(os.getenv("VIDEO_DOWNLOAD_PARALLEL_WORKERS", "4"))
VIDEO_CACHE_TTL_HOURS = int(os.getenv("VIDEO_CACHE_TTL_HOURS", "1"))
VIDEO_CACHE_CLEANUP_INTERVAL_SECONDS = int(os.getenv("VIDEO_CACHE_CLEANUP_INTERVAL_SECONDS", "600"))
VIDEO_UPLOAD_MAX_MB = int(os.getenv("VIDEO_UPLOAD_MAX_MB", "49"))
TELETHON_UPLOAD_MAX_MB = int(os.getenv("TELETHON_UPLOAD_MAX_MB", "1900"))
TELETHON_PARALLEL_UPLOAD = os.getenv("TELETHON_PARALLEL_UPLOAD", "1").strip() == "1"
TELETHON_PARALLEL_UPLOAD_THRESHOLD_MB = int(os.getenv("TELETHON_PARALLEL_UPLOAD_THRESHOLD_MB", "20"))
TELETHON_PARALLEL_UPLOAD_WORKERS = int(os.getenv("TELETHON_PARALLEL_UPLOAD_WORKERS", "8"))
TELETHON_SESSION_NAME = os.getenv("TELETHON_SESSION_NAME", str(DATA_DIR / "anime_uploader_bot")).strip()
VIDEO_DOWNLOAD_PROTECT_CONTENT = os.getenv("VIDEO_DOWNLOAD_PROTECT_CONTENT", "1").strip() == "1"

BOT_BRAND = os.getenv("BOT_BRAND", os.getenv("OT_BRAND", "Anime Brasil")).strip()
WEBAPP_BASE_URL = os.getenv("WEBAPP_BASE_URL", "").strip().rstrip("/")
_SUBSCRIPTIONS_DB_RAW = Path(
    os.getenv("SUBSCRIPTIONS_DB_PATH", "").strip()
    or os.getenv("BALTIGO_SUBSCRIPTIONS_DB_PATH", "").strip()
    or str(DATA_DIR / "offline_subscriptions.sqlite3")
)
SUBSCRIPTIONS_DB_PATH = str(
    _SUBSCRIPTIONS_DB_RAW if _SUBSCRIPTIONS_DB_RAW.is_absolute() else BASE_DIR / _SUBSCRIPTIONS_DB_RAW
)
BALTIGOFLIX_SUBSCRIBE_URL = os.getenv("BALTIGOFLIX_SUBSCRIBE_URL", "http://baltigoflix.com.br/").strip()
BALTIGOFLIX_SUPPORT_URL = os.getenv("BALTIGOFLIX_SUPPORT_URL", "https://t.me/SourceBaltigo_Bot").strip()
CAKTO_CHECKOUT_URL = os.getenv("CAKTO_CHECKOUT_URL", "").strip()
CAKTO_MENSAL_CHECKOUT_URL = os.getenv("CAKTO_MENSAL_CHECKOUT_URL", "https://pay.cakto.com.br/9snqsP3").strip()
CAKTO_TRIMESTRAL_CHECKOUT_URL = os.getenv("CAKTO_TRIMESTRAL_CHECKOUT_URL", "https://pay.cakto.com.br/3fsy24d").strip()
CAKTO_SEMESTRAL_CHECKOUT_URL = os.getenv("CAKTO_SEMESTRAL_CHECKOUT_URL", "https://pay.cakto.com.br/32ocvxm").strip()
CAKTO_ANUAL_CHECKOUT_URL = os.getenv("CAKTO_ANUAL_CHECKOUT_URL", "https://pay.cakto.com.br/u9wz86m").strip()
CAKTO_BRONZE_CHECKOUT_URL = os.getenv("CAKTO_BRONZE_CHECKOUT_URL", CAKTO_MENSAL_CHECKOUT_URL).strip()
CAKTO_OURO_CHECKOUT_URL = os.getenv("CAKTO_OURO_CHECKOUT_URL", CAKTO_TRIMESTRAL_CHECKOUT_URL).strip()
CAKTO_DIAMANTE_CHECKOUT_URL = os.getenv("CAKTO_DIAMANTE_CHECKOUT_URL", CAKTO_SEMESTRAL_CHECKOUT_URL).strip()
CAKTO_RUBI_CHECKOUT_URL = os.getenv("CAKTO_RUBI_CHECKOUT_URL", CAKTO_ANUAL_CHECKOUT_URL).strip()
CAKTO_WEBHOOK_SECRET = os.getenv("CAKTO_WEBHOOK_SECRET", "").strip()
CAKTO_CLIENT_ID = os.getenv("CAKTO_CLIENT_ID", "").strip()
CAKTO_CLIENT_SECRET = os.getenv("CAKTO_CLIENT_SECRET", "").strip()
CAKTO_API_BASE_URL = os.getenv("CAKTO_API_BASE_URL", "https://api.cakto.com.br").strip().rstrip("/")
CAKTO_ORDER_SYNC_LIMIT = int(os.getenv("CAKTO_ORDER_SYNC_LIMIT", "100") or "100")
