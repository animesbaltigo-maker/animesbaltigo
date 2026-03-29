from __future__ import annotations

import html
import re
from typing import Any

from core.http_client import get_http_client

ANILIST_API_URL = "https://graphql.anilist.co"

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


class AniListService:
    async def _post(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        client = await get_http_client()

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        response = await client.post(
            ANILIST_API_URL,
            json={"query": query, "variables": variables or {}},
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()

        if payload.get("errors"):
            raise RuntimeError(f"AniList GraphQL error: {payload['errors']}")

        return payload.get("data", {})

    async def search_anime(self, search: str) -> dict[str, Any] | None:
        query = """
        query ($search: String) {
          Media(search: $search, type: ANIME) {
            id
            siteUrl
            title {
              romaji
              english
              native
            }
            description(asHtml: false)
            episodes
            duration
            status
            format
            season
            seasonYear
            averageScore
            meanScore
            genres
            synonyms
            source
            bannerImage
            coverImage {
              extraLarge
              large
            }
            nextAiringEpisode {
              episode
              timeUntilAiring
            }
            studios(isMain: true) {
              nodes {
                name
              }
            }
          }
        }
        """
        data = await self._post(query, {"search": search})
        media = data.get("Media")
        if media:
            media["description"] = self.clean_text(media.get("description"))
        return media

    async def get_recommendations(self, media_id: int, limit: int = 3) -> list[dict[str, Any]]:
        query = """
        query ($mediaId: Int, $perPage: Int) {
          Media(id: $mediaId, type: ANIME) {
            recommendations(sort: RATING_DESC, perPage: $perPage) {
              nodes {
                mediaRecommendation {
                  id
                  siteUrl
                  title {
                    romaji
                    english
                    native
                  }
                  averageScore
                  format
                  episodes
                }
              }
            }
          }
        }
        """
        data = await self._post(query, {"mediaId": media_id, "perPage": limit})
        nodes = (((data.get("Media") or {}).get("recommendations") or {}).get("nodes") or [])
        results: list[dict[str, Any]] = []
        for node in nodes:
            media = (node or {}).get("mediaRecommendation") or {}
            if media:
                results.append(media)
        return results

    @staticmethod
    def clean_text(text: str | None, limit: int = 900) -> str:
        if not text:
            return ""
        text = html.unescape(text)
        text = _TAG_RE.sub(" ", text)
        text = _WHITESPACE_RE.sub(" ", text).strip()
        if len(text) > limit:
            return text[: limit - 3].rstrip() + "..."
        return text


anilist_service = AniListService()
