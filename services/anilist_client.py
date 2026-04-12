"""
services/anilist_client.py — Cliente AniList para a Akira

Busca dados enriquecidos de anime (score, episódios, status, gêneros, ano)
para a Akira usar ao responder perguntas específicas.

Usado por: services/gemini_ai.py (injetado no contexto antes de chamar o LLM)
"""

import asyncio
from functools import lru_cache
from typing import Optional

import httpx

ANILIST_API = "https://graphql.anilist.co"

_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

_QUERY = """
query ($search: String) {
  Page(perPage: 1) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      siteUrl
      title { romaji english native }
      status
      averageScore
      popularity
      episodes
      season
      seasonYear
      genres
      description(asHtml: false)
    }
  }
}
"""

_STATUS_PT = {
    "FINISHED":        "Finalizado",
    "RELEASING":       "Em lançamento",
    "NOT_YET_RELEASED":"Em breve",
    "CANCELLED":       "Cancelado",
    "HIATUS":          "Em hiato",
}

_SEASON_PT = {
    "WINTER": "inverno",
    "SPRING": "primavera",
    "SUMMER": "verão",
    "FALL":   "outono",
}


def _pick_title(media: dict) -> str:
    t = media.get("title") or {}
    return t.get("english") or t.get("romaji") or t.get("native") or "?"


def _short_synopsis(desc: str, max_chars: int = 200) -> str:
    """Remove tags HTML simples e trunca a sinopse."""
    if not desc:
        return ""
    import re
    desc = re.sub(r"<[^>]+>", "", desc).strip()
    desc = re.sub(r"\s+", " ", desc)
    if len(desc) > max_chars:
        desc = desc[:max_chars].rsplit(" ", 1)[0] + "…"
    return desc


def _format_anilist_data(media: dict) -> dict:
    """Transforma a resposta bruta em um dict limpo para injetar no prompt."""
    genres = media.get("genres") or []
    season = media.get("season") or ""
    season_year = media.get("seasonYear") or ""
    season_str = f"{_SEASON_PT.get(season, season).capitalize()} {season_year}".strip() if season else str(season_year or "N/A")

    return {
        "titulo":    _pick_title(media),
        "score":     media.get("averageScore") or "N/A",
        "status":    _STATUS_PT.get(media.get("status", ""), media.get("status") or "N/A"),
        "episodios": media.get("episodes") or "N/A",
        "temporada": season_str,
        "generos":   ", ".join(genres[:5]) if genres else "N/A",
        "sinopse":   _short_synopsis(media.get("description") or ""),
        "url":       media.get("siteUrl") or "",
        "id":        media.get("id"),
    }


async def buscar_anilist(titulo: str, timeout: float = 5.0) -> Optional[dict]:
    """
    Busca um anime na AniList e retorna dados formatados.
    Retorna None se não encontrar ou der erro.
    """
    titulo = (titulo or "").strip()
    if not titulo:
        return None

    try:
        async with httpx.AsyncClient(timeout=timeout, headers=_HEADERS) as client:
            resp = await client.post(
                ANILIST_API,
                json={"query": _QUERY, "variables": {"search": titulo}},
            )
            resp.raise_for_status()
            data = resp.json()

        media_list = (
            ((data or {}).get("data") or {})
            .get("Page", {})
            .get("media") or []
        )
        if not media_list:
            return None

        return _format_anilist_data(media_list[0])

    except Exception as e:
        print(f"[AniList] erro buscando '{titulo}': {e}")
        return None


def format_for_prompt(info: dict) -> str:
    """
    Formata os dados da AniList como bloco de contexto para injetar no prompt.
    Fica antes da mensagem do usuário para o LLM usar na resposta.
    """
    parts = [f"[DADOS ANILIST — {info['titulo']}]"]
    if info["score"] != "N/A":
        parts.append(f"Score: {info['score']}/100")
    if info["episodios"] != "N/A":
        parts.append(f"Episódios: {info['episodios']}")
    if info["status"] != "N/A":
        parts.append(f"Status: {info['status']}")
    if info["temporada"] != "N/A":
        parts.append(f"Temporada: {info['temporada']}")
    if info["generos"] != "N/A":
        parts.append(f"Gêneros: {info['generos']}")
    if info["sinopse"]:
        parts.append(f"Sinopse: {info['sinopse']}")
    parts.append("[/DADOS ANILIST]")
    return "\n".join(parts)
