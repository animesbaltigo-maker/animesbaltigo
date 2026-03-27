from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ContextTypes

MINIAPP_URL = "https://jerusalem-editorials-screensavers-for.trycloudflare.com/miniapp/index.html"


# 🔍 SIMULA BUSCA (depois você troca pela sua API real)
async def search_anime(query):
    return [
        {"id": "naruto", "title": "Naruto"},
        {"id": "one-piece", "title": "One Piece"},
    ]


# 🎬 SIMULA EPISÓDIOS
async def get_episodes(anime_id):
    return list(range(1, 21))  # EP 1 até 20


# =========================
# /epanime Naruto
# =========================
async def epanime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usa assim:\n/epanime Naruto")
        return

    query = " ".join(context.args)

    results = await search_anime(query)

    keyboard = []
    for item in results:
        keyboard.append([
            InlineKeyboardButton(
                item["title"],
                callback_data=f"anime:{item['id']}"
            )
        ])

    await update.message.reply_text(
        f"Resultados para: {query}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================
# CLICK NO ANIME
# =========================
async def select_anime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    anime_id = query.data.split(":")[1]

    episodes = await get_episodes(anime_id)

    keyboard = []
    for ep in episodes[:10]:
        keyboard.append([
            InlineKeyboardButton(
                f"▶ EP {ep}",
                web_app=WebAppInfo(
                    url=f"{MINIAPP_URL}?anime_id={anime_id}&episode={ep}"
                )
            )
        ])

    await query.edit_message_text(
        f"Escolha o episódio:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
