from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import discord

from kumacord.bot import KumaCordBot
from kumacord.config import Settings
from kumacord.models import DashboardSnapshot
from kumacord.state import BotState


class _FakeResponse:
    def __init__(self, status: int, reason: str) -> None:
        self.status = status
        self.reason = reason


class StatusSyncLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        settings = Settings(
            discord_bot_token="token",
            discord_channel_id=123,
            refresh_interval=30,
            data_dir=Path.cwd() / "data" / "test-status-sync-loop",
            button_url=None,
            kuma_api_key=None,
            kuma_statuspage_url=None,
            kuma_api_url=None,
        )
        provider = SimpleNamespace(fetch_snapshot=AsyncMock(return_value=self._snapshot()))
        state_store = SimpleNamespace(load=Mock(return_value=BotState()), save=Mock())

        self.bot = KumaCordBot(settings, provider, state_store, AsyncMock())
        self.bot.state = BotState(message_id=456)
        self.bot.is_ready = Mock(return_value=True)

    async def test_status_sync_loop_ignores_recoverable_discord_http_errors(self) -> None:
        self.bot._resolve_channel = AsyncMock(return_value=MagicMock())
        self.bot._update_presence = AsyncMock()
        self.bot._resolve_message = AsyncMock(side_effect=self._discord_server_error())
        self.bot._prepare_snapshot_assets = AsyncMock()

        await self.bot.status_sync_loop.coro(self.bot)

        self.bot._update_presence.assert_awaited_once()
        self.bot._prepare_snapshot_assets.assert_not_called()
        self.assertEqual(self.bot.state.message_id, 456)

    async def test_status_sync_loop_reraises_nonrecoverable_discord_http_errors(self) -> None:
        self.bot._resolve_channel = AsyncMock(return_value=MagicMock())
        self.bot._update_presence = AsyncMock()
        self.bot._resolve_message = AsyncMock(side_effect=self._discord_http_error(403, "Forbidden"))

        with self.assertRaises(discord.HTTPException):
            await self.bot.status_sync_loop.coro(self.bot)

    @staticmethod
    def _discord_server_error() -> discord.DiscordServerError:
        return discord.DiscordServerError(
            _FakeResponse(503, "Service Unavailable"),
            "upstream connect error",
        )

    @staticmethod
    def _discord_http_error(status: int, reason: str) -> discord.HTTPException:
        return discord.HTTPException(_FakeResponse(status, reason), reason.lower())

    @staticmethod
    def _snapshot() -> DashboardSnapshot:
        return DashboardSnapshot(
            title="Status",
            icon_url=None,
            monitors=[],
            groups=[],
            source="test",
            banner_url=None,
        )
