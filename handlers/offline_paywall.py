from __future__ import annotations

import html
import time
from urllib.parse import urlencode

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import BALTIGOFLIX_SUBSCRIBE_URL, BALTIGOFLIX_SUPPORT_URL
from services.subscriptions import create_subscription_intent, get_active_subscription


def _subscribe_url(token: str, user_id: int) -> str:
    separator = "&" if "?" in BALTIGOFLIX_SUBSCRIBE_URL else "?"
    query = urlencode({"ref": token, "tg_id": str(user_id), "source": "anime_offline"})
    return f"{BALTIGOFLIX_SUBSCRIBE_URL}{separator}{query}"


def _keyboard(user_id: int, token: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🚀 Assinar BaltigoFlix", url=_subscribe_url(token, user_id))],
        [InlineKeyboardButton("🔄 Já paguei / verificar", callback_data="subcheck")],
    ]
    if BALTIGOFLIX_SUPPORT_URL:
        rows.append([InlineKeyboardButton("🛟 Suporte", url=BALTIGOFLIX_SUPPORT_URL)])
    return InlineKeyboardMarkup(rows)


def _text(title: str = "") -> str:
    anime_line = f"\n\n» <b>Anime:</b> <i>{html.escape(title)}</i>" if title else ""
    return (
        "🔒 <b>Conteúdo exclusivo para assinantes da BaltigoFlix</b>"
        f"{anime_line}\n\n"
        "O download offline de episódios está bloqueado para quem não é assinante.\n\n"
        "Escolha um plano no site. Assim que a Cakto aprovar o pagamento, "
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
