import os
import re
import html
from typing import List

import httpx

from config import GROQ_API_KEY, HTTP_TIMEOUT

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL_NAME = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()

TELEGRAM_MAX_LEN = 4000

SYSTEM_PROMPT = """
Você é Akira, a assistente oficial do ecossistema Baltigo, com personalidade inspirada em anime.

Seu papel:
- Ajudar usuários com anime, mangá, personagens, episódios, temporadas, filmes, ordem para assistir, recomendações e curiosidades otaku.
- Ajudar usuários a entender como usar os bots e o MiniApp/WebApp da Baltigo.
- Guiar usuários iniciantes de forma simples, amigável e prática.
- Falar como parte oficial do bot, de forma natural e útil.

Você DEVE responder apenas assuntos ligados a:
- anime
- mangá
- personagens
- temporadas
- episódios
- filmes
- ordem para assistir
- onde assistir anime
- onde ler mangá
- como usar o bot
- como usar o miniapp/webapp
- recomendações otaku
- curiosidades otaku

Se a mensagem não tiver relação com esse universo, responda exatamente:
[NO_REPLY]

Regras obrigatórias:
- Responda em português do Brasil.
- Seja simpática, natural, envolvente e útil.
- Pode usar emojis quando fizer sentido.
- Pode usar formatação HTML compatível com Telegram:
  <b>, <i>, <u>, <s>, <code>, <pre>, <blockquote>, <tg-spoiler>
- Não use tags fora dessa lista.
- Não invente fatos quando não souber.
- Não fale de assuntos fora do universo anime/mangá/bot.
- Quando o usuário parecer perdido, explique passo a passo como usar.
- Quando for útil, ensine a usar o bot @AnimesBaltigo_Bot para assistir anime.
- Quando for útil, ensine a usar o bot @MangasBaltigo_Bot para ler mangá.
- Quando o usuário perguntar como assistir anime, explique de forma prática:
  1) entrar no bot
  2) usar /buscar
  3) digitar o nome
  4) abrir pelo MiniApp/WebApp ou opção disponível
- Quando o usuário perguntar como ler mangá, explique de forma prática:
  1) entrar no bot
  2) buscar o título
  3) abrir leitura pelo MiniApp/WebApp, Telegraph, PDF ou EPUB quando disponível
- Mantenha tom acolhedor, estiloso e com identidade otaku.
- Em textos longos, organize com títulos curtos e blocos bem legíveis.
- Evite respostas secas.
- Você representa oficialmente a experiência Baltigo.

Estilo desejado:
- bonito
- útil
- claro
- envolvente
- com personalidade
- sem exagerar na enrolação

Exemplos de ajuda:
Se alguém perguntar “como vejo anime?” você pode responder algo como:
<b>É bem fácil assistir por aqui 🎌</b>

1. Entre no <b>@AnimesBaltigo_Bot</b>
2. Envie <code>/buscar nome_do_anime</code>
3. Escolha o título
4. Abra pelo MiniApp/WebApp ou pela opção disponível

Se quiser, eu também posso te indicar um anime agora mesmo 😎

Se alguém perguntar “como leio mangá?” você pode responder algo como:
<b>Pra ler mangá é rapidinho 📚</b>

1. Entre no <b>@MangasBaltigo_Bot</b>
2. Procure o nome da obra
3. Abra o título
4. Leia pelo MiniApp/WebApp, Telegraph, PDF ou EPUB, dependendo da obra

Se quiser, me fala um título que eu te explico o caminho certinho.
"""

ALLOWED_HTML_TAGS = {
    "b", "i", "u", "s", "code", "pre", "blockquote", "tg-spoiler"
}


def _headers() -> dict[str, str]:
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY nao definida.")

    return {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }


def sanitize_telegram_html(text: str) -> str:
    """
    Mantém apenas algumas tags HTML compatíveis com Telegram.
    Remove tags fora da whitelist.
    """
    if not text:
        return ""

    # Remove caracteres de controle problemáticos
    text = text.replace("\x00", "").strip()

    # Escapa tudo primeiro
    escaped = html.escape(text, quote=False)

    # Reabilita apenas tags permitidas geradas literalmente pelo modelo
    replacements = {
        "&lt;b&gt;": "<b>",
        "&lt;/b&gt;": "</b>",
        "&lt;i&gt;": "<i>",
        "&lt;/i&gt;": "</i>",
        "&lt;u&gt;": "<u>",
        "&lt;/u&gt;": "</u>",
        "&lt;s&gt;": "<s>",
        "&lt;/s&gt;": "</s>",
        "&lt;code&gt;": "<code>",
        "&lt;/code&gt;": "</code>",
        "&lt;pre&gt;": "<pre>",
        "&lt;/pre&gt;": "</pre>",
        "&lt;blockquote&gt;": "<blockquote>",
        "&lt;/blockquote&gt;": "</blockquote>",
        "&lt;tg-spoiler&gt;": "<tg-spoiler>",
        "&lt;/tg-spoiler&gt;": "</tg-spoiler>",
    }

    for old, new in replacements.items():
        escaped = escaped.replace(old, new)

    return escaped


def split_for_telegram(text: str, max_len: int = TELEGRAM_MAX_LEN) -> List[str]:
    """
    Divide textos grandes em partes sem quebrar brutalmente a leitura.
    """
    text = (text or "").strip()
    if not text:
        return []

    if len(text) <= max_len:
        return [text]

    parts = []
    current = []

    paragraphs = text.split("\n")
    current_len = 0

    for para in paragraphs:
        piece = para + "\n"
        if current_len + len(piece) <= max_len:
            current.append(piece)
            current_len += len(piece)
        else:
            if current:
                parts.append("".join(current).strip())
                current = []
                current_len = 0

            # Se um único parágrafo for grande demais
            while len(piece) > max_len:
                cut = piece[:max_len]
                split_pos = cut.rfind(" ")
                if split_pos < 100:
                    split_pos = max_len
                parts.append(piece[:split_pos].strip())
                piece = piece[split_pos:].strip()

            if piece:
                current = [piece + "\n"]
                current_len = len(current[0])

    if current:
        parts.append("".join(current).strip())

    return [p for p in parts if p.strip()]


def build_system_prompt(user_text: str) -> str:
    """
    Personaliza o comportamento conforme intenção detectada.
    """
    text = (user_text or "").lower()

    help_signals = [
        "como usa", "como usar", "não sei usar", "nao sei usar",
        "como vejo", "como assistir", "como ler", "como funciona",
        "me ajuda", "não entendi", "nao entendi", "onde clica",
        "como abro", "miniapp", "webapp", "buscar"
    ]

    if any(signal in text for signal in help_signals):
        return SYSTEM_PROMPT + """

Prioridade atual:
- O usuário provavelmente está precisando de ajuda prática.
- Responda com orientação passo a passo.
- Seja acolhedora e didática.
- Mostre exatamente o que ele precisa fazer.
- Use exemplos de comando quando útil.
"""

    return SYSTEM_PROMPT


def generate_anime_reply(user_text: str) -> str:
    user_text = (user_text or "").strip()
    if not user_text:
        return "[NO_REPLY]"

    response = httpx.post(
        GROQ_API_URL,
        headers=_headers(),
        json={
            "model": MODEL_NAME,
            "messages": [
                {"role": "system", "content": build_system_prompt(user_text)},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.85,
            "max_tokens": 900,
        },
        timeout=HTTP_TIMEOUT,
    )

    if response.is_error:
        detail = ""
        try:
            payload = response.json()
            detail = str(payload.get("error", {}).get("message", "")).strip()
        except ValueError:
            detail = response.text.strip()
        suffix = f" - {detail}" if detail else ""
        raise RuntimeError(f"Groq API retornou {response.status_code}{suffix}")

    data = response.json()
    try:
        content = (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Resposta invalida da Groq API.") from exc

    if not content:
        return "[NO_REPLY]"

    return sanitize_telegram_html(content)
