from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MonitorStatus(str, Enum):
    UP = "up"
    DOWN = "down"
    PENDING = "pending"
    MAINTENANCE = "maintenance"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class MonitorSnapshot:
    monitor_id: int
    name: str
    status: MonitorStatus
    ping_ms: float | None
    uptime_24h: float | None


@dataclass(slots=True)
class MonitorGroup:
    group_id: int | None
    name: str
    weight: int
    monitors: list[MonitorSnapshot]


@dataclass(slots=True)
class DashboardBranding:
    title: str
    icon_url: str | None = None
    banner_url: str | None = None


@dataclass(slots=True)
class DashboardSnapshot:
    title: str
    icon_url: str | None
    monitors: list[MonitorSnapshot]
    groups: list[MonitorGroup]
    source: str
    banner_url: str | None = None
