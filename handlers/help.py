from config import BOT_BRAND
from utils.gatekeeper import ensure_channel_membership


async def ajuda(update, context):
    if not await ensure_channel_membership(update, context):
        return
    text = (
    "🆘 <b>Ajuda — Central Animes</b>\n\n"
    "━━━━━━━━━━━━━━\n\n"
    "🔎 <b>Buscar um anime</b>\n"
    "Use o comando:\n"
    "<code>/buscar nome</code>\n\n"
    "📌 <b>Exemplo</b>\n"
    "<code>/buscar attack on titan</code>\n\n"
    "🎬 <b>Como funciona</b>\n"
    "• Pesquise o anime pelo nome\n"
    "• Escolha o resultado desejado\n"
    "• Abra a lista de episódios\n"
    "• Selecione o episódio para assistir\n\n"
    "📺 <b>Na tela do episódio</b>\n"
    "• ▶️ Assistir no site\n"
    "• ⏮ Episódio anterior\n"
    "• ⏭ Próximo episódio\n"
    "• 📋 Lista de episódios\n\n"
    "⚠️ <b>Atenção</b>\n"
    "Alguns animes podem ter nomes alternativos, então vale testar outras formas de pesquisa."
    )
    await update.message.reply_text(text, parse_mode="HTML")
