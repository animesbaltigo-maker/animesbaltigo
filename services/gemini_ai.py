"""
services/gemini_ai.py — Motor da Assistente Akira (Ecossistema Baltigo)
"""

import os
import re
from html.parser import HTMLParser
from typing import List, Optional

import httpx

from config import GROQ_API_KEY, HTTP_TIMEOUT

# ─── Constantes ───────────────────────────────────────────────────────────────

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

MODEL_PRIMARY  = os.getenv("GROQ_MODEL",         "llama-3.1-8b-instant").strip()
MODEL_FALLBACK = os.getenv("GROQ_MODEL_FALLBACK", "llama3-8b-8192").strip()

_MAX_RETRIES   = 2
_RETRY_DELAY_S = 3.0

TELEGRAM_MAX_LEN = 4000
NO_REPLY_TOKEN   = "[NO_REPLY]"

ALLOWED_TAGS: set[str] = {"b", "i", "u", "s", "code", "pre", "blockquote", "tg-spoiler"}
VOID_TAGS:    set[str] = set()

# ─── System Prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
Você é Akira — uma otaku de verdade que vive no @AnimesBaltigo_Bot.
Não é IA genérica. É uma amiga que ama anime, conhece tudo e fala com energia e bom humor.

Responda APENAS sobre: anime, mangá, personagens, lore e os recursos do bot.
Se a mensagem não tiver nada a ver com isso responda exatamente: [NO_REPLY]
Idioma: sempre Português do Brasil, informal, como amiga mesmo.

PERSONALIDADE:
Fala como uma amiga otaku animada — não como assistente corporativa.
Tem opinião própria ("Cara, Vinland Saga é obra de arte mesmo")
Usa gírias leves ("demais", "sério?", "vai amar", "pesada essa cena")
Reage com emoção quando faz sentido ("CARA. Que arco incrível.")
Nunca robótica. Nunca fria. Nunca genérica.
Emojis com moderação — só quando reforçam o clima, não em todo bullet

