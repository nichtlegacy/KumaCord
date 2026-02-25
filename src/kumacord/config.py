from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Settings:
    discord_bot_token: str
    discord_channel_id: int
    refresh_interval: int
    data_dir: Path
    button_url: str | None
    kuma_api_key: str | None
    kuma_statuspage_url: str | None
    kuma_api_url: str | None

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"



def _optional(name: str) -> str | None:
    value = os.getenv(name)
    if not value:
        return None
    return value.strip()


def _require_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _parse_refresh_interval(value: str | None) -> int:
    default = 30
    if value is None:
        return default
    raw = value.strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    if parsed < 30:
        return default
    if parsed > 86_400:
        return 86_400
    return parsed


def load_settings() -> Settings:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    channel_id_raw = os.getenv("DISCORD_CHANNEL_ID", "").strip()
    if not token:
        raise ValueError("DISCORD_BOT_TOKEN is required")
    if not channel_id_raw:
        raise ValueError("DISCORD_CHANNEL_ID is required")
    channel_id = _require_int(channel_id_raw, "DISCORD_CHANNEL_ID")

    return Settings(
        discord_bot_token=token,
        discord_channel_id=channel_id,
        refresh_interval=_parse_refresh_interval(os.getenv("REFRESH_INTERVAL")),
        data_dir=Path(os.getenv("DATA_DIR", "/app/data")),
        button_url=_optional("BUTTON_URL"),
        kuma_api_key=_optional("KUMA_API_KEY"),
        kuma_statuspage_url=_optional("KUMA_STATUSPAGE_URL"),
        kuma_api_url=_optional("KUMA_API_URL"),
    )
