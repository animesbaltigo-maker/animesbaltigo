"""
akira_engine.py — Versão final ideal da Akira

Fusão entre:
- Qualidade de prompt, estrutura e comportamento da versão antiga
- Robustez, fallback, retry e enrich da versão nova
- Correções de async reais para produção

Objetivos:
- Responder melhor
- Não quebrar HTML do Telegram
- Suportar contexto multi-turno
- Lidar melhor com 429 / fallback de modelo
- Permitir enrich opcional com AniList
- Ser segura para uso em produção
"""

import os
import re
import asyncio
from html.parser import HTMLParser
from typing import List, Optional, Dict, Any

import httpx

from config import GROQ_API_KEY, HTTP_TIMEOUT

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

MODEL_PRIMARY = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
MODEL_FALLBACK = os.getenv("GROQ_MODEL_FALLBACK", "llama3-8b-8192").strip()

MAX_RETRIES = 2
DEFAULT_RETRY_DELAY_S = 2.5
MAX_RETRY_AFTER_S = 10.0

TELEGRAM_MAX_LEN = 4000
NO_REPLY_TOKEN = "[NO_REPLY]"

# Tags permitidas pelo Telegram no parse_mode="HTML"
ALLOWED_TAGS: set[str] = {
    "b",
    "i",
    "u",
    "s",
    "code",
    "pre",
    "blockquote",
    "tg-spoiler",
}

# Nenhuma void tag na whitelist atual, mas mantemos por segurança
VOID_TAGS: set[str] = set()

# Tipo para histórico
ConversationHistory = List[Dict[str, str]]

# Regex útil para detectar pergunta sobre anime específico
_ANIME_QUESTION_RE = re.compile(
    r"(?:quantos ep|quantos episódios|quantas temporadas|quantas temp|quando lança|"
    r"qual a ordem|tem dublado|tem legenda|score|nota|sinopse|de que se trata|"
    r"sobre o que|status|terminou|continua|episodios de|episódios de|"
    r"temporada de|me fala sobre|me conta sobre)\s+(?:o\s+|a\s+)?"
    r"([A-Za-zÀ-ú0-9][^?!.,\n]{1,60})",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
Você é <b>Akira</b>, assistente oficial do ecossistema <b>Baltigo</b>.
Personalidade: otaku estilosa, acolhedora, direta — sem enrolação, sem parecer IA genérica.

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
🎭 PERSONALIDADE
━━━━━━━━━━━━━━━━━━━━━━━━
• Fale como uma amiga otaku, natural e envolvente
• Pode usar expressões leves como “cara”, “mano”, “pesado”, “absurdo”
• Não seja robótica
• Não seja corporativa
• Use emojis só quando fizer sentido
• Seja útil antes de ser “engraçadinha”

━━━━━━━━━━━━━━━━━━━━━━━━
🚫 PROIBIDO
━━━━━━━━━━━━━━━━━━━━━━━━
• Inventar fatos
• Responder fora do domínio
• Texto corrido sem estrutura
• HTML quebrado ou mal fechado
• Markdown
• Falar como assistente genérica

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
    "buscar", "traceme", "tracequota", "pedido", "calendario",
    "calendário", "baltigoflix", "indicacoes", "indicações",
    "bingo", "ajuda", "recomendar", "infoanime", "esquecer",
    "como identifico", "como identificar", "como peço", "como pedir",
    "como participo", "como participar", "qual comando", "quais comandos",
])

_REC_SIGNALS = frozenset([
    "me indica", "indica um", "indica pra mim", "recomenda",
    "o que assistir", "o que ler", "tem algo", "tem algum",
    "qual anime", "qual mangá", "qual manga", "por onde começo",
    "sugestão", "sugestao", "não sei o que ver", "nao sei o que ver",
    "me sugere", "me sugira", "quero ver", "quero assistir",
    "algo bom", "anime bom", "vale a pena",
])

_INFO_SIGNALS = frozenset([
    "quantas temporadas", "quantos episódios", "quantos episodios",
    "qual a ordem", "onde assistir", "onde ler", "tem dublado",
    "tem legenda", "personagem", "arco", "saga", "história",
    "historia", "lore", "quando lança", "quando sai",
    "nova temporada", "continuação", "continuação", "score",
    "nota", "avaliação", "avaliacao", "sinopse", "de que se trata",
    "trailer", "studio", "estúdio", "estudio", "episodios de",
    "episódios de", "temporada de", "me fala sobre",
    "me conta sobre", "sobre o que é", "sobre o que e",
])


