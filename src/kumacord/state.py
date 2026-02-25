from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class BotState:
    message_id: int | None = None


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> BotState:
        if not self.path.exists():
            return BotState()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            message_id = payload.get("message_id")
            return BotState(message_id=int(message_id) if message_id else None)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return BotState()

    def save(self, state: BotState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"message_id": state.message_id}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
