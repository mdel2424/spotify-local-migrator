import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_migration import C, P, executor, original_calls, raw_track
from test_migration import job as job
from typer.testing import CliRunner

from spotify_local_migrator import cli
from spotify_local_migrator.errors import (
    PlaylistChangedError,
    RateLimitError,
    SpotifyAPIError,
    StateError,
)
from spotify_local_migrator.migration.planner import identities
from spotify_local_migrator.migration.scanner import parse_entry


def live_behavior(job, monkeypatch):
    """Reproduce the user's current-index DELETE and unchanged GET snapshot."""
    execution, api = executor(job)
    api.position_mode = "shift"
    scan = api.scan
    original_snapshot = job[1].snapshot_id

    def stale_snapshot(playlist_id):
        capture = scan(playlist_id)
        capture.snapshot_id = (
            original_snapshot if playlist_id == api.original_id else "creation-version"
        )
        capture.playlist.snapshot_id = capture.snapshot_id
        return capture

    monkeypatch.setattr(api, "scan", stale_snapshot)
    return execution, api


def saved_progress(store):
    path = store.directory / "migration.json"
    return json.loads(path.read_text()) if path.exists() else None


def pause_after_two_replacements(job, monkeypatch):
    store, _, _, _ = job
    execution, api = live_behavior(job, monkeypatch)

    def interrupted_progress(message):
        if message.startswith("Verified replacement 2/"):
            raise SpotifyAPIError("interrupted after two verified replacements")

    monkeypatch.setattr(execution, "progress", interrupted_progress)
    with pytest.raises(SpotifyAPIError, match="interrupted after two"):
        execution.apply(store)
    monkeypatch.setattr(execution, "progress", lambda message: None)
    assert saved_progress(store)["next_replacement"] == 2
    return execution, api


def test_user_reported_behavior_migrates_exact_order_with_each_delete_sent_once(job, monkeypatch):
    store, _, _, plan = job
    execution, api = live_behavior(job, monkeypatch)
    journal = execution.apply(store)
    assert journal.phase == "COMPLETE"
    assert identities(api.scan(api.original_id)) == plan.desired
    assert len(original_calls(api, "add")) == len(plan.replacements)
    assert len(original_calls(api, "delete")) == len(plan.replacements)
    assert [call[2] for call in original_calls(api, "delete")] == [[5], [3], [1]]
    assert all(call[3] != plan.snapshot_id for call in original_calls(api, "delete"))
    probe = json.loads((store.directory / "probe.json").read_text())
    assert probe["phase"] == "PASSED"
    assert not probe["snapshot_reads_reliable"]
    assert len([call for call in api.calls if call[0] == "delete" and call[1] == P]) == 2
    previous_calls = len(api.calls)
    execution.apply(store)
    assert len(api.calls) == previous_calls


@pytest.mark.parametrize("version", ["latest_mutation", "earlier_mutation", "initial_read"])
def test_known_snapshot_can_catch_up_or_regress_between_verified_operations(
    job, monkeypatch, version
):
    store, _, _, plan = job
    execution, api = live_behavior(job, monkeypatch)
    scan = api.scan

    def lagging_metadata(playlist_id):
        capture = scan(playlist_id)
        state = saved_progress(store)
        if playlist_id == api.original_id and state:
            # Verify each mutation with a different snapshot than the next
            # stable read: real Spotify metadata caught up only between writes.
            if state["pending"] is None and state["operations"]:
                capture.snapshot_id = api.snapshots[playlist_id]
            elif state["operations"]:
                if version == "latest_mutation":
                    capture.snapshot_id = state["mutation_snapshot"]
                elif version == "earlier_mutation":
                    capture.snapshot_id = state["operations"][0]["acknowledged_snapshot"]
            capture.playlist.snapshot_id = capture.snapshot_id
        return capture

    monkeypatch.setattr(api, "scan", lagging_metadata)
    journal = execution.apply(store)
    assert journal.phase == "COMPLETE"
    assert identities(api.scan(api.original_id)) == plan.desired
    assert len(original_calls(api, "add")) == len(plan.replacements)
    assert len(original_calls(api, "delete")) == len(plan.replacements)


