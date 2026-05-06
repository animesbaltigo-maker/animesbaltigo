from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import time

from config import REQUIRED_CHANNEL, REQUIRED_CHANNEL_URL

_ACCESS_NOTICE_TTL = 120


async def ensure_channel_membership(update, context: ContextTypes.DEFAULT_TYPE):

    if not REQUIRED_CHANNEL:
        return True

    if not update.effective_user or not update.effective_message:
        return False

    user_id = update.effective_user.id

    try:
        member = await context.bot.get_chat_member(REQUIRED_CHANNEL, user_id)

        if member.status in ("member", "administrator", "creator"):
            return True

    except Exception:
        pass

    user_key = f"access_notice:{user_id}"
    now = time.monotonic()
    last_notice = context.user_data.get(user_key, 0.0)
    if now - last_notice < _ACCESS_NOTICE_TTL:
        return False
    context.user_data[user_key] = now

    text = (
        "🔒 <b>Acesso restrito</b>\n\n"
        "Para usar o bot você precisa entrar no canal primeiro."
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Entrar no canal", url=REQUIRED_CHANNEL_URL)]
    ])

    await update.effective_message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=keyboard
    )

    return False
