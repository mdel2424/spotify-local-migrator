"""Persist Spotify cooldowns and wait for them without polling the API."""

import hashlib
import logging
import math
import os
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from ..errors import RateLimitError, StateError
from ..migration.state import atomic_write_json

logger = logging.getLogger(__name__)


class Cooldown(BaseModel):
    client_key: str
    until: float = Field(ge=0, allow_inf_nan=False)
    retry_after: float = Field(ge=0, allow_inf_nan=False)
    reason: Literal["QUOTA_EXCEEDED"] | None = None
    estimated: bool = False


class CooldownStore:
    def __init__(self, data_dir: Path, client_id: str, *, clock=time.time):
        self.path = data_dir / ".cache" / "rate-limit.json"
        self.client_key = hashlib.sha256(client_id.encode()).hexdigest()
        self.clock = clock

    def _load(self) -> Cooldown | None:
        if not self.path.exists():
            return None
        try:
            value = Cooldown.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, ValidationError) as exc:
            raise StateError("Cannot read the saved Spotify rate-limit cooldown.") from exc
        if value.client_key != self.client_key:
            return None
        return value

    def remaining(self) -> float:
        value = self._load()
        return max(0.0, value.until - self.clock()) if value else 0.0

    def check(self) -> None:
        value = self._load()
        if value is None:
            return
        remaining = value.until - self.clock()
        if math.isfinite(remaining) and remaining > 0:
            raise RateLimitError(
                f"Spotify cooldown is still active ({math.ceil(remaining)} seconds remaining). "
                "Progress is saved; no API request was sent.",
                retry_after=remaining,
                reason=value.reason,
            )

    def wait(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        notify: Callable[[str], None] | None = None,
    ) -> bool:
        notify = notify or logger.warning
        next_notice = 0.0
        waited = False
        while True:
            value = self._load()
            remaining = max(0.0, value.until - self.clock()) if value else 0.0
            if not remaining:
                if waited:
                    notify("Spotify cooldown finished; continuing automatically.")
                return waited
            now = self.clock()
            if now >= next_notice:
                when = (
                    datetime.fromtimestamp(value.until).astimezone().isoformat(timespec="seconds")
                )
                source = (
                    "estimated backoff; Spotify supplied no reset time"
                    if value.estimated
                    else "Retry-After"
                )
                notify(
                    f"Spotify cooldown ({source}): {math.ceil(remaining)} seconds remaining. "
                    f"Continuing automatically at {when}. Ctrl+C stops with progress saved."
                )
                next_notice = now + 300
            waited = True
            # Check persisted extensions regularly and remain interruptible.
            sleep(min(remaining, 60.0))

    def fallback_delay(self, reason: str | None) -> float:
        """Conservative retry backoff, not a claimed Spotify quota reset time."""
        previous = self._load()
        initial = 3600.0 if reason == "QUOTA_EXCEEDED" else 60.0
        if previous and previous.estimated and previous.reason == reason:
            return min(86400.0, max(initial, previous.retry_after * 2))
        return initial

    @contextmanager
    def _lock(self):
        if os.name != "posix":
            raise StateError("Persistent Spotify cooldown locking requires a POSIX system.")
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def save(
        self, retry_after: float, *, reason: str | None = None, estimated: bool = False
    ) -> None:
        if not math.isfinite(retry_after) or retry_after <= 0:
            return
        value = Cooldown(
            client_key=self.client_key,
            until=self.clock() + retry_after,
            retry_after=retry_after,
            reason=reason,
            estimated=estimated,
        )
        try:
            with self._lock():
                previous = self._load()
                if previous and previous.until >= value.until:
                    return
                atomic_write_json(self.path, value.model_dump(mode="json"))
        except (OSError, ValueError) as exc:
            raise StateError(
                "Cannot persist Spotify rate-limit cooldown; stop and check disk."
            ) from exc
