import html
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from services.animefire_client import search_anime

RESULTS_PER_PAGE = 8


def _build_search_text(query: str, page: int, total: int) -> str:
    total_pages = max(1, ((total - 1) // RESULTS_PER_PAGE) + 1)
    safe_query = html.escape((query or "").strip())

    return (
        f"🔎 <b>Busca de animes</b>\n"
        f"━━━━━━━━━━━━━━\n\n"
        f"🎬 <b>Pesquisa:</b> {safe_query}\n"
        f"📄 <b>Página:</b> {page}/{total_pages}\n"
        f"📚 <b>Resultados:</b> {total}\n\n"
        f"Toque em um anime para abrir."
    )


def _store_search_session(context: ContextTypes.DEFAULT_TYPE, query: str, results: list) -> str:
    token = secrets.token_hex(4)
    context.user_data[f"search_session:{token}"] = {
        "query": query,
        "results": results,
    }
    return token


def _build_results_keyboard(results: list, page: int, total: int, token: str) -> InlineKeyboardMarkup:
    rows = []

    start = (page - 1) * RESULTS_PER_PAGE
    end = start + RESULTS_PER_PAGE
    page_items = results[start:end]

    for idx, item in enumerate(page_items, start=start + 1):
        title = (item.get("title") or "Sem título").strip()

        if len(title) > 42:
            title = title[:39].rstrip() + "..."

        rows.append([
            InlineKeyboardButton(
                f"{idx}. {title}",
                callback_data=f"sa|{token}|{idx - 1}",
            )
        ])

    nav = []
    if page > 1:
        nav.append(
            InlineKeyboardButton(
                "⬅️ Anterior",
                callback_data=f"sp|{token}|{page - 1}",
            )
        )

    if end < total:
        nav.append(
            InlineKeyboardButton(
                "Próxima ➡️",
                callback_data=f"sp|{token}|{page + 1}",
            )
        )

    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(rows)


async def buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text(
            "🔎 <b>Como usar</b>\n\n"
            "Envie o comando assim:\n"
            "<code>/buscar nome do anime</code>",
            parse_mode="HTML",
        )
        return

    query = " ".join(context.args).strip()

    msg = await update.effective_message.reply_text(
        "🔎 <b>Buscando animes...</b>",
        parse_mode="HTML",
    )

    try:
        results = await search_anime(query)

        if not results:
            await msg.edit_text(
                "❌ <b>Nenhum anime encontrado.</b>\n\n"
                "Tente pesquisar com outro nome.",
                parse_mode="HTML",
            )
            return

        token = _store_search_session(context, query, results)

        page = 1
        total = len(results)

        text = _build_search_text(query, page, total)
        keyboard = _build_results_keyboard(results, page, total, token)

        await msg.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    except Exception as e:
        print("ERRO NA BUSCA:", repr(e))
        await msg.edit_text(
            "❌ <b>Erro ao buscar os animes.</b>\n\n"
            "Tente novamente em instantes.",
            parse_mode="HTML",
        )