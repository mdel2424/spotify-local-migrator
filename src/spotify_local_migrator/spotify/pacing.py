"""Pace every Web API attempt and persist a rolling local request budget."""

import logging
import os
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from ..errors import RequestBudgetError, StateError
from ..migration.state import atomic_write_json

logger = logging.getLogger(__name__)
WINDOW_SECONDS = 86400
Timestamp = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class RequestHistory(BaseModel):
    schema_version: Literal[1] = 1
    requests: list[Timestamp]

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
        budget: int,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        # Shared across clients/jobs using this data directory: developer quotas
        # are shared across Client IDs, so changing login must not reset usage.
        self.path = data_dir / ".cache" / "requests.json"
        self.cache_path = data_dir / ".cache" / "search.sqlite3"
        self.interval = interval
        self.budget = budget
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
                # Upgrades must not grant a fresh budget after an unpaced run.
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

    def usage(self) -> tuple[int, float | None]:
        """Read local usage without writing files, waiting or contacting Spotify."""
        history = self._history(self.clock())
        available_at = (
            history.requests[len(history.requests) - self.budget] + WINDOW_SECONDS
            if len(history.requests) >= self.budget
            else None
        )
        return len(history.requests), available_at

    def before_request(self) -> None:
        while True:
            try:
                with self._lock():
                    now = self.clock()
                    history = self._history(now)
                    if len(history.requests) >= self.budget:
                        available_at = (
                            history.requests[len(history.requests) - self.budget] + WINDOW_SECONDS
                        )
                        when = (
                            datetime.fromtimestamp(available_at)
                            .astimezone()
                            .isoformat(timespec="seconds")
                        )
                        raise RequestBudgetError(
                            "Local Spotify request budget reached "
                            f"({len(history.requests)}/{self.budget} "
                            f"attempts in the last 24 hours). Next slot: {when}. "
                            "No API request was sent. Matching/migration progress is saved; "
                            "use resume when the budget allows. Inspect with status."
                        )
                    delay = (
                        max(0.0, history.requests[-1] + self.interval - now)
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
