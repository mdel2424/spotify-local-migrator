import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..errors import StateError
from ..models import PlaylistCapture

PLAYLIST_ID_PATTERN = re.compile(r"[A-Za-z0-9]{22}")


def validate_playlist_id(playlist_id: str) -> str:
    if not PLAYLIST_ID_PATTERN.fullmatch(playlist_id):
        raise StateError("Expected a 22-character Spotify playlist ID.")
    return playlist_id


def sync_directory(directory: Path) -> None:
    if os.name == "posix":
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_write_json(target: Path, data: dict[str, Any]) -> None:
    """Publish complete JSON with restrictive permissions and durable replacement."""
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".pending-", suffix=".json", dir=target.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        temporary = None
        sync_directory(target.parent)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


class CaptureStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir

    def save(self, capture: PlaylistCapture) -> Path:
        playlist_id = validate_playlist_id(capture.playlist.playlist_id)
        playlist_dir = self.data_dir / playlist_id
        scan_id = capture.captured_at.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:8]
        target = playlist_dir / "scans" / scan_id / "original.json"
        try:
            atomic_write_json(target, capture.model_dump(mode="json"))
            # Exclusive publication: later scans never replace the original baseline.
            try:
                os.link(target, playlist_dir / "original.json")
                sync_directory(playlist_dir)
            except FileExistsError:
                pass
        except OSError as exc:
            raise StateError("Cannot save the scan; check data directory permissions.") from exc
        return target

    def originals(self) -> list[Path]:
        return sorted(self.data_dir.glob("*/original.json"))

    @staticmethod
    def load(target: Path) -> PlaylistCapture:
        from pydantic import ValidationError

        try:
            return PlaylistCapture.model_validate_json(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, ValidationError) as exc:
            raise StateError("Cannot read scan state; it may be missing or incomplete.") from exc
