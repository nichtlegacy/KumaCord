from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Settings:
    discord_bot_token: str
    discord_channel_id: int
    refresh_interval: int
    language: str
    data_dir: Path
    kuma_statuspage_url: str | None
    kuma_api_url: str | None
    kuma_username: str | None
    kuma_password: str | None

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"



def _optional(name: str) -> str | None:
    value = os.getenv(name)
    if not value:
        return None
    return value.strip()



def load_settings() -> Settings:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    channel_id_raw = os.getenv("DISCORD_CHANNEL_ID", "").strip()
    if not token:
        raise ValueError("DISCORD_BOT_TOKEN is required")
    if not channel_id_raw:
        raise ValueError("DISCORD_CHANNEL_ID is required")

    return Settings(
        discord_bot_token=token,
        discord_channel_id=int(channel_id_raw),
        refresh_interval=max(10, int(os.getenv("REFRESH_INTERVAL", "30"))),
        language=os.getenv("LANGUAGE", "de").strip() or "de",
        data_dir=Path(os.getenv("DATA_DIR", "/app/data")),
        kuma_statuspage_url=_optional("KUMA_STATUSPAGE_URL"),
        kuma_api_url=_optional("KUMA_API_URL"),
        kuma_username=_optional("KUMA_USERNAME"),
        kuma_password=_optional("KUMA_PASSWORD"),
    )
