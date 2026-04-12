"""
handlers/group_ai.py — Handler da Assistente Akira

Funcionalidades:
  ✅ parse_mode="HTML" em todos os envios
  ✅ Memória curta por chat (últimos 6 turnos, TTL 30 min)
  ✅ Detecção automática de animes em negrito <b>...</b> na resposta
  ✅ Busca paralela via search_anime() com score — só adiciona botão se match confiante
  ✅ Botão ▶️ Assistir agora com deep link direto pro bot
  ✅ Silencioso quando não acha — sem erro pro usuário
  ✅ Comando /esquecer para limpar contexto
"""

import asyncio
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import BOT_USERNAME
from services.animefire_client import search_anime
from services.gemini_ai import generate_anime_reply, split_for_telegram, NO_REPLY_TOKEN
from services.memory import conversation_memory
from utils.gatekeeper import ensure_channel_membership

TRIGGER = "akira"

_MAX_BUTTONS    = 3
_RESOLVE_TIMEOUT = 4.5
_MIN_SCORE_RATIO = 0.60

# Regex para capturar conteúdo de <b>...</b> (até 120 chars para pegar listas)
_BOLD_RE  = re.compile(r"<b>([^<]{2,120})</b>")
# Separadores de lista dentro de um <b>: "X, Y e Z" ou "X ou Y"
_SPLIT_RE = re.compile(r"\s*(?:,\s*|\s+e\s+|\s+ou\s+)\s*")

# Palavras/prefixos que não são títulos de anime
_NOISE_EXACT = frozenset([
    "baltigo", "bot", "miniapp", "webapp", "busca", "episódio", "temporada",
    "dublado", "legendado", "disponível", "gênero", "ação", "romance",
    "comédia", "terror", "drama", "mistério", "fantasia", "esportes",
    "recomendação", "sugestão", "oi", "olá", "sim", "não", "anime", "mangá",
])
_NOISE_PREFIX = ("@", "/", "como", "aqui", "olha", "veja", "entre", "envie",
                 "abra", "escolha", "toque", "clique", "acesse", "passo")


# ─── Detecção e resolução ─────────────────────────────────────────────────────

def _extract_candidates(reply: str, user_text: str) -> list[str]:
    """
    Estratégia em duas camadas:

    1. Prioridade: anime mencionado explicitamente pelo usuário na mensagem.
       Se o usuário disse "quero assistir Naruto", Naruto é o candidato principal.

    2. Complemento: títulos em <b>...</b> da resposta da Akira.
       Divide listas ("X, Y e Z" dentro de um <b>) em candidatos individuais.

    Isso evita o bug de pegar animes mencionados "de passagem" na resposta
    quando o usuário perguntou sobre um anime específico.
    """
    seen: set[str] = set()
    candidates: list[str] = []

    def _add(title: str) -> None:
        t = title.strip().rstrip(".")
        if len(t) < 3 or len(t) > 50:
            return
        if t.lower() in _NOISE_EXACT:
            return
        if any(t.lower().startswith(p) for p in _NOISE_PREFIX):
            return
        if len(t.split()) > 7:
            return
        key = t.lower()
        if key not in seen:
            seen.add(key)
            candidates.append(t)

    # Camada 1 — anime citado pelo usuário (extrai palavras capitalizadas
    # ou sequências que parecem título após "assistir/ver/quero/buscar")
    intent_re = re.compile(
        r"(?:assistir|ver|buscar|jogar|ler|quero|gosto de|falar de|sobre)"
        r"\s+([^?!.,\n]{2,40})",
        re.IGNORECASE,
    )
    for m in intent_re.finditer(user_text):
        _add(m.group(1).strip())

    # Camada 2 — <b>títulos</b> da resposta, com split de listas
    for m in _BOLD_RE.finditer(reply):
        raw = m.group(1).strip()
        parts = _SPLIT_RE.split(raw)
        for p in parts:
            _add(p)
        if len(candidates) >= _MAX_BUTTONS * 2:
            break

    return candidates[:_MAX_BUTTONS * 2]


def _title_similarity(query: str, result_title: str) -> float:
    q = query.lower().strip()
    r = result_title.lower().strip()
    if q == r:
        return 1.0
    if q in r or r in q:
        return 0.85
    q_words = set(q.split())
    r_words = set(r.split())
    if not q_words or not r_words:
        return 0.0
    return len(q_words & r_words) / len(q_words | r_words)


async def _resolve_one(candidate: str) -> tuple[str, str] | None:
    try:
        results = await asyncio.wait_for(
            search_anime(candidate),
            timeout=_RESOLVE_TIMEOUT,
        )
    except Exception as e:
        print(f"[Akira][resolve] erro '{candidate}': {e}")
        return None
    if not results:
        return None
    best       = results[0]
    best_title = str(best.get("title") or "").strip()
    anime_id   = str(best.get("id")    or "").strip()
    if not anime_id or not best_title:
        return None
    if _title_similarity(candidate, best_title) < _MIN_SCORE_RATIO:
        print(f"[Akira][resolve] '{candidate}' → '{best_title}' descartado")
        return None
    return best_title, anime_id


async def _resolve_buttons(reply: str, user_text: str) -> InlineKeyboardMarkup | None:
    candidates = _extract_candidates(reply, user_text)
    if not candidates:
        return None

    raw_results = await asyncio.gather(
        *[_resolve_one(c) for c in candidates],
        return_exceptions=False,
    )

    seen_ids: set[str] = set()
    rows: list[list[InlineKeyboardButton]] = []

    for result in raw_results:
        if result is None:
            continue
        display_title, anime_id = result
        if anime_id in seen_ids:
            continue
        seen_ids.add(anime_id)
        label = f"▶️ {display_title}"
        if len(label) > 40:
            label = label[:37].rstrip() + "..."
        rows.append([InlineKeyboardButton(
            text=label,
            url=f"https://t.me/{BOT_USERNAME}?start=anime_{anime_id}",
        )])
        if len(rows) >= _MAX_BUTTONS:
            break

    return InlineKeyboardMarkup(rows) if rows else None


# ─── Handler principal ────────────────────────────────────────────────────────

async def group_ai_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    text       = message.text.strip()
    text_lower = text.lower()
    chat_id    = message.chat_id

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

    if not user_text:
        await message.reply_text(
            "Fala comigo assim: <code>akira me recomenda um anime</code> 🔥",
            parse_mode=ParseMode.HTML,
        )
        return

    if not await ensure_channel_membership(update, context):
        return

    # ── Gera resposta com contexto ──────────────────────────────────────────
    history = conversation_memory.get_history(chat_id)

    try:
        reply = await generate_anime_reply(user_text, history=history)
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

    if not reply or reply == NO_REPLY_TOKEN:
        return

    # ── Resolve botões em paralelo com o split ──────────────────────────────
    parts, keyboard = await asyncio.gather(
        asyncio.to_thread(split_for_telegram, reply),  # síncrona leve
        _resolve_buttons(reply, user_text),
    )

    # Salva na memória
    conversation_memory.add_turn(chat_id, user_text, reply)

    # ── Envia — botões só na última parte ───────────────────────────────────
    for i, part in enumerate(parts):
        kb = keyboard if (i == len(parts) - 1) else None
        await message.reply_text(
            part,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )


# ─── /esquecer ───────────────────────────────────────────────────────────────

async def esquecer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    conversation_memory.clear(update.message.chat_id)
    await update.message.reply_text(
        "🧹 Pronto! Esqueci tudo que a gente conversou.\n"
        "Pode começar de novo quando quiser 😊",
        parse_mode=ParseMode.HTML,
    )
