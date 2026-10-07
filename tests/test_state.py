import json

import pytest

from spotify_local_migrator.errors import StateError
from spotify_local_migrator.migration.state import CaptureStore, atomic_write_json


def test_full_capture_roundtrip_and_later_scans_do_not_overwrite_original(settings, capture):
    store = CaptureStore(settings.data_dir)
    first_path = store.save(capture)
    original = settings.data_dir / capture.playlist.playlist_id / "original.json"
    original_bytes = original.read_bytes()
    capture.snapshot_id = "new-snapshot"
    second_path = store.save(capture)
    assert first_path != second_path
    assert original.read_bytes() == original_bytes
    assert store.load(first_path).snapshot_id == "snapshot-one"
    assert store.load(second_path).snapshot_id == "new-snapshot"
    assert store.load(original).entries[0].raw == capture.entries[0].raw
    assert original.stat().st_mode & 0o777 == 0o600
    assert store.originals() == [original]


def test_failed_serialization_keeps_previous_state_and_cleans_temp(tmp_path):
    target = tmp_path / "state.json"
    atomic_write_json(target, {"old": True})
    with pytest.raises(TypeError):
        atomic_write_json(target, {"unserializable": object()})
    assert json.loads(target.read_text()) == {"old": True}
    assert list(tmp_path.glob(".pending-*")) == []


def test_invalid_playlist_id_cannot_write_outside_state_dir(settings, capture):
    capture.playlist.playlist_id = "../../escape"
    with pytest.raises(StateError):
        CaptureStore(settings.data_dir).save(capture)
    assert not settings.data_dir.exists()


def test_unreadable_capture_is_a_safe_error(tmp_path):
    target = tmp_path / "original.json"
    target.write_text("not json")
    with pytest.raises(StateError):
        CaptureStore.load(target)


def test_failed_disk_write_is_actionable(settings, capture, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("private system details")

    monkeypatch.setattr("spotify_local_migrator.migration.state.atomic_write_json", fail)
    with pytest.raises(StateError, match="permissions") as error:
        CaptureStore(settings.data_dir).save(capture)
    assert "private system details" not in str(error.value)
