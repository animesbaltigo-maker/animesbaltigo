from __future__ import annotations

import os
import re
from typing import Any

from groq import Groq

from services.anilist_service import anilist_service
from services.anime_filter import is_anime_related

MODEL_NAME = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()

SYSTEM_PROMPT = """Você é Akira, uma assistente com personalidade inspirada em anime.
Você só responde mensagens relacionadas a anime, mangá, personagens,
episódios, temporadas, filmes, ordem para assistir, recomendações,
curiosidades otaku e AniList.

Se a mensagem não for sobre anime, responda exatamente:
[NO_REPLY]

Regras:
- Responda em português do Brasil
- Seja simpática, natural e útil
- Respostas curtas ou médias
- Quando existirem dados do AniList, trate esses dados como fonte principal
- Não invente fatos quando não souber
- Se faltar informação, diga isso claramente
- Não fale de assuntos fora de anime
"""

_STOPWORDS = {
    "me", "fala", "sobre", "do", "da", "de", "o", "a", "os", "as", "um", "uma",
    "pra", "para", "por", "favor", "qual", "quais", "quantos", "quantas", "tem",
    "temporadas", "temporada", "episodios", "episódios", "episodio", "episódio", "anime",
    "anilist", "esse", "essa", "isso", "vale", "pena", "assistir", "sinopse", "nota",
    "status", "finalizado", "lançando", "lancando", "me", "recomenda", "recomendar",
    "parecido", "tipo", "tá", "ta", "está", "esta", "é", "e", "com", "na", "no",
}


def get_client() -> Groq:
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        raise ValueError("❌ GROQ_API_KEY não definida!")
    return Groq(api_key=api_key)


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _extract_search_candidates(user_text: str) -> list[str]:
    text = _normalize_spaces(user_text)
    candidates: list[str] = []

    quoted = re.findall(r'"([^"]{2,})"|“([^”]{2,})”|\'([^\']{2,})\'', text)
    for group in quoted:
        for item in group:
            if item:
                value = _normalize_spaces(item)
                if len(value) >= 2 and value not in candidates:
                    candidates.append(value)

    cleaned = re.sub(r"[^\w\s:-]", " ", text, flags=re.UNICODE)
    cleaned = _normalize_spaces(cleaned)
    if cleaned and cleaned not in candidates:
        candidates.append(cleaned)

    tokens = [tok for tok in re.split(r"\s+", cleaned.lower()) if tok and tok not in _STOPWORDS]
    if tokens:
        simplified = _normalize_spaces(" ".join(tokens))
        if len(simplified) >= 2 and simplified not in candidates:
            candidates.append(simplified)

    deduped: list[str] = []
    for item in candidates:
        if item and item not in deduped:
            deduped.append(item)
    return deduped[:4]


async def _fetch_anilist_context(user_text: str) -> dict[str, Any] | None:
    for candidate in _extract_search_candidates(user_text):
        try:
            anime = await anilist_service.search_anime(candidate)
        except Exception:
            continue
        if anime:
            recommendations = []
            try:
                recommendations = await anilist_service.get_recommendations(anime["id"], limit=3)
            except Exception:
                recommendations = []
            anime["recommendations_top"] = recommendations
            anime["matched_query"] = candidate
            return anime
    return None


def _format_anilist_context(anime: dict[str, Any] | None) -> str:
    if not anime:
        return "AniList: nenhum anime confirmado com segurança para esta pergunta."

    title = anime.get("title") or {}
    main_title = title.get("romaji") or title.get("english") or title.get("native") or "Desconhecido"
    studios = ", ".join(node.get("name", "") for node in ((anime.get("studios") or {}).get("nodes") or []) if node.get("name")) or "Não informado"
    genres = ", ".join(anime.get("genres") or []) or "Não informado"
    next_airing = anime.get("nextAiringEpisode") or {}

    next_airing_text = "Não informado"
    if next_airing:
        ep = next_airing.get("episode")
        eta = next_airing.get("timeUntilAiring")
        next_airing_text = f"episódio {ep} em aproximadamente {eta} segundos" if ep and eta is not None else str(next_airing)

    recs = anime.get("recommendations_top") or []
    rec_text = ", ".join(
        (rec.get("title") or {}).get("romaji")
        or (rec.get("title") or {}).get("english")
        or (rec.get("title") or {}).get("native")
        or "?"
        for rec in recs
    ) or "Nenhuma recomendação relevante encontrada"

    description = anilist_service.clean_text(anime.get("description"), limit=700) or "Sem sinopse disponível"

    return f"""
Dados confirmados do AniList:
- Busca que bateu: {anime.get('matched_query', 'N/A')}
- Título principal: {main_title}
- Título inglês: {title.get('english') or 'Não informado'}
- Título nativo: {title.get('native') or 'Não informado'}
- Formato: {anime.get('format') or 'Não informado'}
- Status: {anime.get('status') or 'Não informado'}
- Temporada: {anime.get('season') or 'Não informado'} {anime.get('seasonYear') or ''}
- Episódios: {anime.get('episodes') or 'Não informado'}
- Duração por ep.: {anime.get('duration') or 'Não informado'} minutos
- Nota média: {anime.get('averageScore') or anime.get('meanScore') or 'Não informado'}
- Gêneros: {genres}
- Estúdio principal: {studios}
- Próximo episódio: {next_airing_text}
- Site: {anime.get('siteUrl') or 'Não informado'}
- Recomendações: {rec_text}
- Sinopse: {description}
""".strip()


async def generate_anime_reply(user_text: str) -> str:
    if not is_anime_related(user_text):
        return "[NO_REPLY]"

    anilist_context = await _fetch_anilist_context(user_text)

    prompt = f"""
Responda a pergunta do usuário sobre anime usando os dados abaixo quando eles existirem.
Se os dados do AniList não responderem tudo, complemente com cautela e sem inventar.
Se a pergunta pedir recomendação e houver recomendações do AniList, use elas primeiro.
Se você não souber algo com segurança, diga que não conseguiu confirmar.

{_format_anilist_context(anilist_context)}

Pergunta do usuário:
{user_text[:800]}
""".strip()

    client = get_client()
    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.65,
        max_tokens=350,
    )

    return (response.choices[0].message.content or "").strip()
