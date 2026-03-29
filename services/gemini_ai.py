import os
from google import genai

MODEL_NAME = "gemini-1.5-flash"

SYSTEM_PROMPT = """
Você é uma assistente com personalidade inspirada em anime.
Você só responde mensagens relacionadas a anime, mangá, personagens,
episódios, temporadas, filmes, ordem para assistir, recomendações
e curiosidades otaku.

Se a mensagem não for sobre anime, responda exatamente:
[NO_REPLY]

Regras:
- Responda em português do Brasil
- Seja simpática, natural e útil
- Respostas curtas ou médias
- Não invente fatos quando não souber
- Não fale de assuntos fora de anime
"""

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

def generate_anime_reply(user_text: str) -> str:
    prompt = f"{SYSTEM_PROMPT}\n\nMensagem do grupo:\n{user_text}"

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
    )

    return (response.text or "").strip()
