from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx

from config import UPSTREAM_PROXY_URL
from core.video_download_queue import HEADERS, _build_download_transport, _probe_direct_download
from services.animefire_client import get_episode_player


async def main() -> None:
    parser = argparse.ArgumentParser(description="Testa AnimeFire + Lightspeed com a mesma rota do bot.")
    parser.add_argument("anime_id", help="Slug do anime, com ou sem -todos-os-episodios")
    parser.add_argument("episode", help="Numero do episodio")
    parser.add_argument("--quality", default="HD", help="Qualidade desejada: HD, SD ou FULLHD")
    args = parser.parse_args()

    player = await get_episode_player(args.anime_id, args.episode, preferred_quality=args.quality)
    video_url = player.get("video") or ""
    if not video_url:
        raise SystemExit("AnimeFire nao retornou link de video.")

    print(f"proxy={'on' if UPSTREAM_PROXY_URL else 'off'}")
    print(f"quality={player.get('quality')} server={player.get('server')}")
    print(f"url={video_url.split('?', 1)[0]}")

    timeout = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=HEADERS,
        transport=_build_download_transport(),
    ) as client:
        probe = await _probe_direct_download(client, video_url)
        print(f"range={probe['range']} total={probe['total']}")


if __name__ == "__main__":
    asyncio.run(main())
