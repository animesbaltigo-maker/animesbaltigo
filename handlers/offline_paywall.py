from __future__ import annotations

import html
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import BALTIGOFLIX_SUPPORT_URL, BOT_BRAND
from services.cakto_api import cakto_api_configured, verify_cakto_payment_for_user
from services.cakto_gateway import get_checkout_options
from services.subscriptions import get_active_subscription

BALTIGOFLIX_OFFER_IMAGE = "https://cdn-checkout.cakto.com.br/images/8f71ba5b-ae9d-45d7-a959-dc749fb51543.jpg"


def _keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(option["label"], url=option["url"])]
        for option in get_checkout_options(user_id)
    ]
    rows.append([InlineKeyboardButton("🔄 Já paguei / verificar", callback_data="subcheck")])
    if BALTIGOFLIX_SUPPORT_URL:
        rows.append([InlineKeyboardButton("🛟 Falar com suporte", url=BALTIGOFLIX_SUPPORT_URL)])
    return InlineKeyboardMarkup(rows)


def _text(title: str = "") -> str:
    brand = html.escape(BOT_BRAND or "BaltigoFlix")
    anime_line = f"» <b>Anime:</b> <i>{html.escape(title)}</i>\n" if title else ""

    return (
        "📥 <b>Download offline bloqueado</b>\n\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"🔒 <b>Área exclusiva para assinantes do {brand}</b>\n"
        f"{anime_line}"
        "» <b>Status:</b> <code>sem assinatura ativa</code>\n"
        "» <b>Liberação:</b> <i>automática pelo seu Telegram ID</i>\n\n"
        "✨ <b>Com o offline você pode:</b>\n"
        "• 📲 Baixar episódios direto no Telegram\n"
        "• 🎬 Assistir quando quiser, sem abrir site\n"
        "• ⚡ Receber o arquivo protegido no seu privado\n\n"
        "🍿 <b>E não para por aí:</b>\n"
        "• 📺 Libera também o acesso completo à BaltigoFlix\n"
        "• 🎞 Filmes, séries, canais, esportes e conteúdo premium\n"
        "• 🚀 Tudo em um só acesso, direto no seu aparelho\n\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "💎 <b>Escolha um plano abaixo para liberar agora.</b>\n"
        "Depois do pagamento, toque em <b>🔄 Já paguei / verificar</b>."
    )


async def send_offline_paywall(query, user, title: str = "") -> None:
    await query.answer("🔒 Offline exclusivo para assinantes.", show_alert=True)
    if query.message:
        try:
            await query.message.reply_photo(
                photo=BALTIGOFLIX_OFFER_IMAGE,
                caption=_text(title),
                parse_mode="HTML",
                reply_markup=_keyboard(user.id),
            )
        except Exception:
            await query.message.reply_text(
                _text(title),
                parse_mode="HTML",
                reply_markup=_keyboard(user.id),
                disable_web_page_preview=True,
            )


async def answer_subscription_check(query, user_id: int) -> None:
    sub = get_active_subscription(user_id)
    if not sub and cakto_api_configured():
        await query.answer("⏳ Verificando pagamento na Cakto...", show_alert=False)
        try:
            result = await verify_cakto_payment_for_user(user_id)
        except Exception:
            result = {"ok": False, "reason": "api_error"}
        if result.get("ok"):
            sub = get_active_subscription(user_id)

    if not sub:
        if not cakto_api_configured():
            text = (
                "⚠️ Não consegui verificar pela API da Cakto.\n\n"
                "Chame o suporte para fazermos a liberação manual."
            )
        else:
            text = (
                "⏳ Pagamento ainda não confirmado.\n\n"
                "Se o Pix já saiu da conta, aguarde alguns instantes e toque em verificar de novo."
            )
        await query.answer(text, show_alert=True)
        return

    expires_at = int(sub.get("expires_at") or 0)
    days_left = max(0, int((expires_at - int(time.time())) / 86400))
    await query.answer(
        f"✅ Assinatura ativa!\n\nPlano: {sub.get('plan_name') or 'BaltigoFlix'}\n"
        f"Validade restante: {days_left} dia(s).",
        show_alert=True,
    )
