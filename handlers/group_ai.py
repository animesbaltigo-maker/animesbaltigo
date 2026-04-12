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

# Captura qualquer texto em <b>...</b> — candidatos a título de anime
_BOLD_RE = re.compile(r"<b>([^<]{2,60})</b>")

# Palavras que definitivamente não são títulos de anime
_NOISE_WORDS = frozenset([
    "baltigo", "animesbaltigo", "mangasbaltigo", "miniapp", "webapp",
    "bot", "como", "aqui", "olha", "veja", "atenção", "nota", "dica",
    "spoiler", "aviso", "importante", "resultado", "busca", "episódio",
    "temporada", "dublado", "legendado", "disponível", "acesse",
    "entre", "envie", "abra", "escolha", "toque", "clique",
    "gênero", "ação", "romance", "comédia", "terror", "drama",
    "mistério", "fantasia", "esportes", "recomendação", "sugestão",
    "passo", "exemplo", "oi", "olá", "sim", "não",
])

# Quantos animes resolver no máximo por resposta
_MAX_BUTTONS = 3
# Timeout por busca (paralelas, não somam)
_RESOLVE_TIMEOUT = 4.5
# Score mínimo que consideramos match confiante (evita falsos positivos)
_MIN_SCORE_RATIO = 0.55


# ─── Detecção e resolução ─────────────────────────────────────────────────────

def _extract_candidates(text: str) -> list[str]:
    """
    Extrai candidatos a título de anime do texto HTML da Akira.
    Pega conteúdo de <b>...</b>, filtra ruído e deduplicada.
    """
    seen: set[str] = set()
    candidates: list[str] = []

    for match in _BOLD_RE.finditer(text):
        raw = match.group(1).strip()

        # Ignora vazios, muito curtos, usernames de bot (@...) e noise words
        if not raw or len(raw) < 3:
            continue
        if raw.startswith("@"):
            continue
        if raw.lower() in _NOISE_WORDS:
            continue
        # Ignora frases longas (mais de 5 palavras provavelmente não é título)
        if len(raw.split()) > 6:
            continue
        # Ignora se começa com emoji ou símbolo
        if raw[0] in "🎌⚡🔥💡📖🤖🎬📅🎲🧠🔎":
            continue

        key = raw.lower()
        if key not in seen:
            seen.add(key)
            candidates.append(raw)

        if len(candidates) >= _MAX_BUTTONS * 2:  # busca extra pra ter margem
            break

    return candidates


def _title_similarity(query: str, result_title: str) -> float:
    """
    Score simples de similaridade entre o candidato extraído e o título retornado.
    Retorna 0.0–1.0. Não depende de libs externas.
    """
    q = query.lower().strip()
    r = result_title.lower().strip()

    if q == r:
        return 1.0
    if q in r or r in q:
        return 0.85

    # Jaccard nas palavras
    q_words = set(q.split())
    r_words = set(r.split())
    if not q_words or not r_words:
        return 0.0
    intersection = q_words & r_words
    union = q_words | r_words
    return len(intersection) / len(union)


async def _resolve_one(candidate: str) -> tuple[str, str] | None:
    """
    Tenta resolver um candidato a um (display_title, anime_id) real.
    Retorna None se não achar com confiança suficiente.
    """
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

    best = results[0]
    best_title = str(best.get("title") or "").strip()
    anime_id   = str(best.get("id")    or "").strip()

    if not anime_id or not best_title:
        return None

    # Verifica se o resultado é realmente sobre o anime que a Akira mencionou
    score = _title_similarity(candidate, best_title)
    if score < _MIN_SCORE_RATIO:
        print(f"[Akira][resolve] '{candidate}' → '{best_title}' score={score:.2f} — descartado")
        return None

    return best_title, anime_id


async def _resolve_buttons(text: str) -> InlineKeyboardMarkup | None:
    """
    Pipeline completo: extrai candidatos → resolve em paralelo → monta teclado.
    Retorna None se nenhum anime for resolvido com confiança.
    """
    candidates = _extract_candidates(text)
    if not candidates:
        return None

    # Resolve todos em paralelo, limita aos primeiros _MAX_BUTTONS*2
    raw_results = await asyncio.gather(
        *[_resolve_one(c) for c in candidates[:_MAX_BUTTONS * 2]],
        return_exceptions=False,
    )

    # Filtra Nones e deduplicada por anime_id
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

        rows.append([
            InlineKeyboardButton(
                text=label,
                url=f"https://t.me/{BOT_USERNAME}?start=anime_{anime_id}",
            )
        ])

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

    if not reply or reply == NO_REPLY_TOKEN:
        return

    # ── Resolve botões em paralelo com o split ──────────────────────────────
    parts, keyboard = await asyncio.gather(
        asyncio.to_thread(split_for_telegram, reply),
        _resolve_buttons(reply),
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