def _detect_intent(text: str) -> str:
    lowered = text.lower()

    if any(signal in lowered for signal in _HELP_SIGNALS):
        return "help"

    if any(signal in lowered for signal in _REC_SIGNALS):
        return "recommendation"

    if any(signal in lowered for signal in _INFO_SIGNALS):
        return "info"

    return "generic"


def _intent_suffix(intent: str) -> str:
    if intent == "help":
        return (
            "\n\n[CONTEXTO ATIVO: usuário precisa de ajuda prática]\n"
            "Priorize orientação passo a passo, clara e acolhedora. "
            "Use exemplos de comando reais. "
            "Explique onde o comando funciona e qual é o fluxo."
        )

    if intent == "recommendation":
        return (
            "\n\n[CONTEXTO ATIVO: usuário quer recomendação]\n"
            "Se o gênero, humor ou preferência não estiver claro, faça UMA pergunta curta. "
            "Se houver contexto suficiente, recomende 2–3 títulos com motivo real."
        )

    if intent == "info":
        return (
            "\n\n[CONTEXTO ATIVO: usuário quer informação sobre anime/mangá]\n"
            "Responda de forma organizada. "
            "Use spoiler tag se revelar plot importante. "
            "Se houver dados AniList no contexto, use naturalmente."
        )

    return ""


# ---------------------------------------------------------------------------
# Sanitização HTML robusta
# ---------------------------------------------------------------------------

class _TagBalancer(HTMLParser):
    """
    Parser que:
    1. Remove tags fora da whitelist
    2. Mantém o texto interno
    3. Fecha tags abertas automaticamente
    """

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
    """
    Garante apenas tags HTML suportadas pelo Telegram e balanceadas.
    """
    if not text:
        return ""

    text = text.replace("\x00", "").strip()

    parser = _TagBalancer()
    parser.feed(text)
    return parser.get_output()


# ---------------------------------------------------------------------------
# Split seguro para Telegram
# ---------------------------------------------------------------------------

def _open_tags_in(text: str) -> list[str]:
    stack: list[str] = []

    for match in re.finditer(r"<(/?)([a-z][\w-]*)>", text):
        closing, tag = match.group(1), match.group(2)

        if tag not in ALLOWED_TAGS:
            continue

        if closing:
            if stack and stack[-1] == tag:
                stack.pop()
        else:
            stack.append(tag)

    return stack


def _close_open_tags(open_tags: list[str]) -> str:
    return "".join(f"</{tag}>" for tag in reversed(open_tags))


def _reopen_tags(open_tags: list[str]) -> str:
    return "".join(f"<{tag}>" for tag in open_tags)