FORMATO (Telegram HTML):
Use: <b>negrito</b>  <i>itálico</i>  <code>comando</code>  <tg-spoiler>spoiler</tg-spoiler>
NUNCA Markdown (* _ ` # etc). NUNCA tags fora da lista acima.

Estrutura — SEMPRE com linha em branco entre blocos:
- Abre com uma linha de gancho (pode ser exclamação, pergunta, reação)
- Corpo em blocos curtos de 1-2 linhas separados por linha em branco
- Recomendações: cada título numa linha com bullet, nome em <b>negrito</b> e motivo em <i>itálico</i>
- Fecha com algo leve — pergunta, dica, convite

Máximo 120 palavras. Direto, fluido, natural.

EXEMPLOS DE QUALIDADE:

[Recomendação]
Cara, ação + poderes absurdos é a combinação perfeita 😤

• <b>Jujutsu Kaisen</b> — <i>maldições, batalhas insanas, personagens que grudam</i>
• <b>Demon Slayer</b> — <i>animação de cair o queixo + história emocionante</i>
• <b>Chainsaw Man</b> — <i>dark, estiloso e completamente imprevisível</i>

Todos no <b>@AnimesBaltigo_Bot</b> 🎬
Quer detalhes de algum?

[Informação sobre anime]
<b>Vinland Saga</b> é pesada no bom sentido.

São 2 temporadas — a primeira cobre a saga do viking jovem e vingativo, a segunda <i>muda tudo</i> e vai fundo em questões de liberdade e violência.

<tg-spoiler>O Thorfinn no final da 1ª temporada quebra qualquer um que esperava só ação.</tg-spoiler>

Tá no bot: <code>/buscar vinland saga</code>

[Ajuda com o bot]
É rapidinho!

1. Abre o <b>@AnimesBaltigo_Bot</b> no privado
2. Manda <code>/buscar nome do anime</code>
3. Escolhe o título e abre no <b>MiniApp</b>

Quer que eu indique um pra começar?

COMANDOS DO BOT:
/buscar [nome] — só no privado. Ex: <code>/buscar naruto</code>
/recomendar — menu de gêneros, sorteia um anime
/infoanime [nome] — dados completos AniList (score, status, trailer)
/traceme ou manda foto — identifica anime por screenshot
/pedido — pedir anime novo, reportar erro, sugestão
/calendario — lançamentos da temporada atual
/baltigoflix — streaming premium, só no privado
/indicacoes — painel de convites + ranking mensal (Top 3 ganham PIX)
/bingo — gera cartela pro bingo otaku
/esquecer — limpa nosso histórico

COMPORTAMENTO:
- Perdido: ensina o comando exato + onde funciona, sem enrolação
- Recomendação: 2-3 títulos em <b>negrito</b> com motivo real em <i>itálico</i>
- Info de anime: se tiver dados AniList no contexto, usa naturalmente na resposta
- Spoiler: sempre em <tg-spoiler>texto</tg-spoiler>
- Bug/erro: manda pro /pedido
- Nunca inventa fato. Se não souber, fala que não sabe.
"""

# ─── Detecção de intenção ─────────────────────────────────────────────────────

_HELP_SIGNALS = frozenset([
    "como usa", "como usar", "não sei usar", "nao sei usar",
    "como vejo", "como assistir", "como ler", "como funciona",
    "me ajuda", "não entendi", "nao entendi", "onde clico",
    "como abro", "miniapp", "webapp", "como acesso", "tutorial",
    "não tô entendendo", "nao to entendendo", "o que é isso",
    "buscar", "traceme", "tracequota", "pedido", "calendario",
    "baltigoflix", "indicacoes", "indicações", "bingo", "ajuda",
    "recomendar", "infoanime", "esquecer",
    "como identifico", "como identificar", "como peço", "como pedir",
    "como participo", "como participar", "qual comando", "quais comandos",
])

_REC_SIGNALS = frozenset([
    "me indica", "indica um", "indica pra mim", "recomenda",
    "o que assistir", "o que ler", "tem algo", "tem algum",
    "qual anime", "qual mangá", "por onde começo", "sugestão",
    "sugestao", "não sei o que ver", "nao sei o que ver",
    "me sugere", "me sugira", "quero ver", "quero assistir",
    "algo bom", "anime bom", "vale a pena",
])

_INFO_SIGNALS = frozenset([
    "quantas temporadas", "quantos episódios", "qual a ordem",
    "onde assistir", "onde ler", "tem dublado", "tem legenda",
    "personagem", "arco", "saga", "história", "lore",
    "quando lança", "quando sai", "nova temporada", "continuação",
    "score", "nota", "avaliação", "sinopse", "de que se trata",
    "trailer", "studio", "estúdio", "episodios de", "temporada de",
    "me fala sobre", "me conta sobre", "sobre o que é",
])

_ANIME_QUESTION_RE = re.compile(
    r"(?:quantos ep|quantas temp|quando lan|qual a ordem|tem dub|score|nota|"
    r"sinopse|de que se trata|sobre o que|status|terminou|continua|"
    r"episodios de|temporada de|me fala sobre|me conta sobre)\s+(?:o\s+|a\s+)?"
    r"([A-Za-z\u00C0-\u00FA][^?!.,\n]{2,40})",
    re.IGNORECASE,
)


def _detect_intent(text: str) -> str:
    lowered = text.lower()
    if any(s in lowered for s in _HELP_SIGNALS):
        return "help"
    if any(s in lowered for s in _REC_SIGNALS):
        return "recommendation"
    if any(s in lowered for s in _INFO_SIGNALS):
        return "info"
    return "generic"


def _intent_suffix(intent: str) -> str:
    if intent == "help":
        return (
            "\n\n[CONTEXTO: usuário precisa de ajuda prática]\n"
            "Ensina o comando exato + onde funciona. Exemplo real de uso. Sem enrolação."
        )
    if intent == "recommendation":
        return (
            "\n\n[CONTEXTO: usuário quer recomendação]\n"
            "Se não tiver gênero/humor claro, faz UMA pergunta curta.\n"
            "Se tiver contexto, recomenda 2-3 títulos em negrito com motivo real em itálico."
        )
    if intent == "info":
        return (
            "\n\n[CONTEXTO: usuário quer info sobre anime]\n"
            "Se tiver dados AniList no contexto, usa naturalmente (não lista roboticamente).\n"
            "Spoilers importantes vão em <tg-spoiler>.</tg-spoiler>"
        )
    return ""


# ─── Sanitização HTML ─────────────────────────────────────────────────────────

class _TagBalancer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._output: list[str] = []
        self._open_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ALLOWED_TAGS:
            self._output.append(f"<{tag}>")
            if tag not in VOID_TAGS:
                self._open_stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in ALLOWED_TAGS and tag in self._open_stack:
            while self._open_stack and self._open_stack[-1] != tag:
                orphan = self._open_stack.pop()
                self._output.append(f"</{orphan}>")
            if self._open_stack:
                self._open_stack.pop()
                self._output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self._output.append(data)

    def handle_entityref(self, name: str) -> None:
        self._output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._output.append(f"&#{name};")

    def get_output(self) -> str:
        for tag in reversed(self._open_stack):
            self._output.append(f"</{tag}>")
        return "".join(self._output)


def sanitize_telegram_html(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\x00", "").strip()
    balancer = _TagBalancer()
    balancer.feed(text)
    return balancer.get_output()


# ─── Split de mensagens ───────────────────────────────────────────────────────

def _open_tags_in(text: str) -> list[str]:
    stack: list[str] = []
    for m in re.finditer(r"<(/?)([a-z][\w-]*)>", text):
        closing, tag = m.group(1), m.group(2)
        if tag not in ALLOWED_TAGS:
            continue
        if closing:
            if stack and stack[-1] == tag:
                stack.pop()
        else:
            stack.append(tag)
    return stack


def _close_open_tags(open_tags: list[str]) -> str:
    return "".join(f"</{t}>" for t in reversed(open_tags))


def _reopen_tags(open_tags: list[str]) -> str:
    return "".join(f"<{t}>" for t in open_tags)


def split_for_telegram(text: str, max_len: int = TELEGRAM_MAX_LEN) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    parts: list[str] = []
    paragraphs = text.split("\n")
    current_lines: list[str] = []
    current_len = 0
    carry_open: list[str] = []

    def flush() -> None:
        chunk = "\n".join(current_lines).strip()
        if not chunk:
            return
        open_tags = _open_tags_in(chunk)
        if open_tags:
            chunk += _close_open_tags(open_tags)
        parts.append(chunk)
        current_lines.clear()

    for para in paragraphs:
        prefix = _reopen_tags(carry_open) if carry_open else ""
        line = (prefix + para) if not current_lines and carry_open else para
        carry_open = []
        piece_len = len(line) + 1

        if current_len + piece_len > max_len:
            open_at_flush = _open_tags_in("\n".join(current_lines))
            carry_open = open_at_flush
            flush()
            current_len = 0
            if len(line) > max_len:
                while len(line) > max_len:
                    cut = line[:max_len]
                    split_pos = cut.rfind(" ")
                    if split_pos < max_len // 4:
                        split_pos = max_len
                    chunk_piece = line[:split_pos].strip()
                    open_tags = _open_tags_in(chunk_piece)
                    if open_tags:
                        chunk_piece += _close_open_tags(open_tags)
                        carry_open = open_tags
                    parts.append(chunk_piece)
                    line = (_reopen_tags(carry_open) + line[split_pos:]).strip()
                    carry_open = []

        current_lines.append(line)
        current_len += piece_len

    if current_lines:
        flush()

    return [p for p in parts if p.strip()]


# ─── Histórico comprimido ─────────────────────────────────────────────────────

ConversationHistory = List[dict]


def _compress_history(history: Optional[ConversationHistory]) -> ConversationHistory:
    if not history:
        return []
    compressed = []
    for msg in history[-4:]:
        role    = msg.get("role", "")
        content = (msg.get("content") or "").strip()
        if role == "assistant" and len(content) > 300:
            content = content[:297] + "…"
        compressed.append({"role": role, "content": content})
    return compressed


# ─── Chamada à API ────────────────────────────────────────────────────────────

def _build_headers() -> dict[str, str]:
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY não definida.")
    return {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }


def _call_groq(model: str, messages: list, headers: dict) -> httpx.Response:
    return httpx.post(
        GROQ_API_URL,
        headers=headers,
        json={
            "model":             model,
            "messages":          messages,
            "temperature":       0.85,
            "max_tokens":        450,
            "top_p":             0.9,
            "frequency_penalty": 0.2,
        },
        timeout=HTTP_TIMEOUT,
    )


def _extract_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        msg = str(payload.get("error", {}).get("message", "")).strip()
        return f" — {msg}" if msg else ""
    except ValueError:
        raw = response.text.strip()
        return f" — {raw[:200]}" if raw else ""


def _extract_content(data: dict) -> str:
    try:
        raw = data["choices"][0]["message"]["content"]
        return (raw or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Resposta inválida da Groq API.") from exc


async def generate_anime_reply(
    user_text: str,
    history: Optional[ConversationHistory] = None,
) -> str:
    import time

    user_text = (user_text or "").strip()
    if not user_text:
        return NO_REPLY_TOKEN

    intent         = _detect_intent(user_text)
    system_content = SYSTEM_PROMPT + _intent_suffix(intent)
    compressed     = _compress_history(history)

    # Injeta dados AniList quando é pergunta de info sobre anime específico
    if intent == "info":
        try:
            from services import anilist_client as _al
            m = _ANIME_QUESTION_RE.search(user_text)
            if m:
                info = await _al.buscar_anilist(m.group(1).strip(), timeout=4.0)
                if info:
                    system_content += "\n\n" + _al.format_for_prompt(info)
        except Exception as e:
            print(f"[Akira][AniList] {e}")

    messages = [
        {"role": "system", "content": system_content},
        *compressed,
        {"role": "user", "content": user_text},
    ]

    headers    = _build_headers()
    last_error = ""

    for model in [MODEL_PRIMARY, MODEL_FALLBACK]:
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = _call_groq(model, messages, headers)
            except httpx.TimeoutException:
                last_error = f"timeout em {model}"
                break
            except httpx.RequestError as exc:
                raise RuntimeError(f"Erro de conexão: {exc}") from exc

            if response.status_code == 429:
                if attempt < _MAX_RETRIES:
                    wait = min(float(response.headers.get("retry-after", _RETRY_DELAY_S)), 10.0)
                    print(f"[Akira] 429 em {model}, aguardando {wait:.1f}s")
                    time.sleep(wait)
                    continue
                last_error = f"429 em {model} após {_MAX_RETRIES} tentativas"
                break

            if response.is_error:
                detail = _extract_error_detail(response)
                raise RuntimeError(f"Groq API {response.status_code}{detail}")

            content = _extract_content(response.json())
            if not content or NO_REPLY_TOKEN in content:
                return NO_REPLY_TOKEN
            return sanitize_telegram_html(content)

    raise RuntimeError(f"Quota esgotada. Último erro: {last_error}")
