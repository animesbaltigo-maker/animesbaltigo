BOT_USERNAME = "AnimesBaltigo_Bot"  # ajusta se precisar
TRIGGER = "akira"

async def group_ai_handler(update, context):
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

    # Verifica se começa com "akira"
    if text_lower.startswith(TRIGGER):
        user_text = text[len(TRIGGER):].strip()
    elif replying_to_bot:
        user_text = text
    else:
        return

    if not user_text:
        await message.reply_text("Fala comigo assim: akira me recomenda um anime 🔥")
        return

    # resposta
    try:
        reply = generate_anime_reply(user_text)
        await message.reply_text(reply)
    except Exception as e:
        print("Erro IA:", e)
        await message.reply_text("Tive um bug aqui 😵 tenta de novo")
