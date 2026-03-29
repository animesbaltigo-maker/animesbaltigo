from telegram import Update
from telegram.ext import ContextTypes

from config import BOT_USERNAME
from services.gemini_ai import generate_anime_reply
from utils.gatekeeper import ensure_channel_membership

TRIGGER = "akira"


async def group_ai_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if not message or not message.text:
        return

    text = message.text.strip()
    text_lower = text.lower()

    # Verifica se é reply ao bot
    replying_to_bot = bool(
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.username
        and BOT_USERNAME
        and message.reply_to_message.from_user.username.lower() == BOT_USERNAME.lower()
    )

    # Privado: responde qualquer mensagem
    if update.effective_chat and update.effective_chat.type == "private":
        user_text = text

    # Grupo: precisa começar com o trigger
    elif text_lower.startswith(TRIGGER):
        user_text = text[len(TRIGGER):].strip()

    # Grupo: ou ser reply ao bot
    elif replying_to_bot:
        user_text = text

    else:
        return

    if not user_text:
        await message.reply_text("Fala comigo assim: akira me recomenda um anime 🔥")
        return

    # Bloqueia uso da IA para quem não estiver no canal obrigatório
    if not await ensure_channel_membership(update, context):
        return

    try:
        reply = generate_anime_reply(user_text)

        if not reply or reply.strip() == "[NO_REPLY]":
            return

        await message.reply_text(reply)

    except Exception as e:
        err = str(e)

        if "RESOURCE_EXHAUSTED" in err or "429" in err or "quota" in err.lower():
            await message.reply_text(
                "Tch… gastei todo meu chakra respondendo vocês 😵‍💫\n"
                "Me dá um tempinho e tenta de novo, ok?"
            )
            return

        print("Erro IA:", e)
        await message.reply_text("Tive um bug aqui 😵 tenta de novo")
