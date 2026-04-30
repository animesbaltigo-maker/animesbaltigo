import html
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes

from config import BOT_USERNAME, STICKER_DIVISOR, WEBAPP_BASE_URL
from services.affiliate_db import affiliate_summary, cents_to_money

DEFAULT_BANNER_URL = (
    "https://photo.chelpbot.me/AgACAgEAAxkBaz9i2mnzeCnUmtUCTPw2T4wmM5Ko9-20AALSC2sb37mgR1jBwbjGWjhgAQADAgADeQADOwQ/photo.jpg"
)


def _bot_username() -> str:
    return (BOT_USERNAME or "AnimesBaltigo_Bot").strip().lstrip("@")


def _affiliate_webapp_url(user_id: int) -> str:
    base = (WEBAPP_BASE_URL or "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}/affiliate?user_id={int(user_id)}&bot={quote(_bot_username())}"


async def _send_panel(message, user_id: int):
    summary = affiliate_summary(user_id)
    link = f"https://t.me/{_bot_username()}?start=ref_{user_id}"
    app_url = _affiliate_webapp_url(user_id)

    text = (
        "<b>💸 PROGRAMA DE AFILIADOS BALTIGO</b>\n\n"
        "<i>Transforme o bot de animes em uma fonte de renda dentro do Telegram.</i>\n\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "<b>📊 Seu desempenho</b>\n\n"
        f"💰 Disponível: <b>{cents_to_money(summary['available_cents'])}</b>\n"
        f"⏳ Em garantia: <b>{cents_to_money(summary['pending_cents'])}</b>\n"
        f"📈 Vendas válidas: <b>{summary['valid_sales']}</b>\n\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "<b>🚀 Como funciona</b>\n\n"
        "Você indica → a pessoa assina o offline\n"
        "→ você ganha comissão automaticamente\n\n"
        "Após <b>7 dias</b>, o valor fica disponível pra saque via Pix.\n\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "<b>⚡ Painel completo</b>\n\n"
        "Acesse seu painel para:\n\n"
        "• 🔗 Ver e copiar seu link\n"
        "• 👥 Acompanhar indicados\n"
        "• 📊 Ver cliques e conversões\n"
        "• 💰 Controlar comissões\n"
        "• 🏦 Solicitar saque\n\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "<i>Toque no botão abaixo para abrir seu painel 👇</i>"
    )

    rows = []
    if app_url:
        rows.append([InlineKeyboardButton("Abrir painel de afiliados", web_app=WebAppInfo(url=app_url))])

    telegram_text = (
        "🎬🔥 ANIME OFFLINE CHEGOU NO BALTIGO\n\n"
        f"Agora o @{_bot_username()} ficou ainda mais absurdo.\n\n"
        "Você pode baixar e assistir seus animes OFFLINE, quando quiser, onde quiser.\n\n"
        "🔥 Acesse agora:\n"
        f"👉 {link}"
    )
    rows.append([InlineKeyboardButton("Compartilhar no Telegram", url=f"https://t.me/share/url?text={quote(telegram_text)}")])
    rows.append([InlineKeyboardButton("Compartilhar no WhatsApp", url="https://wa.me/?text=" + quote(telegram_text))])

    if not app_url:
        text += "\n\nConfigure <code>WEBAPP_BASE_URL</code> para ativar o botão do painel."

    banner = STICKER_DIVISOR if str(STICKER_DIVISOR or "").startswith("http") else DEFAULT_BANNER_URL
    try:
        await message.reply_photo(
            photo=banner,
            caption=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except Exception:
        await message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
            disable_web_page_preview=True,
        )


async def indicacoes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    await _send_panel(message, user.id)


async def referral_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    if query.data != "noop_indicar":
        return

    await query.answer()
    await _send_panel(query.message, user.id)
