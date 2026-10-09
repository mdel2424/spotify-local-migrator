"""Avoid repeated captures while retaining fresh verification around mutations."""

import json

import pytest
from test_migration import executor, original_calls
from test_migration import job as job

from spotify_local_migrator.errors import PlaylistChangedError, SpotifyAPIError
from spotify_local_migrator.migration.planner import identities


def observe_scans(api, monkeypatch):
    observed = []
    scan = api.scan

    def record(playlist_id):
        capture = scan(playlist_id)
        if playlist_id == api.original_id:
            observed.append(identities(capture))
        return capture

    monkeypatch.setattr(api, "scan", record)
    return observed


def test_two_verification_reads_per_replacement_and_no_duplicate_prewrite_scans(job, monkeypatch):
    store, _, _, plan = job
    execution, api = executor(job)
    observed = observe_scans(api, monkeypatch)
    before_writes = []
    add, delete = api.add_tracks, api.remove_positions

    def add_and_record(playlist_id, *args):
        if playlist_id == api.original_id:
            before_writes.append(observed[-1])
        return add(playlist_id, *args)

    def delete_and_record(playlist_id, *args):
        if playlist_id == api.original_id:
            before_writes.append(observed[-1])
        return delete(playlist_id, *args)

    monkeypatch.setattr(api, "add_tracks", add_and_record)
    monkeypatch.setattr(api, "remove_positions", delete_and_record)
    journal = execution.apply(store)
    # Initial read, fresh read after the probe, then one after each write.
    assert len(observed) == 2 + 2 * len(plan.replacements)
    assert before_writes == observed[1:-1]
    assert observed[-1] == plan.desired
    assert journal.phase == "COMPLETE" and journal.pending is None
    assert len(original_calls(api, "add")) == len(plan.replacements)
    assert len(original_calls(api, "delete")) == len(plan.replacements)
    scans_before = len(observed)
    calls_before = len(api.calls)
    execution.apply(store)
    assert len(observed) == scans_before + 1  # Resume always reads again.
    assert len(api.calls) == calls_before


@pytest.mark.parametrize("changed", [False, True])
def test_delayed_progress_invalidates_the_reusable_capture(job, monkeypatch, changed):
    store, _, _, plan = job
    execution, api = executor(job)
    observed = observe_scans(api, monkeypatch)
    now = 0
    execution.clock = lambda: now
    count_at_delay = None

    def progress(message):
        nonlocal now, count_at_delay
        if message.startswith("Verified replacement 1/"):
            count_at_delay = len(observed)
            now += 10
            if changed:
                api.entries[api.original_id].reverse()
                api._snapshot(api.original_id)

    execution.progress = progress
    if changed:
        with pytest.raises(PlaylistChangedError):
            execution.apply(store)
        assert len(observed) == count_at_delay + 1
        assert len(original_calls(api, "add")) == 1
        assert len(original_calls(api, "delete")) == 1
    else:
        assert execution.apply(store).phase == "COMPLETE"
        assert len(observed) == 3 + 2 * len(plan.replacements)
        assert observed[-1] == plan.desired


def test_interruption_after_progress_resumes_from_persisted_verification(job, monkeypatch):
    store, _, _, plan = job
    execution, api = executor(job)
    observed = observe_scans(api, monkeypatch)

    def stop(message):
        if message.startswith("Verified replacement 1/"):
            state = json.loads((store.directory / "migration.json").read_text())
            assert state["next_replacement"] == 1
            assert state["pending"] is None
            raise SpotifyAPIError("interrupted after saved progress")

    execution.progress = stop
    with pytest.raises(SpotifyAPIError, match="after saved progress"):
        execution.apply(store)
    count_before = len(observed)
    execution.progress = lambda message: None
    assert execution.apply(store).phase == "COMPLETE"
    assert len(observed) - count_before == 1 + 2 * (len(plan.replacements) - 1)
    assert observed[-1] == plan.desired
    assert len(original_calls(api, "add")) == len(plan.replacements)
    assert len(original_calls(api, "delete")) == len(plan.replacements)
