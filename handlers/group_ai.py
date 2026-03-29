from telegram import Update
from telegram.ext import ContextTypes

from config import BOT_USERNAME
from services.gemini_ai import generate_anime_reply
from services.anime_filter import is_anime_related
from services.ai_group_state import can_reply, mark_reply, should_random_reply


async def group_ai_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    if chat.type not in ("group", "supergroup"):
        return

    text = (message.text or "").strip()
    if not text:
        return

    if not is_anime_related(text):
        return

    mentioned = False
    if BOT_USERNAME:
        mentioned = f"@{BOT_USERNAME}".lower() in text.lower()

    replying_to_bot = False
    if message.reply_to_message and message.reply_to_message.from_user:
        replying_to_bot = bool(message.reply_to_message.from_user.is_bot)

    auto_mode = False

    if not mentioned and not replying_to_bot:
        if not can_reply(chat.id):
            return
        if not should_random_reply():
            return
        auto_mode = True

    try:
        answer = generate_anime_reply(text)
    except Exception as e:
        print("[GROUP_AI_ERROR]", repr(e))
        return

    if not answer or answer == "[NO_REPLY]":
        return

    answer = answer[:500]
    await message.reply_text(answer)

    if auto_mode:
        mark_reply(chat.id)
