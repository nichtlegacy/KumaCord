from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import discord

from kumacord.models import DashboardSnapshot, MonitorSnapshot, MonitorStatus

MAX_FIELDS = 25
MAX_EMBED_CHARS = 6000

EMOJI_BY_STATUS = {
    MonitorStatus.UP: "✅",
    MonitorStatus.DOWN: "❌",
    MonitorStatus.PENDING: "⚠️",
    MonitorStatus.MAINTENANCE: "🔧",
    MonitorStatus.UNKNOWN: "❓",
}


@dataclass(slots=True)
class RenderResult:
    embeds: list[discord.Embed]
    summary_color: discord.Color


class EmbedBuilder:
    def __init__(self, language: str = "de") -> None:
        self.language = language

    def _monitor_line(self, monitor: MonitorSnapshot) -> str:
        emoji = EMOJI_BY_STATUS.get(monitor.status, "❓")
        ping = "n/a" if monitor.ping_ms is None else f"{monitor.ping_ms:.0f}ms"
        uptime = "n/a" if monitor.uptime_24h is None else f"{monitor.uptime_24h:.1f}%"
        return f"{emoji} **{monitor.name}** | Ping: {ping} | 24h: {uptime}"

    def _overall_color(self, monitors: list[MonitorSnapshot]) -> discord.Color:
        if not monitors:
            return discord.Color.from_rgb(210, 135, 0)
        if any(m.status == MonitorStatus.DOWN for m in monitors):
            return discord.Color.from_rgb(200, 70, 60)
        if any(m.status in {MonitorStatus.PENDING, MonitorStatus.MAINTENANCE, MonitorStatus.UNKNOWN} for m in monitors):
            return discord.Color.from_rgb(210, 135, 0)
        return discord.Color.from_rgb(45, 150, 90)

    def build(self, snapshot: DashboardSnapshot) -> RenderResult:
        color = self._overall_color(snapshot.monitors)
        unix_ts = int(datetime.now(tz=timezone.utc).timestamp())
        footer = f"Last updated <t:{unix_ts}:R>"

        embeds: list[discord.Embed] = []
        current = discord.Embed(title=snapshot.title, color=color)
        if snapshot.icon_url:
            current.set_thumbnail(url=snapshot.icon_url)
            current.set_author(name=snapshot.title, icon_url=snapshot.icon_url)
        if snapshot.banner_url:
            current.set_image(url=snapshot.banner_url)

        for monitor in snapshot.monitors:
            value = self._monitor_line(monitor)
            would_exceed_fields = len(current.fields) >= MAX_FIELDS
            projected_chars = len(current) + len(value) + len(monitor.name)
            would_exceed_chars = projected_chars >= (MAX_EMBED_CHARS - 100)

            if would_exceed_fields or would_exceed_chars:
                current.set_footer(text=footer, icon_url=snapshot.icon_url)
                embeds.append(current)
                current = discord.Embed(title=snapshot.title, color=color)
                if snapshot.icon_url:
                    current.set_thumbnail(url=snapshot.icon_url)
                    current.set_author(name=snapshot.title, icon_url=snapshot.icon_url)
                if snapshot.banner_url:
                    current.set_image(url=snapshot.banner_url)

            current.add_field(name=monitor.name, value=value, inline=False)

        if not current.fields:
            current.description = "No monitors found."

        current.set_footer(text=footer, icon_url=snapshot.icon_url)
        embeds.append(current)

        return RenderResult(embeds=embeds, summary_color=color)
