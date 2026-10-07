"""Persist long Retry-After windows across processes and CLI invocations."""

import hashlib
import math
import time
from pathlib import Path

from pydantic import BaseModel, ValidationError

from ..errors import RateLimitError, StateError
from ..migration.state import atomic_write_json


class Cooldown(BaseModel):
    client_key: str
    until: float
    retry_after: float


class CooldownStore:
    def __init__(self, data_dir: Path, client_id: str, *, clock=time.time):
        self.path = data_dir / ".cache" / "rate-limit.json"
        self.client_key = hashlib.sha256(client_id.encode()).hexdigest()
        self.clock = clock

    def check(self) -> None:
        if not self.path.exists():
            return
        try:
            value = Cooldown.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, ValidationError) as exc:
            raise StateError("Cannot read the saved Spotify rate-limit cooldown.") from exc
        if value.client_key != self.client_key:
            return
        remaining = value.until - self.clock()
        if math.isfinite(remaining) and remaining > 0:
            raise RateLimitError(
                f"Spotify cooldown is still active ({math.ceil(remaining)} seconds remaining). "
                "Progress is saved. Resume after the Retry-After window; no API request was sent.",
                retry_after=remaining,
            )

    def save(self, retry_after: float) -> None:
        if not math.isfinite(retry_after) or retry_after <= 0:
            return
        value = Cooldown(
            client_key=self.client_key, until=self.clock() + retry_after, retry_after=retry_after
        )
        try:
            atomic_write_json(self.path, value.model_dump(mode="json"))
        except (OSError, ValueError) as exc:
            raise StateError(
                "Cannot persist Spotify rate-limit cooldown; stop and check disk."
            ) from exc
