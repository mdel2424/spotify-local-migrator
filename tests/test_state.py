import json
from datetime import timedelta

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


def test_saved_local_counts_use_latest_capture_time_and_include_zero(settings, capture):
    from spotify_local_migrator.migration.jobs import JobStore

    store = CaptureStore(settings.data_dir)
    playlist_id = capture.playlist.playlist_id
    store.save(capture)
    latest = capture.model_copy(
        update={"captured_at": capture.captured_at + timedelta(days=2), "entries": []}
    )
    store.save(latest)
    # An older imported capture can be saved to a new job after the latest scan.
    JobStore.create(settings.data_dir, capture)
    assert store.local_counts([playlist_id]) == {playlist_id: "0"}
    assert len(store.load(store.originals()[0]).local_tracks) == 1
    newer_job_capture = capture.model_copy(
        update={"captured_at": capture.captured_at + timedelta(days=3)}
    )
    JobStore.create(settings.data_dir, newer_job_capture)
    assert store.local_counts([playlist_id]) == {playlist_id: "1"}


@pytest.mark.parametrize(
    "relative_path", ["original.json", "scans/scan/original.json", "jobs/job/original.json"]
)
def test_saved_local_counts_recognize_existing_captures(settings, capture, relative_path):
    target = settings.data_dir / capture.playlist.playlist_id / relative_path
    atomic_write_json(target, capture.model_dump(mode="json"))
    assert CaptureStore(settings.data_dir).local_counts([capture.playlist.playlist_id]) == {
        capture.playlist.playlist_id: "1"
    }


def test_saved_local_counts_ignore_missing_invalid_and_wrong_playlist_captures(settings, capture):
    store = CaptureStore(settings.data_dir)
    playlist_id = capture.playlist.playlist_id
    assert store.local_counts([playlist_id, "../escape"]) == {}
    assert not settings.data_dir.exists()
    store.save(capture)
    bad_path = settings.data_dir / playlist_id / "jobs" / "broken" / "original.json"
    bad_path.parent.mkdir(parents=True)
    bad_path.write_text("incomplete json")
    wrong_capture = capture.model_copy(
        update={
            "captured_at": capture.captured_at + timedelta(days=1),
            "playlist": capture.playlist.model_copy(update={"playlist_id": "B" * 22}),
            "entries": [],
        }
    )
    atomic_write_json(
        bad_path.parent.parent / "wrong" / "original.json", wrong_capture.model_dump(mode="json")
    )
    assert store.local_counts([playlist_id, "B" * 22]) == {playlist_id: "1"}
