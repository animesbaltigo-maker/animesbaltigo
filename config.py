import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "8675150552:AAHoUu64RoMPHNdaChP9RQGF0iz-tk7Crbo").strip()
SOURCE_SITE_BASE = os.getenv("SOURCE_SITE_BASE", "https://animefire.io").strip().rstrip("/")

REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@Centraldeanimes_Baltigo").strip()
REQUIRED_CHANNEL_URL = os.getenv("REQUIRED_CHANNEL_URL", "t.me/Centraldeanimes_Baltigo").strip()
BOT_USERNAME = "AnimesBaltigo_Bot"
CANAL_POSTAGEM = "@Centraldeanimes_Baltigo"
STICKER_DIVISOR = "CAACAgQAAx0CbKkU-AACFJtps_kRLpeUt2Gvd7mT4d0gS1vyCgACOhUAAqDAiFJSU5pkUMltvzoE"
ADMIN_IDS = [1852596083, 987654321]


SEARCH_LIMIT = int(os.getenv("SEARCH_LIMIT", "10"))
EPISODES_PER_PAGE = int(os.getenv("EPISODES_PER_PAGE", "12"))
EPISODE_LOOKUP_LIMIT = int(os.getenv("EPISODE_LOOKUP_LIMIT", "400"))
ANTI_FLOOD_SECONDS = float(os.getenv("ANTI_FLOOD_SECONDS", "1.2"))
API_CACHE_TTL_SECONDS = int(os.getenv("API_CACHE_TTL_SECONDS", "900"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "45"))

BOT_BRAND = os.getenv("BOT_BRAND", "Anime Brasil").strip()
WEBAPP_BASE_URL = os.getenv("WEBAPP_BASE_URL", "").strip().rstrip("/")
