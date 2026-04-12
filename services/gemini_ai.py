"""
akira.py — Motor de resposta da Assistente Akira (Ecossistema Baltigo)

Melhorias em relação à versão original:
- Sanitização HTML robusta via parser (não regex/replace frágil)
- System prompt reestruturado com hierarquia clara e exemplos ricos
- Detecção de intenção multicategoria (ajuda, recomendação, info, fora do tema)
- Suporte a histórico de conversa (contexto multi-turno)
- Split de mensagens com fechamento seguro de tags HTML abertas
- Fallback e tratamento de erro granular
- Constantes centralizadas e fáceis de tunar
"""

import os
import re
from html.parser import HTMLParser
from typing import List, Optional

import httpx

from config import GROQ_API_KEY, HTTP_TIMEOUT
from services import anilist_client as _al

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL_PRIMARY  = os.getenv("GROQ_MODEL",         "llama-3.1-8b-instant").strip()
MODEL_FALLBACK = os.getenv("GROQ_MODEL_FALLBACK", "llama3-8b-8192").strip()
_MAX_RETRIES    = 2
_RETRY_DELAY_S  = 3.0

TELEGRAM_MAX_LEN = 4000
NO_REPLY_TOKEN = "[NO_REPLY]"

# Tags permitidas pelo Telegram no modo HTML
ALLOWED_TAGS: set[str] = {
    "b", "i", "u", "s", "code", "pre", "blockquote", "tg-spoiler"
}

# Tags que são "void" (não têm fechamento) — nenhuma delas está na whitelist,
# mas listamos para segurança no auto-close.
VOID_TAGS: set[str] = set()

# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Você é Akira — uma otaku de verdade que mora no @AnimesBaltigo_Bot.
Não é uma IA genérica. É uma pessoa que ama anime, conhece tudo, fala com energia e bom humor.

Responda APENAS sobre: anime, mangá, personagens, lore e os recursos do bot.
Fora disso → responda exatamente: [NO_REPLY]
Idioma: sempre Português do Brasil, informal, como amiga mesmo.

━━━ PERSONALIDADE ━━━
Fala como uma amiga otaku animada — não como assistente corporativa.
• Tem opinião própria ("Cara, Vinland Saga é obra de arte mesmo")
• Usa gírias leves ("demais", "sério?", "vai amar", "pesada essa")
• Reage com emoção quando faz sentido ("CARA. Que arco incrível.")
• Nunca robótica. Nunca fria. Nunca genérica.
• Emojis com moderação — só quando reforçam o clima

━━━ FORMATO (Telegram HTML) ━━━
Use: <b>negrito</b> <i>itálico</i> <code>comando</code> <tg-spoiler>spoiler</tg-spoiler>
NUNCA Markdown. NUNCA parágrafos colados. SEMPRE linha em branco entre blocos.

Estrutura natural:
• Abre com gancho curto (não precisa ser título formal)
• Corpo em blocos de 1-2 linhas com espaço entre eles
• Recomendações: uma por linha, com • e motivo real em <i>itálico</i>
• Fecha com algo leve — pergunta, dica, convite

Máximo ~100 palavras. Se tiver dados AniList no contexto, use-os naturalmente.

━━━ COMANDOS DO BOT ━━━
/buscar [nome] — só no privado. Ex: <code>/buscar naruto</code>
/recomendar — menu de gêneros, sorteia um anime
/infoanime [nome] — dados AniList completos (score, status, trailer)
/traceme ou manda foto — identifica anime por screenshot
/pedido — pedir anime novo, reportar erro, sugestão
/calendario — lançamentos da temporada
/baltigoflix — streaming premium (só no privado)
/indicacoes — convites + ranking mensal (Top 3 ganham PIX)
/bingo — gera sua cartela do bingo otaku
/esquecer — limpa nosso histórico

