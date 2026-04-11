"""
handlers/group_ai.py — Handler da Assistente Akira

Correções aplicadas:
  ✅ parse_mode="HTML" em TODOS os reply_text (era o bug das tags visíveis)
  ✅ Memória curta por chat (últimos 6 turnos, TTL 30 min)
  ✅ Comando /esquecer para limpar contexto
  ✅ Split de mensagens longas com HTML preservado
"""

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import BOT_USERNAME
from services.gemini_ai import generate_anime_reply, split_for_telegram, NO_REPLY_TOKEN
from services.memory import conversation_memory
from utils.gatekeeper import ensure_channel_membership

# Palavra-chave que ativa a Akira em grupos
TRIGGER = "akira"


# ─── Handler principal ────────────────────────────────────────────────────────

async def group_ai_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    text       = message.text.strip()
    text_lower = text.lower()
    chat_id    = message.chat_id

    # ── Determina se deve responder e extrai o texto útil ──────────────────
    replying_to_bot = bool(
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.username
        and BOT_USERNAME
        and message.reply_to_message.from_user.username.lower() == BOT_USERNAME.lower()
    )

    chat_type = update.effective_chat.type if update.effective_chat else "private"

    if chat_type == "private":
        user_text = text
    elif text_lower.startswith(TRIGGER):
        user_text = text[len(TRIGGER):].strip()
    elif replying_to_bot:
        user_text = text
    else:
        return

    # ── Texto vazio após o trigger ──────────────────────────────────────────
    if not user_text:
        await message.reply_text(
            "Fala comigo assim: <code>akira me recomenda um anime</code> 🔥",
            parse_mode=ParseMode.HTML,
        )
        return

    # ── Verifica membership no canal obrigatório ────────────────────────────
    if not await ensure_channel_membership(update, context):
        return

    # ── Gera resposta com contexto de memória ───────────────────────────────
    history = conversation_memory.get_history(chat_id)

    try:
        reply = generate_anime_reply(user_text, history=history)
    except RuntimeError as e:
        err = str(e)
        if any(k in err for k in ("RESOURCE_EXHAUSTED", "429", "quota")):
            await message.reply_text(
                "Tch… gastei todo meu chakra respondendo vocês 😵‍💫\n"
                "Me dá um tempinho e tenta de novo, ok?",
                parse_mode=ParseMode.HTML,
            )
        else:
            print(f"[Akira ERROR] {err}")
            await message.reply_text(
                "Tive um bug aqui 😵 tenta de novo em instantes!",
                parse_mode=ParseMode.HTML,
            )
        return

    # ── Ignora respostas fora do domínio ────────────────────────────────────
    if not reply or reply == NO_REPLY_TOKEN:
        return

    # ── Salva turno na memória ──────────────────────────────────────────────
    conversation_memory.add_turn(chat_id, user_text, reply)

    # ── Envia com parse_mode=HTML (esse era o bug das tags como texto) ──────
    parts = split_for_telegram(reply)
    for part in parts:
        await message.reply_text(part, parse_mode=ParseMode.HTML)


# ─── /esquecer — limpa contexto do chat ──────────────────────────────────────

async def esquecer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Apaga o histórico de conversa do chat atual."""
    if not update.message:
        return
    conversation_memory.clear(update.message.chat_id)
    await update.message.reply_text(
        "🧹 Pronto! Esqueci tudo que a gente conversou.\n"
        "Pode começar de novo quando quiser 😊",
        parse_mode=ParseMode.HTML,
    )
