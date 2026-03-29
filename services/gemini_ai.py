import os
from groq import Groq

MODEL_NAME = "llama-3.3-70b-versatile"

SYSTEM_PROMPT = """Você é uma assistente com personalidade inspirada em anime.
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

def get_client():
    api_key = os.getenv("GROQ_API_KEY")

    if not api_key:
        raise ValueError("❌ GROQ_API_KEY não definida!")

    return Groq(api_key=api_key)

def generate_anime_reply(user_text: str) -> str:
    client = get_client()

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text[:800]},
        ],
        temperature=0.8,
        max_tokens=250,
    )

    return (response.choices[0].message.content or "").strip()
