from config import BOT_BRAND
from utils.gatekeeper import ensure_channel_membership


async def ajuda(update, context):
    if not await ensure_channel_membership(update, context):
        return
    text = (
        f"🆘 <b>Ajuda — {BOT_BRAND}</b>\n\n"
        "• <code>/buscar nome</code> → procura um anime\n"
        "• Escolha o anime\n"
        "• Escolha um episódio\n\n"
        "Na tela do episódio:\n"
        "• ▶️ Abrir episódio no site\n"
        "• 📺 Mini App dentro do Telegram\n"
        "• ⬅️ episódio anterior\n"
        "• ➡️ próximo episódio\n"
        "• 📋 lista de episódios"
    )
    await update.message.reply_text(text, parse_mode="HTML")