@pytest.mark.parametrize("version", ["latest_mutation", "earlier_mutation"])
def test_resume_two_completed_replacements_after_snapshot_catches_up(job, monkeypatch, version):
    store, _, _, plan = job
    execution, api = pause_after_two_replacements(job, monkeypatch)
    previous_operations = saved_progress(store)["operations"]
    scan = api.scan
    observed = (
        saved_progress(store)["mutation_snapshot"]
        if version == "latest_mutation"
        else previous_operations[0]["acknowledged_snapshot"]
    )

    def caught_up_metadata(playlist_id):
        capture = scan(playlist_id)
        if playlist_id == api.original_id:
            capture.snapshot_id = capture.playlist.snapshot_id = observed
        return capture

    monkeypatch.setattr(api, "scan", caught_up_metadata)
    journal = execution.apply(store)
    assert journal.phase == "COMPLETE"
    assert journal.operations[:4] == previous_operations
    assert identities(api.scan(api.original_id)) == plan.desired
    assert [call[2] for call in original_calls(api, "add")] == [4, 2, 0]
    assert [call[2] for call in original_calls(api, "delete")] == [[5], [3], [1]]


def test_reliable_snapshot_policy_still_requires_saved_version_on_resume(job, monkeypatch):
    store, _, _, _ = job
    execution, api = pause_after_two_replacements(job, monkeypatch)
    proof_path = store.directory / "compatibility.json"
    proof = json.loads(proof_path.read_text())
    proof["snapshot_reads_reliable"] = True
    store.save("compatibility.json", proof)
    scan = api.scan

    def advanced_metadata(playlist_id):
        capture = scan(playlist_id)
        if playlist_id == api.original_id:
            capture.snapshot_id = capture.playlist.snapshot_id = api.snapshots[playlist_id]
        return capture

    monkeypatch.setattr(api, "scan", advanced_metadata)
    previous_calls = len(api.calls)
    with pytest.raises(PlaylistChangedError):
        execution.apply(store)
    assert len(api.calls) == previous_calls


@pytest.mark.parametrize("change_sequence", [False, True])
def test_resume_still_stops_on_unknown_snapshot_or_changed_sequence(
    job, monkeypatch, change_sequence
):
    store, _, _, _ = job
    execution, api = pause_after_two_replacements(job, monkeypatch)
    scan = api.scan
    observed = saved_progress(store)["mutation_snapshot"] if change_sequence else "external-version"
    if change_sequence:
        entries = api.entries[api.original_id]
        entries.append(parse_entry({"item": raw_track(C), "_occurrence": "external"}, len(entries)))

    def changed_read(playlist_id):
        capture = scan(playlist_id)
        if playlist_id == api.original_id:
            capture.snapshot_id = capture.playlist.snapshot_id = observed
        return capture

    monkeypatch.setattr(api, "scan", changed_read)
    previous_calls = len(api.calls)
    with pytest.raises(PlaylistChangedError):
        execution.apply(store)
    assert len(api.calls) == previous_calls
    assert saved_progress(store)["next_replacement"] == 2


@pytest.mark.parametrize("kind", ["add", "delete"])
def test_pending_before_state_with_known_snapshot_catchup_never_replays(job, monkeypatch, kind):
    store, _, _, plan = job
    execution, api = pause_after_two_replacements(job, monkeypatch)
    api.fail_before = kind
    with pytest.raises(SpotifyAPIError):
        execution.apply(store)
    scan = api.scan
    observed = saved_progress(store)["mutation_snapshot"]

    def caught_up_metadata(playlist_id):
        capture = scan(playlist_id)
        if playlist_id == api.original_id:
            capture.snapshot_id = capture.playlist.snapshot_id = observed
        return capture

    monkeypatch.setattr(api, "scan", caught_up_metadata)
    previous_calls = len(api.calls)
    with pytest.raises(StateError, match="unconfirmed"):
        execution.apply(store)
    assert len(api.calls) == previous_calls
    journal = execution.apply(
        store, retry_unconfirmed_add=kind == "add", retry_unconfirmed_delete=kind == "delete"
    )
    assert journal.phase == "COMPLETE"
    assert identities(api.scan(api.original_id)) == plan.desired


@pytest.mark.parametrize("kind", ["add", "delete"])
def test_lost_committed_response_with_stale_snapshot_is_reconciled_without_replay(
    job, monkeypatch, kind
):
    store, _, _, plan = job
    execution, api = live_behavior(job, monkeypatch)
    api.fail_after = kind
    with pytest.raises(SpotifyAPIError):
        execution.apply(store)
    execution.apply(store)
    assert identities(api.scan(api.original_id)) == plan.desired
    assert len(original_calls(api, "add")) == len(plan.replacements)
    assert len(original_calls(api, "delete")) == len(plan.replacements)


