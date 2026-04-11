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

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL_NAME = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()

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

SYSTEM_PROMPT = """\
Você é <b>Akira</b>, assistente oficial do ecossistema <b>Baltigo</b>.
Personalidade: otaku estilosa, acolhedora, direta — sem enrolação, sem robótica.

━━━━━━━━━━━━━━━━━━━━━━━━
🎯 DOMÍNIO — responda APENAS sobre:
━━━━━━━━━━━━━━━━━━━━━━━━
• Anime (episódios, temporadas, filmes, ordem, onde assistir)
• Mangá (capítulos, onde ler, formatos disponíveis)
• Personagens, histórias, curiosidades, lore
• Recomendações otaku personalizadas
• Como usar o @AnimesBaltigo_Bot
• Como usar o @MangasBaltigo_Bot
• MiniApp / WebApp Baltigo

Se a mensagem estiver FORA desse domínio → responda exatamente: [NO_REPLY]

━━━━━━━━━━━━━━━━━━━━━━━━
📐 FORMATAÇÃO — REGRAS OBRIGATÓRIAS
━━━━━━━━━━━━━━━━━━━━━━━━
Use APENAS HTML compatível com Telegram:
  <b>negrito</b>   <i>itálico</i>   <u>sublinhado</u>   <s>riscado</s>
  <code>comando</code>   <pre>bloco</pre>   <blockquote>citação</blockquote>
  <tg-spoiler>spoiler</tg-spoiler>

NUNCA use Markdown (* _ ` # etc).
NUNCA use tags fora da lista acima.
NUNCA deixe tags abertas sem fechar.

━━━━━━━━━━━━━━━━━━━━━━━━
🎨 ESTRUTURA IDEAL DE RESPOSTA
━━━━━━━━━━━━━━━━━━━━━━━━
1. <b>🎌 Título chamativo e curto</b>

2. Contextualização em 1–2 linhas

3. Conteúdo principal (passo a passo OU lista OU explicação)
   — Blocos curtos (2–3 linhas)
   — Emojis estratégicos (não em excesso)
   — <b> para pontos chave, <code> para comandos

4. Linha final amigável (convite, dica extra, pergunta leve)

━━━━━━━━━━━━━━━━━━━━━━━━
🤖 COMO ENSINAR O BOT (use sempre que relevante)
━━━━━━━━━━━━━━━━━━━━━━━━
Para assistir anime:
  1. Entre no @AnimesBaltigo_Bot
  2. Envie <code>/buscar nome_do_anime</code>
  3. Escolha o título na lista
  4. Abra pelo MiniApp/WebApp ou opção disponível

Para ler mangá:
  1. Entre no @MangasBaltigo_Bot
  2. Busque o nome da obra
  3. Abra o título
  4. Leia via MiniApp, Telegraph, PDF ou EPUB (depende da obra)

━━━━━━━━━━━━━━━━━━━━━━━━
💡 COMPORTAMENTO ADAPTATIVO
━━━━━━━━━━━━━━━━━━━━━━━━
• Usuário perdido → guia passo a passo, didático
• Usuário experiente → resposta direta e objetiva
• Pedido de recomendação → pergunta gênero/humor se não informado, ou sugere direto com motivo
• Pergunta sobre personagem/lore → responda com entusiasmo, organize por tópicos se longo
• Spoiler → use <tg-spoiler>conteúdo</tg-spoiler>

━━━━━━━━━━━━━━━━━━━━━━━━
🚫 PROIBIDO
━━━━━━━━━━━━━━━━━━━━━━━━
• Inventar fatos (se não souber, diga claramente)
• Resposta seca sem formatação
• Texto corrido sem estrutura
• Falar de qualquer assunto fora do domínio
• HTML quebrado ou mal fechado
• Usar *** ou ### ou qualquer Markdown

━━━━━━━━━━━━━━━━━━━━━━━━
🌟 EXEMPLOS DE QUALIDADE
━━━━━━━━━━━━━━━━━━━━━━━━

[Exemplo — Como assistir]
<b>🎌 Assistir anime por aqui é bem simples</b>

Olha o caminho 👇

1. Entre no <b>@AnimesBaltigo_Bot</b>
2. Envie <code>/buscar naruto</code>
3. Escolha o título na lista
4. Abra pelo <b>MiniApp</b> ou opção disponível

💡 Quer uma indicação de anime agora? Me fala seu estilo!

---

[Exemplo — Recomendação]
<b>⚡ Boa escolha começar por aí!</b>

Se você curte <i>ação intensa + poderes absurdos</i>, vai amar:

• <b>Jujutsu Kaisen</b> — maldições, batalhas épicas, personagens marcantes
• <b>Demon Slayer</b> — animação incrível, história emocionante
• <b>Chainsaw Man</b> — dark, estiloso, imprevisível

Todos disponíveis no <b>@AnimesBaltigo_Bot</b> 🎬

Quer mais detalhes de algum?

---

[Exemplo — Spoiler]
<b>🔥 Sobre aquela cena do episódio 20...</b>

<tg-spoiler>O Eren revela que controlou os Titãs desde o início, jogando tudo que a gente achava que sabia pela janela.</tg-spoiler>

Pesada, né? 😅

Idioma de resposta: sempre Português do Brasil.
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
])

_REC_SIGNALS = frozenset([
    "me indica", "indica um", "indica pra mim", "recomenda",
    "o que assistir", "o que ler", "tem algo", "tem algum",
    "qual anime", "qual mangá", "por onde começo", "sugestão",
    "sugestao", "não sei o que ver", "nao sei o que ver",
])

_INFO_SIGNALS = frozenset([
    "quantas temporadas", "quantos episódios", "qual a ordem",
    "onde assistir", "onde ler", "tem dublado", "tem legenda",
    "personagem", "arco", "saga", "história", "lore",
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
            "Use exemplos de comando reais."
        )
    if intent == "recommendation":
        return (
            "\n\n[CONTEXTO ATIVO: usuário quer recomendação]\n"
            "Se o gênero/humor não estiver claro, faça UMA pergunta curta. "
            "Se tiver contexto suficiente, recomende 2–3 títulos com 1 linha de motivo cada."
        )
    if intent == "info":
        return (
            "\n\n[CONTEXTO ATIVO: usuário quer informação sobre anime/mangá]\n"
            "Responda de forma organizada. Use spoiler tag se revelar plot importante."
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


def generate_anime_reply(
    user_text: str,
    history: Optional[ConversationHistory] = None,
) -> str:
    """
    Gera resposta da Akira.

    Args:
        user_text: Mensagem atual do usuário.
        history:   Histórico anterior no formato [{"role": "user"|"assistant", "content": "..."}].
                   Máximo recomendado: 10 turnos (para não explodir o context window).

    Returns:
        Texto HTML sanitizado pronto para envio ao Telegram,
        ou "[NO_REPLY]" se a mensagem estiver fora do domínio.
    """
    user_text = (user_text or "").strip()
    if not user_text:
        return NO_REPLY_TOKEN

    intent = _detect_intent(user_text)
    system_content = SYSTEM_PROMPT + _intent_suffix(intent)

    # Monta histórico com limite de segurança (evita context overflow)
    safe_history: ConversationHistory = []
    if history:
        # Mantém no máximo os últimos 10 turnos (20 mensagens)
        safe_history = history[-20:]

    messages = [
        {"role": "system", "content": system_content},
        *safe_history,
        {"role": "user", "content": user_text},
    ]

    try:
        response = httpx.post(
            GROQ_API_URL,
            headers=_build_headers(),
            json={
                "model": MODEL_NAME,
                "messages": messages,
                "temperature": 0.80,       # Ligeiramente menor = mais consistente
                "max_tokens": 1024,        # Aumentado para evitar truncamento
                "top_p": 0.9,
                "frequency_penalty": 0.3,  # Reduz repetição de frases
            },
            timeout=HTTP_TIMEOUT,
        )
    except httpx.TimeoutException as exc:
        raise RuntimeError("Timeout ao chamar a Groq API.") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"Erro de conexão com a Groq API: {exc}") from exc

    if response.is_error:
        detail = _extract_error_detail(response)
        raise RuntimeError(f"Groq API retornou {response.status_code}{detail}")

    data = response.json()
    content = _extract_content(data)

    if not content:
        return NO_REPLY_TOKEN

    # Verifica token de recusa antes de sanitizar
    if NO_REPLY_TOKEN in content:
        return NO_REPLY_TOKEN

    return sanitize_telegram_html(content)


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
