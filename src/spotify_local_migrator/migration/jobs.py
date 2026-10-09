import hashlib
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from ..errors import StateError
from ..matching.models import MatchReport
from ..models import PlaylistCapture
from .state import CaptureStore, atomic_write_json, validate_playlist_id


def content_hash(data: Any) -> str:
    if isinstance(data, BaseModel):
        data = data.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(
            data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class JobStore:
    def __init__(self, directory: Path):
        self.directory = directory

    @classmethod
    def create(cls, data_dir: Path, capture: PlaylistCapture) -> "JobStore":
        playlist_id = validate_playlist_id(capture.playlist.playlist_id)
        job_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        directory = data_dir / playlist_id / "jobs" / job_id
        directory.mkdir(parents=True, mode=0o700)
        store = cls(directory)
        store.save("original.json", capture)
        return store

    def save(self, name: str, data: BaseModel | dict[str, Any]) -> None:
        if name not in {
            "original.json",
            "matches.json",
            "plan.json",
            "migration.json",
            "probe.json",
            "compatibility.json",
        }:
            raise StateError("Invalid migration state filename.")
        try:
            atomic_write_json(
                self.directory / name,
                data.model_dump(mode="json") if isinstance(data, BaseModel) else data,
            )
        except (OSError, ValueError) as exc:
            raise StateError(
                "Cannot persist migration state; stop and check disk permissions."
            ) from exc

    def capture(self) -> PlaylistCapture:
        return CaptureStore.load(self.directory / "original.json")

    def report(self) -> MatchReport:
        try:
            report = MatchReport.model_validate_json(
                (self.directory / "matches.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError, ValidationError) as exc:
            raise StateError("Cannot read matching state for this job.") from exc
        if report.capture_hash != content_hash(self.capture()):
            raise StateError("Job capture changed after matching; start a new job.")
        return report

    @contextmanager
    def lock(self):
        if os.name != "posix":
            raise StateError("Migration locking currently requires a POSIX system.")
        import fcntl

        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.directory / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StateError("Another process is using this migration job.") from exc
            yield
        finally:
            os.close(descriptor)


def latest_job(data_dir: Path, playlist_id: str | None = None) -> JobStore:
    pattern = (validate_playlist_id(playlist_id) if playlist_id else "*") + "/jobs/*/matches.json"
    candidates = list(data_dir.glob(pattern))
    if not candidates:
        raise StateError("No matching jobs found. Run match or migrate --dry-run first.")
    return JobStore(max(candidates, key=lambda file: file.stat().st_mtime_ns).parent)