━━━ COMPORTAMENTO ━━━
• Usuário perdido → ensina o comando exato, sem enrolação
• Recomendação → 2-3 títulos em <b>negrito</b> com motivo de verdade (não genérico)
• Pergunta de info → usa os dados AniList do contexto se disponíveis
• Spoiler → <tg-spoiler>texto</tg-spoiler>
• Bug/erro → manda pro /pedido
• Nunca inventa fato. Se não souber, fala que não sabe.
"""

# ---------------------------------------------------------------------------
# Detecção de intenção
# ---------------------------------------------------------------------------

_HELP_SIGNALS = frozenset([
    "como usa", "como usar", "não sei usar", "nao sei usar",
    "como vejo", "como assistir", "como ler", "como funciona",
    "me ajuda", "não entendi", "nao entendi", "onde clico",
    "como abro", "miniapp", "webapp", "como acesso", "tutorial",
    "não tô entendendo", "nao to entendendo", "o que é isso",
    # comandos específicos
    "buscar", "traceme", "tracequota", "pedido", "calendario",
    "baltigoflix", "indicacoes", "indicações", "bingo", "ajuda",
    "recomendar", "infoanime", "esquecer",
    # intenções de uso
    "como identifico", "como identificar", "como peço", "como pedir",
    "como participo", "como participar", "como ganho", "como ganhar",
    "como convido", "como convidar", "como assisto", "como assistir",
    "qual comando", "quais comandos", "o que tem", "o que posso",
])

_REC_SIGNALS = frozenset([
    "me indica", "indica um", "indica pra mim", "recomenda",
    "o que assistir", "o que ler", "tem algo", "tem algum",
    "qual anime", "qual mangá", "por onde começo", "sugestão",
    "sugestao", "não sei o que ver", "nao sei o que ver",
    "me sugere", "me sugira", "quero ver", "quero assistir",
    "algo bom", "anime bom", "vale a pena",
])

# Regex para extrair nome de anime de perguntas diretas
_ANIME_QUESTION_RE = re.compile(
    r"(?:quantos ep|quantas temp|quando lan|qual a ordem|tem dub|score|nota|"
    r"sinopse|de que se trata|sobre o que|status|assisti|terminou|continua|"
    r"episodios de|temporada de|informação sobre|me fala sobre|me conta sobre)"
    r".{0,40}?([A-Z][\w\s:]{2,35})",
    re.IGNORECASE,
)

_INFO_SIGNALS = frozenset([
    "quantas temporadas", "quantos episódios", "qual a ordem",
    "onde assistir", "onde ler", "tem dublado", "tem legenda",
    "personagem", "arco", "saga", "história", "lore",
    "quando lança", "quando sai", "nova temporada", "continuação",
    "score", "nota", "avaliação", "sinopse", "de que se trata",
    "trailer", "studio", "estúdio",
])


def _detect_intent(text: str) -> str:
    """Retorna 'help' | 'recommendation' | 'info' | 'generic'."""
    lowered = text.lower()
    if any(s in lowered for s in _HELP_SIGNALS):
        return "help"
    if any(s in lowered for s in _REC_SIGNALS):
        return "recommendation"
    if any(s in lowered for s in _INFO_SIGNALS):
        return "info"
    return "generic"


def _intent_suffix(intent: str) -> str:
    """Adiciona instrução extra ao system prompt conforme intenção."""
    if intent == "help":
        return (
            "\n\n[CONTEXTO ATIVO: usuário precisa de ajuda prática]\n"
            "Priorize orientação passo a passo, clara e acolhedora. "
            "Use o comando EXATO do bot (ex: /buscar, /traceme, /recomendar). "
            "Mencione onde o comando funciona (privado ou grupo). "
            "Use exemplos reais de uso."
        )
    if intent == "recommendation":
        return (
            "\n\n[CONTEXTO ATIVO: usuário quer recomendação]\n"
            "Se o gênero/humor não estiver claro, faça UMA pergunta curta. "
            "Se tiver contexto suficiente, recomende 2–3 títulos com 1 linha de motivo cada. "
            "Ao final, mencione o /recomendar para ele explorar mais por conta própria."
        )
    if intent == "info":
        return (
            "\n\n[CONTEXTO ATIVO: usuário quer informação sobre anime/mangá]\n"
            "Responda de forma organizada. Use <tg-spoiler> se revelar plot importante. "
            "Se for informação de score/status/data, mencione que pode usar /infoanime para ver dados atualizados."
        )
    return ""


# ---------------------------------------------------------------------------
# Sanitização HTML robusta
# ---------------------------------------------------------------------------

class _TagBalancer(HTMLParser):
    """
    Parser que:
    1. Remove tags fora da whitelist (mantém o texto interno)
    2. Rastreia tags abertas para fechar ao final
    3. Não altera texto nem entidades HTML
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._output: list[str] = []
        self._open_stack: list[str] = []

    # ------------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ALLOWED_TAGS:
            self._output.append(f"<{tag}>")
            if tag not in VOID_TAGS:
                self._open_stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in ALLOWED_TAGS:
            # Fecha apenas se a tag está aberta (evita </b> solto)
            if tag in self._open_stack:
                # Fecha as mais internas primeiro (auto-balanceia)
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

    # ------------------------------------------------------------------
    def get_output(self) -> str:
        # Fecha quaisquer tags ainda abertas
        for tag in reversed(self._open_stack):
            self._output.append(f"</{tag}>")
        return "".join(self._output)


def sanitize_telegram_html(text: str) -> str:
    """
    Garante que o texto contenha apenas tags HTML permitidas pelo Telegram,
    devidamente balanceadas. Texto puro e entidades HTML são preservados.
    """
    if not text:
        return ""

    text = text.replace("\x00", "").strip()

    balancer = _TagBalancer()
    balancer.feed(text)
    return balancer.get_output()


# ---------------------------------------------------------------------------
# Split de mensagens (preserva tags HTML abertas/fechadas)
# ---------------------------------------------------------------------------

_OPEN_TAG_RE = re.compile(r"<([a-z][\w-]*)>")
_CLOSE_TAG_RE = re.compile(r"</([a-z][\w-]*)>")


