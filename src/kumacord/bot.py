from __future__ import annotations

import logging

import aiohttp
import discord
from discord.ext import commands, tasks

from kumacord.config import Settings
from kumacord.embed_builder import EmbedBuilder
from kumacord.models import DashboardSnapshot, MonitorStatus
from kumacord.providers import AuthenticationError, KumaDataProvider, ProviderError
from kumacord.state import BotState, StateStore

LOGGER = logging.getLogger(__name__)


class KumaCordBot(commands.Bot):
    """Discord runtime that keeps one persistent dashboard message in sync."""

    def __init__(
        self,
        settings: Settings,
        provider: KumaDataProvider,
        state_store: StateStore,
        session: aiohttp.ClientSession,
    ) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.provider = provider
        self.state_store = state_store
        self.state = BotState()
        self.session = session
        self.embed_builder = EmbedBuilder(language=settings.language)
        self.last_snapshot: DashboardSnapshot | None = None
        self.current_interval = settings.refresh_interval
        self.last_branding_signature: tuple[str, str | None] | None = None

    async def setup_hook(self) -> None:
        self.state = self.state_store.load()
        self.status_sync_loop.change_interval(seconds=self.current_interval)
        self.status_sync_loop.start()

    async def close(self) -> None:
        self.status_sync_loop.cancel()
        await super().close()

    async def on_ready(self) -> None:
        LOGGER.info("Connected as %s (%s)", self.user, self.user.id if self.user else "n/a")

    async def _resolve_channel(self) -> discord.TextChannel:
        channel = self.get_channel(self.settings.discord_channel_id)
        if channel is None:
            channel = await self.fetch_channel(self.settings.discord_channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("DISCORD_CHANNEL_ID must be a guild text channel")
        return channel

    async def _resolve_message(self, channel: discord.TextChannel) -> discord.Message | None:
        if not self.state.message_id:
            return None
        try:
            return await channel.fetch_message(self.state.message_id)
        except discord.NotFound:
            LOGGER.warning("Saved message %s no longer exists; creating a new one", self.state.message_id)
            self.state.message_id = None
            self.state_store.save(self.state)
            return None

    async def _sync_bot_branding(self, snapshot: DashboardSnapshot) -> None:
        """Try to align bot profile name/avatar with dashboard branding.

        Discord applies strict limits for profile edits; failures are non-fatal.
        """

        if not self.user:
            return

        signature = (snapshot.title[:32], snapshot.icon_url)
        if signature == self.last_branding_signature:
            return

        avatar_data = None
        if snapshot.icon_url:
            try:
                async with self.session.get(snapshot.icon_url, timeout=20) as response:
                    response.raise_for_status()
                    avatar_data = await response.read()
            except aiohttp.ClientError:
                LOGGER.debug("Could not download icon for bot avatar", exc_info=True)

        try:
            await self.user.edit(username=signature[0], avatar=avatar_data)
            self.last_branding_signature = signature
            LOGGER.info("Updated bot profile branding to '%s'", signature[0])
        except discord.HTTPException:
            LOGGER.warning("Bot profile branding update failed (likely rate-limited)", exc_info=True)

    async def _update_presence(self, snapshot: DashboardSnapshot) -> None:
        """Update custom presence: 🟢 X online | 🔴 Y offline."""

        online = sum(1 for m in snapshot.monitors if m.status == MonitorStatus.UP)
        offline = sum(1 for m in snapshot.monitors if m.status == MonitorStatus.DOWN)
        presence_text = f"🟢 {online} online | 🔴 {offline} offline"
        activity = discord.CustomActivity(name=presence_text)
        await self.change_presence(activity=activity)

    @staticmethod
    def _has_significant_change(previous: DashboardSnapshot | None, current: DashboardSnapshot) -> bool:
        if previous is None:
            return True

        previous_map = {m.monitor_id: m for m in previous.monitors}
        current_map = {m.monitor_id: m for m in current.monitors}
        if previous_map.keys() != current_map.keys():
            return True

        for monitor_id, current_monitor in current_map.items():
            old_monitor = previous_map[monitor_id]
            if old_monitor.status != current_monitor.status:
                return True
            old_ping = old_monitor.ping_ms or 0
            new_ping = current_monitor.ping_ms or 0
            if abs(old_ping - new_ping) >= 10:
                return True

        return False

    def _raise_backoff(self) -> None:
        if self.current_interval < 60:
            self.current_interval = 60
            self.status_sync_loop.change_interval(seconds=self.current_interval)
            LOGGER.warning("Rate limit detected; backoff set to %ss", self.current_interval)

    @tasks.loop(seconds=30)
    async def status_sync_loop(self) -> None:
        if not self.is_ready():
            return

        channel = await self._resolve_channel()

        try:
            snapshot = await self.provider.fetch_snapshot()
        except AuthenticationError as exc:
            LOGGER.error("Provider authentication error: %s", exc)
            return
        except ProviderError as exc:
            LOGGER.warning("Provider fetch failed (recoverable): %s", exc)
            return

        await self._sync_bot_branding(snapshot)
        await self._update_presence(snapshot)

        if not self._has_significant_change(self.last_snapshot, snapshot):
            LOGGER.debug("No significant monitor change detected; skipping edit")
            return

        render = self.embed_builder.build(snapshot)
        message = await self._resolve_message(channel)

        try:
            if message is None:
                message = await channel.send(embeds=render.embeds)
                self.state.message_id = message.id
                self.state_store.save(self.state)
                LOGGER.info("Created new dashboard message: %s", message.id)
            else:
                await message.edit(embeds=render.embeds)
                LOGGER.debug("Dashboard updated (%s embeds)", len(render.embeds))
        except discord.HTTPException as exc:
            if exc.status == 429:
                self._raise_backoff()
                return
            raise

        self.last_snapshot = snapshot

    @status_sync_loop.before_loop
    async def before_loop(self) -> None:
        await self.wait_until_ready()
