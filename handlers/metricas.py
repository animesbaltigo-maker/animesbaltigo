import html
import os

from telegram import Update
from telegram.ext import ContextTypes

from services.metrics import get_metrics_report, clear_metrics

ADMIN_IDS = {
    int(os.getenv("ADMIN_ID", "1852596083")),
}


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _fmt_top(rows, empty_text="Nenhum dado"):
    if not rows:
        return empty_text

    lines = []
    for idx, row in enumerate(rows, start=1):
        label = html.escape(str(row["label"]))
        total = row["total"]
        lines.append(f"{idx}. <code>{label}</code> — <b>{total}</b>")
    return "\n".join(lines)


def _normalize_period(args: list[str]) -> str:
    if not args:
        return "total"

    raw = (args[0] or "").strip().lower()

    aliases = {
        "hoje": "hoje",
        "today": "hoje",
        "7d": "7d",
        "7dias": "7d",
        "7": "7d",
        "semana": "7d",
        "30d": "30d",
        "30dias": "30d",
        "30": "30d",
        "mes": "30d",
        "mês": "30d",
        "total": "total",
        "all": "total",
    }

    return aliases.get(raw, "total")


def _period_label(period: str) -> str:
    labels = {
        "hoje": "Hoje",
        "7d": "Últimos 7 dias",
        "30d": "Últimos 30 dias",
        "total": "Total",
    }
    return labels.get(period, "Total")


async def metricas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not user or not _is_admin(user.id):
        await update.effective_message.reply_text(
            "❌ Você não tem permissão para usar esse comando."
        )
        return

    period = _normalize_period(context.args or [])
    data = get_metrics_report(limit=7, period=period)

    text = (
        f"📊 <b>Métricas do bot</b>\n"
        f"🗂 <b>Período:</b> {html.escape(_period_label(period))}\n\n"

        "🔎 <b>Buscas mais feitas</b>\n"
        f"{_fmt_top(data['top_searches'])}\n\n"

        "🎬 <b>Animes mais abertos</b>\n"
        f"{_fmt_top(data['top_opened_animes'])}\n\n"

        "▶️ <b>Cliques em assistir</b>\n"
        f"{_fmt_top(data['top_watch_clicks'])}\n\n"

        "📺 <b>Episódios acessados</b>\n"
        f"{_fmt_top(data['top_episodes'])}\n\n"

        "📉 <b>Buscas sem resultado</b>\n"
        f"<b>{data['searches_without_result']}</b>\n\n"

        "👥 <b>Novos usuários</b>\n"
        f"<b>{data['new_users']}</b>\n\n"

        "🔁 <b>Usuários ativos</b>\n"
        f"<b>{data['active_users']}</b>"
    )

    await update.effective_message.reply_text(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def metricas_limpar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not user or not _is_admin(user.id):
        await update.effective_message.reply_text(
            "❌ Você não tem permissão para usar esse comando."
        )
        return

    clear_metrics()

    await update.effective_message.reply_text(
        "🗑 <b>Todas as métricas foram limpas com sucesso.</b>",
        parse_mode="HTML",
    )