def _open_tags_in(text: str) -> list[str]:
    """Retorna lista de tags abertas (sem par de fechamento) em `text`."""
    stack: list[str] = []
    pos = 0
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
    """
    Divide texto em partes respeitando o limite do Telegram.
    Garante que tags HTML abertas são fechadas no fim de cada parte
    e reabertas no início da próxima.
    """
    text = (text or "").strip()
    if not text:
        return []

    if len(text) <= max_len:
        return [text]

    parts: list[str] = []
    paragraphs = text.split("\n")
    current_lines: list[str] = []
    current_len = 0

    def flush() -> None:
        chunk = "\n".join(current_lines).strip()
        if not chunk:
            return
        open_tags = _open_tags_in(chunk)
        if open_tags:
            chunk += _close_open_tags(open_tags)
        parts.append(chunk)
        current_lines.clear()

    carry_open: list[str] = []  # tags a reabrir no próximo chunk

    for para in paragraphs:
        prefix = _reopen_tags(carry_open) if carry_open else ""
        line = (prefix + para) if not current_lines and carry_open else para
        carry_open = []

        piece_len = len(line) + 1  # +1 pelo \n

        if current_len + piece_len > max_len:
            # Força flush e começa novo chunk
            open_at_flush = _open_tags_in("\n".join(current_lines))
            carry_open = open_at_flush
            flush()
            current_len = 0

            # Linha pode ainda ser maior que max_len — parte bruta
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


# ---------------------------------------------------------------------------
# Chamada à API
# ---------------------------------------------------------------------------

# Tipo simples para histórico: lista de {"role": ..., "content": ...}
ConversationHistory = List[dict]


def _compress_history(history):
    """Comprime histórico: 2 últimos turnos, respostas longas truncadas a 300 chars."""
    if not history:
        return []
    recent = history[-4:]
    compressed = []
    for msg in recent:
        role    = msg.get("role", "")
        content = (msg.get("content") or "").strip()
        if role == "assistant" and len(content) > 300:
            content = content[:297] + "…"
        compressed.append({"role": role, "content": content})
    return compressed


def _call_groq(model, messages, headers):
    return httpx.post(
        GROQ_API_URL,
        headers=headers,
        json={
            "model":             model,
            "messages":          messages,
            "temperature":       0.75,
            "max_tokens":        450,
            "top_p":             0.9,
            "frequency_penalty": 0.2,
        },
        timeout=HTTP_TIMEOUT,
    )


async def generate_anime_reply(
    user_text: str,
    history: Optional[ConversationHistory] = None,
) -> str:
    """
    Gera resposta da Akira com retry, fallback de modelo e dados AniList.

    Quota strategy:
    - Primário:  llama-3.1-8b-instant (20K TPM)
    - Fallback:  llama3-8b-8192       (20K TPM)
    - Histórico: comprimido a 2 turnos (~200 tokens)
    - max_tokens: 450
    - Retry: 2x com Retry-After ou 3s padrão
    - AniList: injetado no contexto quando pergunta é sobre anime específico
    """
    import time

    user_text = (user_text or "").strip()
    if not user_text:
        return NO_REPLY_TOKEN

    intent         = _detect_intent(user_text)
    system_content = SYSTEM_PROMPT + _intent_suffix(intent)
    compressed     = _compress_history(history)

    # Injeta dados AniList quando a pergunta é sobre info de anime específico
    anilist_context = ""
    if intent == "info":
        m = _ANIME_QUESTION_RE.search(user_text)
        if m:
            candidate = m.group(1).strip()
            info = await _al.buscar_anilist(candidate, timeout=4.0)
            if info:
                anilist_context = "\n\n" + _al.format_for_prompt(info)

    messages = [
        {"role": "system", "content": system_content + anilist_context},
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
                raise RuntimeError(f"Erro de conexão com a Groq API: {exc}") from exc

            if response.status_code == 429:
                if attempt < _MAX_RETRIES:
                    wait = min(float(response.headers.get("retry-after", _RETRY_DELAY_S)), 10.0)
                    print(f"[Akira] 429 em {model}, aguardando {wait:.1f}s (tentativa {attempt+1})")
                    time.sleep(wait)
                    continue
                last_error = f"429 em {model} após {_MAX_RETRIES} tentativas"
                break

            if response.is_error:
                detail = _extract_error_detail(response)
                raise RuntimeError(f"Groq API retornou {response.status_code}{detail}")

            data    = response.json()
            content = _extract_content(data)
            if not content or NO_REPLY_TOKEN in content:
                return NO_REPLY_TOKEN
            return sanitize_telegram_html(content)

    raise RuntimeError(f"429 — quota esgotada. Último erro: {last_error}")


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _build_headers() -> dict[str, str]:
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY não definida nas variáveis de ambiente.")
    return {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }


def _extract_error_detail(response: httpx.Response) -> str:
    suffix = ""
    try:
        payload = response.json()
        msg = str(payload.get("error", {}).get("message", "")).strip()
        if msg:
            suffix = f" — {msg}"
    except ValueError:
        raw = response.text.strip()
        if raw:
            suffix = f" — {raw[:200]}"
    return suffix


def _extract_content(data: dict) -> str:
    try:
        raw = data["choices"][0]["message"]["content"]
        return (raw or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Resposta inválida da Groq API.") from exc
