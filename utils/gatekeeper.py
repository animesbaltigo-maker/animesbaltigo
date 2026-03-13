from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import REQUIRED_CHANNEL, REQUIRED_CHANNEL_URL


async def ensure_channel_membership(update, context: ContextTypes.DEFAULT_TYPE):

    if not REQUIRED_CHANNEL:
        return True

    user_id = update.effective_user.id

    try:
        member = await context.bot.get_chat_member(REQUIRED_CHANNEL, user_id)

        if member.status in ("member", "administrator", "creator"):
            return True

    except Exception:
        pass

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