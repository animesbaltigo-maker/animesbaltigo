async def post_init(app):
    me = await app.bot.get_me()
    print("BOT INICIADO:", me.username)

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Configure BOT_TOKEN nas variáveis de ambiente.")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
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
