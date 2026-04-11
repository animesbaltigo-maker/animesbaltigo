import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

BOT_TOKEN = os.getenv("BOT_TOKEN", "8675150552:AAHoUu64RoMPHNdaChP9RQGF0iz-tk7Crbo").strip()
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

BOT_BRAND = os.getenv("BOT_BRAND", "Anime Brasil").strip()
WEBAPP_BASE_URL = os.getenv("WEBAPP_BASE_URL", "").strip().rstrip("/")
