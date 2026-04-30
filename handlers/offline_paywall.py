from __future__ import annotations

import html
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    BALTIGOFLIX_SUPPORT_URL,
    BOT_BRAND,
)
from services.cakto_api import cakto_api_configured, verify_cakto_payment_for_user
from services.cakto_gateway import get_checkout_options
from services.subscriptions import get_active_subscription


def _keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(option["label"], url=option["url"])]
        for option in get_checkout_options(user_id)
    ]
    rows.append([InlineKeyboardButton("Ja paguei / verificar", callback_data="subcheck")])
    if BALTIGOFLIX_SUPPORT_URL:
        rows.append([InlineKeyboardButton("Suporte", url=BALTIGOFLIX_SUPPORT_URL)])
    return InlineKeyboardMarkup(rows)


def _text(title: str = "") -> str:
    anime_line = f"\n\n» <b>Anime:</b> <i>{html.escape(title)}</i>" if title else ""
    brand = html.escape(BOT_BRAND or "BaltigoFlix")
    return (
        f"<b>Offline exclusivo para assinantes do {brand}</b>"
        f"{anime_line}\n\n"
        "Para baixar episodios pelo bot, escolha um plano abaixo.\n\n"
        "Depois do pagamento, toque em <b>Ja paguei / verificar</b>. "
        "O bot consulta a Cakto e libera seu Telegram automaticamente."
    )


async def send_offline_paywall(query, user, title: str = "") -> None:
    await query.answer("Offline exclusivo para assinantes.", show_alert=True)
    if query.message:
        await query.message.reply_text(
            _text(title),
            parse_mode="HTML",
            reply_markup=_keyboard(user.id),
            disable_web_page_preview=True,
        )


async def answer_subscription_check(query, user_id: int) -> None:
    sub = get_active_subscription(user_id)
    if not sub and cakto_api_configured():
        await query.answer("Verificando pagamento na Cakto...", show_alert=False)
        try:
            result = await verify_cakto_payment_for_user(user_id)
        except Exception:
            result = {"ok": False, "reason": "api_error"}
        if result.get("ok"):
            sub = get_active_subscription(user_id)

    if not sub:
        if not cakto_api_configured():
            text = (
                "Nao consegui verificar pela API da Cakto.\n\n"
                "Configure CAKTO_CLIENT_ID e CAKTO_CLIENT_SECRET ou chame o suporte para liberacao manual."
            )
        else:
            text = (
                "Ainda nao encontrei pagamento aprovado para seu Telegram.\n\n"
                "Se o Pix ja saiu da conta, aguarde alguns instantes e tente de novo ou chame o suporte."
            )
        await query.answer(text, show_alert=True)
        return

    expires_at = int(sub.get("expires_at") or 0)
    days_left = max(0, int((expires_at - int(time.time())) / 86400))
    await query.answer(
        f"Assinatura ativa: {sub.get('plan_name') or 'BaltigoFlix'}\nValidade restante: {days_left} dia(s).",
        show_alert=True,
    )
