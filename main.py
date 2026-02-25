from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Support direct `python main.py` execution with src-layout without requiring editable install.
REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import aiohttp
from dotenv import load_dotenv

from kumacord.bot import KumaCordBot
from kumacord.config import load_settings
from kumacord.providers import build_provider
from kumacord.state import StateStore


async def run() -> None:
    load_dotenv()
    settings = load_settings()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    async with aiohttp.ClientSession() as session:
        provider = build_provider(
            statuspage_url=settings.kuma_statuspage_url,
            api_url=settings.kuma_api_url,
            username=settings.kuma_username,
            password=settings.kuma_password,
            session=session,
        )
        state_store = StateStore(settings.state_file)
        bot = KumaCordBot(settings=settings, provider=provider, state_store=state_store, session=session)
        await bot.start(settings.discord_bot_token)


if __name__ == "__main__":
    asyncio.run(run())
