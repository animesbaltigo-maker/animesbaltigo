from __future__ import annotations

import html
import time
from urllib.parse import urlencode

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    BALTIGOFLIX_SUBSCRIBE_URL,
    BALTIGOFLIX_SUPPORT_URL,
    CAKTO_ANUAL_CHECKOUT_URL,
    CAKTO_BRONZE_CHECKOUT_URL,
    CAKTO_CHECKOUT_URL,
    CAKTO_DIAMANTE_CHECKOUT_URL,
    CAKTO_MENSAL_CHECKOUT_URL,
    CAKTO_OURO_CHECKOUT_URL,
    CAKTO_RUBI_CHECKOUT_URL,
    CAKTO_SEMESTRAL_CHECKOUT_URL,
    CAKTO_TRIMESTRAL_CHECKOUT_URL,
)
from services.subscriptions import create_subscription_intent, get_active_subscription


def _tracked_url(base_url: str, token: str, user_id: int, plan_code: str = "") -> str:
    separator = "&" if "?" in base_url else "?"
    query = urlencode({
        "ref": token,
        "tg_id": str(user_id),
        "source": "anime_offline",
        "external_reference": token,
        "utm_source": "anime_bot",
        "utm_medium": "telegram",
        "utm_campaign": "offline_download",
        "utm_content": token,
        "sck": token,
        "plan": plan_code,
    })
    return f"{base_url}{separator}{query}"


def _plan_buttons(user_id: int, token: str) -> list[list[InlineKeyboardButton]]:
    plans = [
        ("Plano mensal", CAKTO_MENSAL_CHECKOUT_URL or CAKTO_BRONZE_CHECKOUT_URL, "mensal"),
        ("Plano trimestral", CAKTO_TRIMESTRAL_CHECKOUT_URL or CAKTO_OURO_CHECKOUT_URL, "trimestral"),
        ("Plano semestral", CAKTO_SEMESTRAL_CHECKOUT_URL or CAKTO_DIAMANTE_CHECKOUT_URL, "semestral"),
        ("Plano anual", CAKTO_ANUAL_CHECKOUT_URL or CAKTO_RUBI_CHECKOUT_URL, "anual"),
    ]
    rows = [
        [InlineKeyboardButton(label, url=_tracked_url(url, token, user_id, code))]
        for label, url, code in plans
        if url
    ]
    if rows:
        return rows

    fallback = CAKTO_CHECKOUT_URL or BALTIGOFLIX_SUBSCRIBE_URL
    return [[
        InlineKeyboardButton(
            "🚀 Assinar BaltigoFlix",
            url=_tracked_url(fallback, token, user_id, "baltigoflix"),
        )
    ]]


def _keyboard(user_id: int, token: str) -> InlineKeyboardMarkup:
    rows = _plan_buttons(user_id, token)
    rows.append([InlineKeyboardButton("🔄 Já paguei / verificar", callback_data="subcheck")])
    if BALTIGOFLIX_SUPPORT_URL:
        rows.append([InlineKeyboardButton("🛟 Suporte", url=BALTIGOFLIX_SUPPORT_URL)])
    return InlineKeyboardMarkup(rows)


def _text(title: str = "") -> str:
    anime_line = f"\n\n» <b>Anime:</b> <i>{html.escape(title)}</i>" if title else ""
    return (
        "🔒 <b>Conteúdo exclusivo para assinantes da BaltigoFlix</b>"
        f"{anime_line}\n\n"
        "O download offline de episódios está bloqueado para quem não é assinante.\n\n"
        "Escolha um plano. Assim que a Cakto aprovar o pagamento, "
        "o bot libera seu Telegram ID automaticamente pelo tempo correspondente ao plano.\n\n"
        "📌 <b>Seu acesso atual:</b> <code>sem assinatura ativa</code>\n"
        "📈 <b>Para liberar:</b> assine a BaltigoFlix."
    )


async def send_offline_paywall(query, user, title: str = "") -> None:
    token = create_subscription_intent(
        user_id=user.id,
        username=user.username or "",
        full_name=" ".join(part for part in [user.first_name, user.last_name] if part),
    )["token"]

    await query.answer("🔒 Offline exclusivo para assinantes BaltigoFlix.", show_alert=True)
    if query.message:
        await query.message.reply_text(
            _text(title),
            parse_mode="HTML",
            reply_markup=_keyboard(user.id, token),
        )


async def answer_subscription_check(query, user_id: int) -> None:
    sub = get_active_subscription(user_id)
    if not sub:
        await query.answer(
            "Ainda não encontrei assinatura ativa para seu Telegram ID. Se acabou de pagar, aguarde a aprovação da Cakto.",
            show_alert=True,
        )
        return

    expires_at = int(sub.get("expires_at") or 0)
    days_left = max(0, int((expires_at - int(time.time())) / 86400))
    await query.answer(
        f"Assinatura ativa: {sub.get('plan_name') or 'BaltigoFlix'}\nValidade restante: {days_left} dia(s).",
        show_alert=True,
    )
