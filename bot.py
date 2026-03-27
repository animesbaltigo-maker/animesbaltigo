import asyncio

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import BOT_TOKEN
from core.http_client import close_http_client
from handlers.start import start
from handlers.search import buscar
from handlers.help import ajuda
from handlers.callbacks import callbacks
from handlers.infoanime import infoanime, callback_info_anime
from handlers.postanime import postanime
from handlers.novoseps import postnovoseps, auto_post_new_eps_job
from handlers.postfilmes import postfilmes
from handlers.recommend import recomendar
from handlers.baltigoflix import baltigoflix
from handlers.metricas import metricas, metricas_limpar
from handlers.pedido import pedido
from handlers.calendario import calendario
from handlers.broadcast import (
    broadcast_command,
    broadcast_callbacks,
    broadcast_message_router,
)
from handlers.referral import indicacoes, referral_button
from handlers.referral_admin import refstats, auto_referral_check_job
from services.referral_db import init_referral_db
from handlers.bingo import bingo
from handlers.bingo_admin import startbingo, sortear, startbingo_auto, resetbingo

from services.metrics import init_metrics_db
from services.animefire_client import preload_popular_cache

from telegram.ext import InlineQueryHandler
from handlers.inline import inline_query
from handlers.testminiapp import testminiapp

from handlers.epanime import (
    epanime_command,
    epanime_select_callback,
    epanime_page_callback,
    epanime_back_search_callback,
)

init_metrics_db()


async def post_init(app: Application):
    asyncio.create_task(preload_popular_cache())


async def post_shutdown(app: Application):
    await close_http_client()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("ERRO:", repr(context.error))
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "❌ Ocorreu um erro ao processar sua solicitação."
            )
    except Exception:
        pass


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Configure BOT_TOKEN nas variáveis de ambiente.")

    init_referral_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("testminiapp", testminiapp))
    app.add_handler(CommandHandler("buscar", buscar))
    app.add_handler(CommandHandler("ajuda", ajuda))
    app.add_handler(CommandHandler("infoanime", infoanime))
    app.add_handler(CommandHandler("postanime", postanime))
    app.add_handler(CommandHandler("postnovoseps", postnovoseps))
    app.add_handler(CommandHandler("postfilmes", postfilmes))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("indicacoes", indicacoes))
    app.add_handler(CommandHandler("refstats", refstats))
    app.add_handler(CommandHandler("recomendar", recomendar))
    app.add_handler(CommandHandler("bingo", bingo))
    app.add_handler(CommandHandler("startbingo", startbingo))
    app.add_handler(CommandHandler("sortear", sortear))
    app.add_handler(CommandHandler("autobingo", startbingo_auto))
    app.add_handler(CommandHandler("resetbingo", resetbingo))
    app.add_handler(CommandHandler("baltigoflix", baltigoflix))
    app.add_handler(CommandHandler("metricas", metricas))
    app.add_handler(CommandHandler("metricaslimpar", metricas_limpar))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(CommandHandler("pedido", pedido))
    app.add_handler(CommandHandler("calendario", calendario))
    app.add_handler(CommandHandler("epanime", epanime))
    app.add_handler(CallbackQueryHandler(epanime_select_callback, pattern=r"^epanime_select:"))
    app.add_handler(CallbackQueryHandler(epanime_page_callback, pattern=r"^epanime_page:"))
    app.add_handler(CallbackQueryHandler(epanime_back_search_callback, pattern=r"^epanime_back_search$"))

    app.add_handler(CallbackQueryHandler(callback_info_anime, pattern=r"^info_anime:"))
    app.add_handler(CallbackQueryHandler(broadcast_callbacks, pattern=r"^bc\|"))
    app.add_handler(CallbackQueryHandler(referral_button, pattern=r"^noop_indicar$"))
    app.add_handler(CallbackQueryHandler(callbacks))

    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            broadcast_message_router,
        ),
        group=99,
    )

    if app.job_queue:
        app.job_queue.run_repeating(
            auto_post_new_eps_job,
            interval=600,
            first=15,
            name="auto_post_new_eps",
        )

        app.job_queue.run_repeating(
            auto_referral_check_job,
            interval=3600,
            first=60,
            name="auto_referral_check",
        )

    app.add_error_handler(error_handler)

    print("Bot rodando...")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
