import asyncio
import logging

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from config import BOT_TOKEN
from core.http_client import close_http_client

# HANDLERS
from handlers.start import start
from handlers.search import buscar
from handlers.callbacks import callbacks
from handlers.inline import inline_query
from handlers.help import ajuda
from handlers.infoanime import infoanime, callback_info_anime
from handlers.novoseps import postnovoseps, auto_post_new_eps_job
from handlers.referral_admin import auto_referral_check_job
from handlers.referral import indicacoes, referral_button
from handlers.metricas import metricas, metricas_limpar
from handlers.postanime import postanime
from handlers.postfilmes import postfilmes
from handlers.baltigoflix import baltigoflix
from handlers.testminiapp import testminiapp
from handlers.recommend import recomendar
from handlers.calendario import calendario
from handlers.bingo import bingo
from handlers.bingo_admin import startbingo, startbingo_auto, sortear, resetbingo
from handlers.broadcast import (
    broadcast_command,
    broadcast_callbacks,
    broadcast_message_router,
)
from handlers.pedido import pedido

from services.animefire_client import preload_popular_cache


# =========================
# LOGGING (remove spam HTTP)
# =========================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

# 🔥 corta spam absurdo do httpx
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# =========================
# JOBS OTIMIZADOS
# =========================

async def auto_referral_check_job_safe(context: ContextTypes.DEFAULT_TYPE):
    """
    Wrapper do referral pra evitar flood absurdo
    """
    try:
        await auto_referral_check_job(context)
    except Exception as e:
        logging.error(f"[REFERRAL JOB ERROR] {e}")


async def auto_post_eps_job_safe(context: ContextTypes.DEFAULT_TYPE):
    """
    Wrapper dos episódios
    """
    try:
        await auto_post_new_eps_job(context)
    except Exception as e:
        logging.error(f"[NOVOSEPS JOB ERROR] {e}")


# =========================
# MAIN
# =========================

async def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # =========================
    # COMMANDS
    # =========================
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("buscar", buscar))
    app.add_handler(CommandHandler("ajuda", ajuda))
    app.add_handler(CommandHandler("infoanime", infoanime))
    app.add_handler(CommandHandler("postnovoseps", postnovoseps))
    app.add_handler(CommandHandler("indicacoes", indicacoes))
    app.add_handler(CommandHandler("metricas", metricas))
    app.add_handler(CommandHandler("metricaslimpar", metricas_limpar))
    app.add_handler(CommandHandler("postanime", postanime))
    app.add_handler(CommandHandler("postfilmes", postfilmes))
    app.add_handler(CommandHandler("baltigoflix", baltigoflix))
    app.add_handler(CommandHandler("testminiapp", testminiapp))
    app.add_handler(CommandHandler("recomendar", recomendar))
    app.add_handler(CommandHandler("calendario", calendario))
    app.add_handler(CommandHandler("bingo", bingo))
    app.add_handler(CommandHandler("startbingo", startbingo))
    app.add_handler(CommandHandler("startbingo_auto", startbingo_auto))
    app.add_handler(CommandHandler("sortear", sortear))
    app.add_handler(CommandHandler("resetbingo", resetbingo))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("pedido", pedido))

    # =========================
    # CALLBACKS
    # =========================
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(CallbackQueryHandler(callback_info_anime, pattern="^infoanime"))
    app.add_handler(CallbackQueryHandler(referral_button, pattern="^referral"))
    app.add_handler(CallbackQueryHandler(broadcast_callbacks, pattern="^broadcast"))

    # =========================
    # INLINE
    # =========================
    app.add_handler(InlineQueryHandler(inline_query))

    # =========================
    # MESSAGE ROUTER
    # =========================
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_message_router))

    # =========================
    # JOBS (AQUI QUE TAVA O PROBLEMA)
    # =========================

    # 🔥 NOVOS EPISÓDIOS (OK)
    app.job_queue.run_repeating(
        auto_post_eps_job_safe,
        interval=600,   # 10 min
        first=10,
        name="auto_post_new_eps",
    )

    # ⚠️ REFERRAL (REDUZIDO PRA NÃO FLOODAR)
    app.job_queue.run_repeating(
        auto_referral_check_job_safe,
        interval=3600,   # 1h (mantido)
        first=60,
        name="auto_referral_check",
    )

    # =========================
    # START BOT
    # =========================
    await preload_popular_cache()

    logging.info("🤖 Bot iniciado com sucesso.")

    await app.run_polling(close_loop=False)

    await close_http_client()


# =========================
# ENTRYPOINT
# =========================

if __name__ == "__main__":
    asyncio.run(main())