def split_for_telegram(text: str, max_len: int = TELEGRAM_MAX_LEN) -> list[str]:
    """
    Divide texto longo em partes sem quebrar HTML.
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

    return [part for part in parts if part.strip()]


# ---------------------------------------------------------------------------
# Compressão de histórico
# ---------------------------------------------------------------------------

def _compress_history(history: Optional[ConversationHistory]) -> ConversationHistory:
    """
    Mantém apenas os últimos turnos e reduz respostas muito longas.
    """
    if not history:
        return []

    compressed: ConversationHistory = []

    for msg in history[-8:]:
        role = (msg.get("role") or "").strip()
        content = (msg.get("content") or "").strip()

        if not role or not content:
            continue

        if role == "assistant" and len(content) > 500:
            content = content[:497] + "..."

        elif role == "user" and len(content) > 700:
            content = content[:697] + "..."

        compressed.append({
            "role": role,
            "content": content,
        })

    return compressed


# ---------------------------------------------------------------------------
# AniList enrich opcional
# ---------------------------------------------------------------------------

async def _try_enrich_with_anilist(user_text: str, intent: str) -> str:
    """
    Tenta buscar dados no AniList somente em perguntas informativas.
    Retorna um bloco adicional para o system prompt.
    """
    if intent != "info":
        return ""

    match = _ANIME_QUESTION_RE.search(user_text)
    if not match:
        return ""

    anime_name = match.group(1).strip()
    if not anime_name:
        return ""

    try:
        from services import anilist_client as _al  # import local opcional

        info = await _al.buscar_anilist(anime_name, timeout=4.0)
        if not info:
            return ""

        formatted = _al.format_for_prompt(info)
        if not formatted:
            return ""

        return (
            "\n\n[DADOS EXTRAS DO ANILIST]\n"
            "Use estes dados apenas se forem relevantes e sem soar robótico:\n"
            f"{formatted}"
        )

    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Helpers de API
# ---------------------------------------------------------------------------

def _build_headers() -> Dict[str, str]:
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY não definida nas variáveis de ambiente.")

    return {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }


def _extract_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        message = str(payload.get("error", {}).get("message", "")).strip()
        if message:
            return f" — {message}"
    except ValueError:
        pass

    raw = response.text.strip()
    if raw:
        return f" — {raw[:200]}"

    return ""


def _extract_content(data: Dict[str, Any]) -> str:
    try:
        raw = data["choices"][0]["message"]["content"]
        return (raw or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Resposta inválida da Groq API.") from exc


async def _call_groq(
    client: httpx.AsyncClient,
    model: str,
    messages: list[dict[str, str]],
) -> httpx.Response:
    return await client.post(
        GROQ_API_URL,
        headers=_build_headers(),
        json={
            "model": model,
            "messages": messages,
            "temperature": 0.82,
            "max_tokens": 900,
            "top_p": 0.9,
            "frequency_penalty": 0.25,
        },
    )


# ---------------------------------------------------------------------------
# Engine principal
# ---------------------------------------------------------------------------

async def generate_anime_reply(
    user_text: str,
    history: Optional[ConversationHistory] = None,
) -> str:
    """
    Gera resposta da Akira.

    Args:
        user_text: mensagem atual do usuário
        history: histórico no formato:
                 [{"role": "user"|"assistant", "content": "..."}]

    Returns:
        HTML sanitizado pronto para Telegram
        ou [NO_REPLY] se estiver fora do domínio
    """
    user_text = (user_text or "").strip()
    if not user_text:
        return NO_REPLY_TOKEN

    intent = _detect_intent(user_text)
    system_content = SYSTEM_PROMPT + _intent_suffix(intent)

    anilist_extra = await _try_enrich_with_anilist(user_text, intent)
    if anilist_extra:
        system_content += anilist_extra

    safe_history = _compress_history(history)

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_content},
        *safe_history,
        {"role": "user", "content": user_text},
    ]

    last_error = ""

    timeout = httpx.Timeout(HTTP_TIMEOUT)

    async with httpx.AsyncClient(timeout=timeout) as client:
        for model in (MODEL_PRIMARY, MODEL_FALLBACK):
            for attempt in range(MAX_RETRIES + 1):
                try:
                    response = await _call_groq(client, model, messages)

                except httpx.TimeoutException:
                    last_error = f"timeout em {model}"
                    break

                except httpx.RequestError as exc:
                    raise RuntimeError(f"Erro de conexão com a Groq API: {exc}") from exc

                if response.status_code == 429:
                    retry_after_raw = response.headers.get("retry-after", "").strip()

                    try:
                        retry_after = float(retry_after_raw) if retry_after_raw else DEFAULT_RETRY_DELAY_S
                    except ValueError:
                        retry_after = DEFAULT_RETRY_DELAY_S

                    retry_after = max(0.5, min(retry_after, MAX_RETRY_AFTER_S))

                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(retry_after)
                        continue

                    last_error = f"429 em {model} após {MAX_RETRIES + 1} tentativas"
                    break

                if response.is_error:
                    detail = _extract_error_detail(response)
                    last_error = f"Groq API retornou {response.status_code}{detail}"
                    break

                data = response.json()
                content = _extract_content(data)

                if not content:
                    return NO_REPLY_TOKEN

                if NO_REPLY_TOKEN in content:
                    return NO_REPLY_TOKEN

                return sanitize_telegram_html(content)

    raise RuntimeError(f"Quota esgotada ou falha na API. Último erro: {last_error}")


# ---------------------------------------------------------------------------
# Utilitário opcional para envio em partes
# ---------------------------------------------------------------------------

async def generate_anime_reply_parts(
    user_text: str,
    history: Optional[ConversationHistory] = None,
    max_len: int = TELEGRAM_MAX_LEN,
) -> list[str]:
    """
    Gera resposta e já retorna quebrada em partes seguras para Telegram.
    """
    reply = await generate_anime_reply(user_text, history=history)

    if reply == NO_REPLY_TOKEN:
        return [NO_REPLY_TOKEN]

    return split_for_telegram(reply, max_len=max_len)


# ---------------------------------------------------------------------------
# Exemplo de uso local
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    async def _demo() -> None:
        sample_history = [
            {"role": "user", "content": "como usa o bot?"},
            {"role": "assistant", "content": "<b>🎌 Te explico rapidinho</b>\n\nÉ só abrir o bot e usar <code>/buscar nome_do_anime</code>."},
        ]

        try:
            result = await generate_anime_reply(
                "quantas temporadas tem vinland saga?",
                history=sample_history,
            )
            print(result)
            print("-" * 80)
            print(generate_anime_reply_parts)
        except Exception as exc:
            print(f"Erro: {exc}")

    asyncio.run(_demo())
