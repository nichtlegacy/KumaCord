from __future__ import annotations

import asyncio
import logging
import os
import platform
import sys
from pathlib import Path

# Ensure src/ is on sys.path when running from repo root without installation.
ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Configure event loop policy for Windows compatibility.
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

RUNNING_IN_DOCKER = os.getenv("RUNNING_IN_DOCKER", "false").lower() == "true"

import aiohttp
from dotenv import load_dotenv

from kumacord.bot import KumaCordBot
from kumacord.config import load_settings
from kumacord.providers import build_provider
from kumacord.state import StateStore


class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\x1b[36m",
        logging.INFO: "\x1b[32m",
        logging.WARNING: "\x1b[33m",
        logging.ERROR: "\x1b[31m",
        logging.CRITICAL: "\x1b[35m",
    }
    RESET = "\x1b[0m"

    def __init__(self, fmt: str, datefmt: str, use_color: bool) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        levelname = record.levelname
        if self.use_color:
            color = self.COLORS.get(record.levelno)
            if color:
                record.levelname = f"{color}{levelname}{self.RESET}"
        try:
            return super().format(record)
        finally:
            record.levelname = levelname


def configure_logging(use_color: bool) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        ColorFormatter(
            fmt="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            use_color=use_color,
        )
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])


async def run() -> None:
    if not RUNNING_IN_DOCKER:
        load_dotenv()
    settings = load_settings()

    configure_logging(use_color=sys.stderr.isatty())

    async with aiohttp.ClientSession() as session:
        provider = build_provider(
            statuspage_url=settings.kuma_statuspage_url,
            api_url=settings.kuma_api_url,
            api_key=settings.kuma_api_key,
            session=session,
        )
        state_store = StateStore(settings.state_file)
        bot = KumaCordBot(
            settings=settings,
            provider=provider,
            state_store=state_store,
            session=session,
        )
        await bot.start(settings.discord_bot_token)


if __name__ == "__main__":
    asyncio.run(run())