def test_uncommitted_delete_with_stale_snapshot_stops_and_requires_explicit_retry(job, monkeypatch):
    store, _, _, plan = job
    execution, api = live_behavior(job, monkeypatch)
    api.fail_before = "delete"
    with pytest.raises(SpotifyAPIError):
        execution.apply(store)
    for _ in range(2):
        with pytest.raises(StateError, match="No deletion was replayed"):
            execution.apply(store)
    assert len(original_calls(api, "delete")) == 1
    execution.apply(store, retry_unconfirmed_delete=True)
    assert identities(api.scan(api.original_id)) == plan.desired
    assert len(original_calls(api, "delete")) == len(plan.replacements) + 1


@pytest.mark.parametrize("lost_response", [False, True])
def test_delayed_delete_readback_never_replays_even_when_old_snapshot_is_unchanged(
    job, monkeypatch, lost_response
):
    store, _, _, plan = job
    execution, api = live_behavior(job, monkeypatch)
    scan, remove = api.scan, api.remove_positions
    cached_before = None
    hide_commit = True

    def delete(playlist_id, positions, snapshot):
        nonlocal cached_before
        if playlist_id == api.original_id and cached_before is None:
            cached_before = scan(playlist_id)
        return remove(playlist_id, positions, snapshot)

    def delayed_read(playlist_id):
        if playlist_id == api.original_id and cached_before is not None and hide_commit:
            return cached_before.model_copy(deep=True)
        return scan(playlist_id)

    monkeypatch.setattr(api, "remove_positions", delete)
    monkeypatch.setattr(api, "scan", delayed_read)
    if lost_response:
        api.fail_after = "delete"
    with pytest.raises(SpotifyAPIError if lost_response else StateError):
        execution.apply(store)
    with pytest.raises(StateError):
        execution.apply(store)
    assert len(original_calls(api, "delete")) == 1
    hide_commit = False
    execution.apply(store)
    assert identities(api.scan(api.original_id)) == plan.desired
    assert len(original_calls(api, "delete")) == len(plan.replacements)


def test_external_edit_is_detected_even_when_read_snapshot_stays_constant(job, monkeypatch):
    store, _, _, _ = job
    execution, api = live_behavior(job, monkeypatch)
    api.fail_before = "delete"
    with pytest.raises(SpotifyAPIError):
        execution.apply(store)
    entries = api.entries[api.original_id]
    entries.append(parse_entry({"item": raw_track(C), "_occurrence": "external"}, len(entries)))
    api._snapshot(api.original_id)
    previous_calls = len(api.calls)
    with pytest.raises(PlaylistChangedError):
        execution.apply(store)
    assert len(api.calls) == previous_calls


def test_explicit_rate_limit_delete_rejection_can_resume_after_reverification(job, monkeypatch):
    store, _, _, plan = job
    execution, api = live_behavior(job, monkeypatch)
    api.settings.wait_for_rate_limits = True
    remove = api.remove_positions
    rejected = False
    waits = []

    def delete(playlist_id, positions, snapshot):
        nonlocal rejected
        if playlist_id == api.original_id and not rejected:
            rejected = True
            raise RateLimitError("Explicit rejection", retry_after=86400)
        return remove(playlist_id, positions, snapshot)

    monkeypatch.setattr(api, "remove_positions", delete)
    monkeypatch.setattr(api, "wait_for_cooldown", lambda: waits.append(True), raising=False)
    execution.apply(store)
    assert waits == [True]
    assert identities(api.scan(api.original_id)) == plan.desired
    assert len(original_calls(api, "delete")) == len(plan.replacements)


def test_uncertain_delete_retry_confirmation_defaults_to_no(job, settings, monkeypatch):
    store, _, _, _ = job
    store.save("migration.json", {})
    monkeypatch.setattr(cli, "load_settings", lambda env_file: settings)
    retry_flags = []

    @contextmanager
    def services(*args):
        yield None, object()

    class Execution:
        def __init__(self, *args, **kwargs):
            pass

        def apply(self, store, **kwargs):
            retry_flags.append(kwargs["retry_unconfirmed_delete"])
            return SimpleNamespace(phase="COMPLETE", next_replacement=3)

    monkeypatch.setattr(cli, "services", services)
    monkeypatch.setattr("spotify_local_migrator.migration.executor.MigrationExecutor", Execution)
    result = CliRunner().invoke(
        cli.app, ["resume", "--job", str(store.directory), "--retry-unconfirmed-delete"], input="\n"
    )
    assert result.exit_code == 0, result.output
    assert retry_flags == [False]
