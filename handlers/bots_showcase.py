from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes

from config import WEBAPP_BASE_URL


DEFAULT_WEBAPP_BASE_URL = "https://rough-double-remarkable-north.trycloudflare.com"


def bots_webapp_url() -> str:
    base = (WEBAPP_BASE_URL or DEFAULT_WEBAPP_BASE_URL).strip().rstrip("/")
    if base.endswith("/app"):
        base = base[:-4]
    return f"{base}/miniapp/bots/index.html"


def bots_showcase_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🧭 Abrir vitrine Baltigo",
                    web_app=WebAppInfo(url=bots_webapp_url()),
                )
            ]
        ]
    )


async def bots_showcase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message:
        return

    first_name = user.first_name if user and user.first_name else "otaku"
    text = (
        f"🧭 <b>Vitrine Baltigo</b>\n\n"
        f"{first_name}, veja os bots da rede Baltigo em um só lugar."
    )

    await message.reply_text(
        text=text,
        parse_mode="HTML",
        reply_markup=bots_showcase_keyboard(),
        disable_web_page_preview=True,
    )
