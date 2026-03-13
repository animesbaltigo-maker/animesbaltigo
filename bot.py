from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from config import BOT_TOKEN
from core.http_client import close_http_client
from handlers.start import start
from handlers.search import buscar
from handlers.help import ajuda
from handlers.callbacks import callbacks


async def post_shutdown(app):
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

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("buscar", buscar))
    app.add_handler(CommandHandler("ajuda", ajuda))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_error_handler(error_handler)
    print("Bot rodando...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
