"""Pace every Web API attempt and retain usage statistics across restarts."""

import logging
import os
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from ..errors import StateError
from ..migration.state import atomic_write_json

logger = logging.getLogger(__name__)
WINDOW_SECONDS = 86400
Timestamp = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class RequestHistory(BaseModel):
    schema_version: Literal[1] = 1
    requests: list[Timestamp]
    backoff_interval: float = Field(default=0, ge=0, le=60, allow_inf_nan=False)

    @field_validator("requests")
    @classmethod
    def ordered(cls, values: list[float]) -> list[float]:
        if values != sorted(values):
            raise ValueError("Request timestamps must be ordered.")
        return values


class RequestPacer:
    def __init__(
        self,
        data_dir: Path,
        *,
        interval: float,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        # Shared across clients/jobs using this data directory: developer quotas
        # are shared across Client IDs, so changing login must not reset usage.
        self.path = data_dir / ".cache" / "requests.json"
        self.cache_path = data_dir / ".cache" / "search.sqlite3"
        self.interval = interval
        self.clock = clock
        self.sleep = sleep

    @contextmanager
    def _lock(self):
        if os.name != "posix":
            raise StateError("Persistent Spotify request pacing requires a POSIX system.")
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _history(self, now: float) -> RequestHistory:
        try:
            if self.path.exists():
                history = RequestHistory.model_validate_json(self.path.read_text(encoding="utf-8"))
            else:
                # Cached successes provide a lower bound for past API usage.
                timestamps = []
                if self.cache_path.exists():
                    connection = sqlite3.connect(
                        self.cache_path.resolve().as_uri() + "?mode=ro", uri=True
                    )
                    try:
                        timestamps = [
                            row[0]
                            for row in connection.execute(
                                "SELECT created FROM searches WHERE created > ? ORDER BY created",
                                (now - WINDOW_SECONDS,),
                            )
                        ]
                    finally:
                        connection.close()
                history = RequestHistory(requests=timestamps)
        except (OSError, ValueError, ValidationError, sqlite3.Error) as exc:
            raise StateError(
                "Cannot read Spotify request history; stop and check local state."
            ) from exc
        history.requests = [stamp for stamp in history.requests if stamp > now - WINDOW_SECONDS]
        return history

    def usage(self) -> int:
        """Read local usage without writing files, waiting or contacting Spotify."""
        history = self._history(self.clock())
        return len(history.requests)

    def effective_interval(self) -> float:
        return max(self.interval, self._history(self.clock()).backoff_interval)

    def back_off(self) -> None:
        """Slow down after a short burst limit; quotas use the server cooldown."""
        if not self.interval:
            return
        try:
            with self._lock():
                history = self._history(self.clock())
                history.backoff_interval = min(
                    60.0, max(self.interval, history.backoff_interval) * 2
                )
                atomic_write_json(self.path, history.model_dump(mode="json"))
        except OSError as exc:
            raise StateError("Cannot persist Spotify pacing backoff; stop and check disk.") from exc

    def before_request(self) -> None:
        while True:
            try:
                with self._lock():
                    now = self.clock()
                    history = self._history(now)
                    delay = (
                        max(
                            0.0,
                            history.requests[-1]
                            + max(self.interval, history.backoff_interval)
                            - now,
                        )
                        if history.requests
                        else 0.0
                    )
                    if not delay:
                        # Reserve/count before dispatch, including failures and
                        # retries. A crash can overcount, never erase an attempt.
                        history.requests.append(now)
                        atomic_write_json(self.path, history.model_dump(mode="json"))
                        return
            except OSError as exc:
                raise StateError(
                    "Cannot persist Spotify request usage; no API request was sent."
                ) from exc
            # Do not hold a process lock while waiting. Recheck afterwards so
            # simultaneous commands cannot reserve the same request slot.
            logger.debug("Pacing Spotify requests; waiting %.2fs", delay)
            self.sleep(min(delay, 60.0))
