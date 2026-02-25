from __future__ import annotations

import hashlib
import logging
import random
from dataclasses import replace
from pathlib import Path
import time
from urllib.parse import urlparse

import aiohttp
import discord
from discord.ext import commands, tasks

from kumacord.config import Settings
from kumacord.embed_builder import EmbedBuilder
from kumacord.models import DashboardSnapshot, MonitorStatus
from kumacord.providers import AuthenticationError, KumaDataProvider, ProviderError
from kumacord.state import BotState, StateStore

LOGGER = logging.getLogger(__name__)
ASSET_PREFIX = "kumacord-"
ASSET_DIR = "branding"
ASSET_TTL_SECONDS = 6 * 60 * 60


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
        self.embed_builder = EmbedBuilder()
        self.button_url = settings.button_url
        self.last_snapshot: DashboardSnapshot | None = None
        self.refresh_interval = settings.refresh_interval
        self._presence_items: list[str] = []
        self._presence_index = 0
        self._last_presence_change = 0.0
        self._presence_interval = 180
        self._asset_dir = self.settings.data_dir / ASSET_DIR

    @staticmethod
    def _asset_ext(url: str) -> str:
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
            return suffix
        return ".png"

    def _asset_filename(self, label: str, url: str) -> str:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        return f"{ASSET_PREFIX}{label}-{digest}{self._asset_ext(url)}"

    async def _download_asset(self, url: str, destination: Path) -> bool:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with self.session.get(url, timeout=20) as response:
                response.raise_for_status()
                data = await response.read()
        except aiohttp.ClientError:
            LOGGER.warning("Failed to download branding asset: %s", url)
            return False
        try:
            destination.write_bytes(data)
        except OSError:
            LOGGER.warning("Failed to write branding asset: %s", destination)
            return False
        return True

    async def _resolve_asset(
        self, label: str, url: str | None, message: discord.Message | None
    ) -> tuple[str | None, discord.File | None, str | None]:
        if not url:
            return None, None, None

        filename = self._asset_filename(label, url)
        path = self._asset_dir / filename
        if not path.exists() or (time.time() - path.stat().st_mtime) > ASSET_TTL_SECONDS:
            ok = await self._download_asset(url, path)
            if not ok:
                return None, None, None

        if message and any(att.filename == filename for att in message.attachments):
            return f"attachment://{filename}", None, filename

        return f"attachment://{filename}", discord.File(path, filename=filename), filename

    async def _prepare_snapshot_assets(
        self, snapshot: DashboardSnapshot, message: discord.Message | None
    ) -> tuple[DashboardSnapshot, list[discord.File], list[discord.Attachment] | None]:
        files_by_name: dict[str, discord.File] = {}
        keep_filenames: set[str] = set()

        icon_source = snapshot.icon_url
        banner_source = snapshot.banner_url

        icon_url, icon_file, icon_name = await self._resolve_asset("icon", icon_source, message)
        if icon_file:
            if icon_name and icon_name not in files_by_name:
                files_by_name[icon_name] = icon_file
        if icon_name:
            keep_filenames.add(icon_name)

        banner_url, banner_file, banner_name = await self._resolve_asset("banner", banner_source, message)
        if banner_file:
            if banner_name and banner_name not in files_by_name:
                files_by_name[banner_name] = banner_file
        if banner_name:
            keep_filenames.add(banner_name)

        updated = replace(snapshot, icon_url=icon_url, banner_url=banner_url)

        attachments: list[discord.Attachment] | None = None
        if message is not None:
            attachments = [
                att
                for att in message.attachments
                if not att.filename.startswith(ASSET_PREFIX) or att.filename in keep_filenames
            ]

        return updated, list(files_by_name.values()), attachments

    def _build_view(self) -> discord.ui.View | None:
        if not self.button_url:
            return None
        view = discord.ui.View()
        button = discord.ui.Button(
            style=discord.ButtonStyle.link,
            label="Uptime Kuma",
            url=self.button_url,
            emoji="🔗",
        )
        view.add_item(button)
        return view

    async def setup_hook(self) -> None:
        self.state = self.state_store.load()
        self.status_sync_loop.change_interval(seconds=self.refresh_interval)
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

    def _build_presence_items(self, snapshot: DashboardSnapshot) -> list[str]:
        monitors = snapshot.monitors
        total = len(monitors)
        if total == 0:
            return ["Waiting for monitors"]

        online = sum(1 for m in monitors if m.status == MonitorStatus.UP)
        offline = sum(1 for m in monitors if m.status == MonitorStatus.DOWN)

        items: list[str] = []

        if offline:
            items.append(f"{online} Online 🟢 | {offline} Offline 🔴")

        uptimes = [m.uptime_24h for m in monitors if m.uptime_24h is not None]
        pings = [m.ping_ms for m in monitors if m.ping_ms is not None]

        avg_ping = None
        if pings:
            avg_ping = sum(pings) / len(pings)

        avg_uptime = None
        if uptimes:
            avg_uptime = sum(uptimes) / len(uptimes)

        if avg_ping is not None and avg_uptime is not None:
            items.append(f"{online} Online 🟢 | avg ping {avg_ping:.0f}ms 📶")
            items.append(f"{online} Online 🟢 | 24h uptime {avg_uptime:.0f}% ⏱️")
            items.append(f"avg ping {avg_ping:.0f}ms 📶 | 24h uptime {avg_uptime:.0f}% ⏱️")
        elif avg_ping is not None:
            items.append(f"{online} Online 🟢 | avg ping {avg_ping:.0f}ms 📶")
        elif avg_uptime is not None:
            items.append(f"{online} Online 🟢 | 24h uptime {avg_uptime:.0f}% ⏱️")

        if not items:
            items.append(f"{online} Online 🟢")

        return items

    async def _update_presence(self, snapshot: DashboardSnapshot) -> None:
        """Rotate custom presence every few minutes, skip offline when there is none."""

        items = self._build_presence_items(snapshot)
        if not items:
            return

        now = time.monotonic()
        items_changed = items != self._presence_items
        should_update = items_changed or (now - self._last_presence_change >= self._presence_interval)

        if not should_update:
            return

        if items_changed:
            self._presence_items = items
            self._presence_index = random.randrange(len(self._presence_items))
        else:
            self._presence_index = (self._presence_index + 1) % len(self._presence_items)

        activity = discord.CustomActivity(name=self._presence_items[self._presence_index])
        await self.change_presence(activity=activity)
        self._last_presence_change = now
        LOGGER.info("Presence updated: %s", self._presence_items[self._presence_index])

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

        await self._update_presence(snapshot)

        message = await self._resolve_message(channel)
        snapshot_with_assets, files, attachments = await self._prepare_snapshot_assets(snapshot, message)
        render = self.embed_builder.build(snapshot_with_assets)
        view = self._build_view()

        try:
            if message is None:
                message = await channel.send(embeds=render.embeds, files=files, view=view)
                self.state.message_id = message.id
                self.state_store.save(self.state)
                LOGGER.info("Created new dashboard message: %s", message.id)
            else:
                if attachments is None:
                    await message.edit(embeds=render.embeds, attachments=files, view=view)
                else:
                    await message.edit(
                        embeds=render.embeds,
                        attachments=[*attachments, *files],
                        view=view,
                    )
                LOGGER.info("Dashboard updated (%s embeds)", len(render.embeds))
        except discord.HTTPException as exc:
            if exc.status == 429:
                LOGGER.warning("Discord rate limit hit while updating dashboard")
                return
            raise

        self.last_snapshot = snapshot

    @status_sync_loop.before_loop
    async def before_loop(self) -> None:
        await self.wait_until_ready()
