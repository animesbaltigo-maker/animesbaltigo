from config import BOT_BRAND
from utils.gatekeeper import ensure_channel_membership


async def start(update, context):
    if not await ensure_channel_membership(update, context):
        return
    await update.message.reply_text(
        f"🎬 <b>{BOT_BRAND}</b>\n\n"
        "Use <code>/buscar nome_do_anime</code>\n\n"
        "Exemplos:\n• <code>/buscar one piece</code>\n• <code>/buscar naruto</code>\n• <code>/buscar solo leveling</code>\n\n"
        "Use <code>/ajuda</code> para ver tudo.",
        parse_mode="HTML",
    )
