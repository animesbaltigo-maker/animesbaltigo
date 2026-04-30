import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN", "8675150552:AAHoUu64RoMPHNdaChP9RQGF0iz-tk7Crbo").strip()
API_ID = int(os.getenv("API_ID", "39909232") or "0")
API_HASH = os.getenv("API_HASH", "af7a08316fb157de8396ce7d38bae2d5").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "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").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "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").strip()

SOURCE_SITE_BASE = os.getenv("SOURCE_SITE_BASE", "https://animefire.io").strip().rstrip("/")

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
VIDEO_DOWNLOAD_QUEUE_LIMIT = int(os.getenv("VIDEO_DOWNLOAD_QUEUE_LIMIT", "20"))
VIDEO_DOWNLOAD_WORKERS = int(os.getenv("VIDEO_DOWNLOAD_WORKERS", "2"))
VIDEO_DOWNLOAD_CACHE_DIR = os.getenv(
    "VIDEO_DOWNLOAD_CACHE_DIR",
    str(DATA_DIR / "video_cache"),
).strip()
VIDEO_DOWNLOAD_MAX_MB = int(os.getenv("VIDEO_DOWNLOAD_MAX_MB", "1900"))
VIDEO_CACHE_TTL_HOURS = int(os.getenv("VIDEO_CACHE_TTL_HOURS", "1"))
VIDEO_CACHE_CLEANUP_INTERVAL_SECONDS = int(os.getenv("VIDEO_CACHE_CLEANUP_INTERVAL_SECONDS", "600"))
VIDEO_UPLOAD_MAX_MB = int(os.getenv("VIDEO_UPLOAD_MAX_MB", "49"))
TELETHON_UPLOAD_MAX_MB = int(os.getenv("TELETHON_UPLOAD_MAX_MB", "1900"))
TELETHON_PARALLEL_UPLOAD = os.getenv("TELETHON_PARALLEL_UPLOAD", "1").strip() == "1"
TELETHON_PARALLEL_UPLOAD_THRESHOLD_MB = int(os.getenv("TELETHON_PARALLEL_UPLOAD_THRESHOLD_MB", "20"))
TELETHON_PARALLEL_UPLOAD_WORKERS = int(os.getenv("TELETHON_PARALLEL_UPLOAD_WORKERS", "8"))
TELETHON_SESSION_NAME = os.getenv("TELETHON_SESSION_NAME", str(DATA_DIR / "anime_uploader_bot")).strip()
VIDEO_DOWNLOAD_PROTECT_CONTENT = os.getenv("VIDEO_DOWNLOAD_PROTECT_CONTENT", "1").strip() == "1"

OT_BRAND = os.getenv("BOT_BRAND", "Anime Brasil").strip()
WEBAPP_BASE_URL = os.getenv("WEBAPP_BASE_URL", "").strip().rstrip("/")
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
CAKTO_WEBHOOK_SECRET = os.getenv("CAKTO_WEBHOOK_SECRET", "0ce52ce5-aa98-4aff-be81-23ccbb85f741").strip()
CAKTO_CLIENT_ID = os.getenv("CAKTO_CLIENT_ID", "2j5rsXTfq6C7OW3cJJGl6CAUkQBotG5n2u1q4JnL").strip()
CAKTO_CLIENT_SECRET = os.getenv("CAKTO_CLIENT_SECRET", "Cxokh3ErCGWEbLzxsDDUZ2wvkBUB5skAVirWWaPofKr9JFx5ctbKbPv2axSEeUtvwMMmF1aaiETPTLjcf6wSp4CyTpZ7A7KJQg1A0lMoBw01jwkIqTbUEOn1lDBLXs5Z").strip()
CAKTO_API_BASE_URL = os.getenv("CAKTO_API_BASE_URL", "https://api.cakto.com.br").strip().rstrip("/")
CAKTO_ORDER_SYNC_LIMIT = int(os.getenv("CAKTO_ORDER_SYNC_LIMIT", "100") or "100")
