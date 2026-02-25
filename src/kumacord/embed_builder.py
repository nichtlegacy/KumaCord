from __future__ import annotations

from dataclasses import dataclass

import discord

from kumacord.models import DashboardSnapshot, MonitorGroup, MonitorSnapshot, MonitorStatus

MAX_FIELDS = 25
MAX_EMBED_CHARS = 6000
UPTIME_KUMA_ICON_URL = "https://raw.githubusercontent.com/louislam/uptime-kuma/refs/heads/master/public/icon.png"

EMOJI_BY_STATUS = {
    MonitorStatus.UP: "🟢",
    MonitorStatus.DOWN: "🔴",
    MonitorStatus.PENDING: "⚠️",
    MonitorStatus.MAINTENANCE: "🔧",
    MonitorStatus.UNKNOWN: "❓",
}


@dataclass(slots=True)
class RenderResult:
    embeds: list[discord.Embed]
    summary_color: discord.Color


class EmbedBuilder:
    def _build_title(self, snapshot: DashboardSnapshot) -> str:
        monitors = snapshot.monitors
        total = len(monitors)
        online = sum(1 for m in monitors if m.status == MonitorStatus.UP)
        offline = sum(1 for m in monitors if m.status == MonitorStatus.DOWN)
        pings = [m.ping_ms for m in monitors if m.ping_ms is not None]

        if total == 0:
            stats = "No monitors"
        elif offline:
            stats = f"{online}/{total} Online 🟢 | {offline} Offline 🔴"
        elif pings:
            avg_ping = sum(pings) / len(pings)
            stats = f"{online}/{total} Online 🟢 | avg ping {avg_ping:.0f}ms 📶"
        else:
            stats = f"{online}/{total} Online 🟢"

        return stats

    def _monitor_field(self, monitor: MonitorSnapshot) -> tuple[str, str]:
        emoji = EMOJI_BY_STATUS.get(monitor.status, "❓")
        ping = "n/a" if monitor.ping_ms is None else f"{monitor.ping_ms:.0f}ms"
        uptime = "n/a" if monitor.uptime_24h is None else f"{monitor.uptime_24h:.1f}%"
        name = f"{emoji} {monitor.name}"
        value = f"📶 Ping: **`{ping}`**\n⏱️ 24h: **`{uptime}`**"
        return name, value

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
        timestamp = discord.utils.utcnow()
        title = self._build_title(snapshot)
        author_name = snapshot.title or "Uptime Kuma Dashboard"

        groups = snapshot.groups or []
        if not groups and snapshot.monitors:
            groups = [MonitorGroup(group_id=None, name="Monitors", weight=0, monitors=snapshot.monitors)]

        embeds: list[discord.Embed] = []
        current = discord.Embed(title=title, color=color, timestamp=timestamp)
        current.set_author(name=author_name, icon_url=UPTIME_KUMA_ICON_URL)
        if snapshot.icon_url:
            current.set_thumbnail(url=snapshot.icon_url)
        if snapshot.banner_url:
            current.set_image(url=snapshot.banner_url)

        inline_count = 0
        current_group_name: str | None = None
        header_added = True

        def start_new_embed() -> None:
            nonlocal current, inline_count, header_added
            current = discord.Embed(title=title, color=color, timestamp=timestamp)
            current.set_author(name=author_name, icon_url=UPTIME_KUMA_ICON_URL)
            if snapshot.icon_url:
                current.set_thumbnail(url=snapshot.icon_url)
            if snapshot.banner_url:
                current.set_image(url=snapshot.banner_url)
            inline_count = 0
            header_added = False

        def try_add_field(name: str, value: str, inline: bool) -> bool:
            nonlocal inline_count
            projected_chars = len(current) + len(name) + len(value)
            if len(current.fields) >= MAX_FIELDS or projected_chars >= (MAX_EMBED_CHARS - 100):
                return False
            current.add_field(name=name, value=value, inline=inline)
            if inline:
                inline_count += 1
            else:
                inline_count = 0
            return True

        def pad_inline_row() -> None:
            nonlocal inline_count
            needed = (3 - (inline_count % 3)) % 3
            for _ in range(needed):
                if not try_add_field("\u200b", "\u200b", inline=True):
                    break

        def finalize_embed() -> None:
            if current.fields:
                pad_inline_row()
            if not current.fields and not snapshot.monitors:
                current.description = "No monitors found."
            current.set_footer(text="Last updated", icon_url=snapshot.icon_url)
            embeds.append(current)

        def add_group_header(name: str) -> None:
            nonlocal header_added
            pad_inline_row()
            header_value = f"**{name}**"
            if not try_add_field("\u200b", header_value, inline=False):
                finalize_embed()
                start_new_embed()
                try_add_field("\u200b", header_value, inline=False)
            header_added = True

        def add_monitor_field(name: str, value: str) -> None:
            if current_group_name and not header_added:
                add_group_header(current_group_name)
            if not try_add_field(name, value, inline=True):
                finalize_embed()
                start_new_embed()
                if current_group_name:
                    add_group_header(current_group_name)
                try_add_field(name, value, inline=True)

        if groups:
            for group in groups:
                if not group.monitors:
                    continue
                current_group_name = group.name
                header_added = False
                for monitor in group.monitors:
                    field_name, field_value = self._monitor_field(monitor)
                    add_monitor_field(field_name, field_value)
        else:
            for monitor in snapshot.monitors:
                field_name, field_value = self._monitor_field(monitor)
                add_monitor_field(field_name, field_value)

        finalize_embed()

        return RenderResult(embeds=embeds, summary_color=color)
