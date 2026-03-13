from config import BOT_BRAND
from utils.gatekeeper import ensure_channel_membership

BANNER = "https://photo.chelpbot.me/AgACAgEAAxkBZ75s9mmy_mKmcmT2MqR4wfh7LgM92iV3AAKeC2sbGmKZRU2Q2nq2G9RgAQADAgADeQADOgQ/photo.jpg"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    # Deep link para abrir capítulo 
    if context.args:

        payload = context.args[0]

        if payload.startswith("cap_"):

            chapter_id = payload.replace("cap_", "")

            await open_chapter(update, context, chapter_id)

            return

    text = (
        "🎬 <b>Bem-vindo ao Animes Baltigo!</b>\n\n"
        "Aqui você pode assistir gratuitamente, direto no Telegram.\n\n"
        "✨ <b>O que você pode fazer aqui:</b>\n"
        "• 🔎 Buscar qualquer anime\n"
        "• 🎞 Navegar pelob acervo facilmente\n"
        "• ⚡ Rápido e sem anúncios\n\n"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ Adicionar ao grupo",
                url=f"https://t.me/{BOT_USERNAME}?startgroup=true"
            )
        ],
        [
            InlineKeyboardButton(
                "🏴‍☠️ QG Baltigo",
                url="https://t.me/QG_BALTIGO"
            )
        ]
    ])

    await update.effective_message.reply_photo(
        photo=BANNER,
        caption=text,
        parse_mode="HTML",
        reply_markup=keyboard
    )
