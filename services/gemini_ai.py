import httpx

from config import GROQ_API_KEY, HTTP_TIMEOUT


GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL_NAME = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()

SYSTEM_PROMPT = """Voce e uma assistente com personalidade inspirada em anime.
Voce so responde mensagens relacionadas a anime, manga, personagens,
episodios, temporadas, filmes, ordem para assistir, recomendacoes
e curiosidades otaku.

Se a mensagem nao for sobre anime, responda exatamente:
[NO_REPLY]

Regras:
- Responda em portugues do Brasil
- Seja simpatica, natural e util
- Respostas curtas ou medias
- Nao invente fatos quando nao souber
- Nao fale de assuntos fora de anime
"""


def _headers() -> dict[str, str]:
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY nao definida.")

    return {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }


def generate_anime_reply(user_text: str) -> str:
    response = httpx.post(
        GROQ_API_URL,
        headers=_headers(),
        json={
            "model": MODEL_NAME,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text[:800]},
            ],
            "temperature": 0.8,
            "max_tokens": 250,
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
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Resposta invalida da Groq API.") from exc
