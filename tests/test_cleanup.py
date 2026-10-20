import json
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import Mock
from urllib.parse import quote_plus

import pytest
from rich.console import Console
from test_migration import A, B, C, FakeSpotify, original_calls, raw_track
from test_migration import job as job
from typer.testing import CliRunner

from spotify_local_migrator import cli
from spotify_local_migrator.errors import (
    PlaylistChangedError,
    RateLimitError,
    SpotifyAPIError,
    StateError,
)
from spotify_local_migrator.matching.cleanup import (
    PlaylistSearch,
    cleanup_report,
    refresh_cleanup_choices,
)
from spotify_local_migrator.matching.config import MatchingConfig
from spotify_local_migrator.migration.executor import MigrationExecutor, load_plan
from spotify_local_migrator.migration.jobs import JobStore, content_hash, latest_job
from spotify_local_migrator.migration.planner import build_plan, identities
from spotify_local_migrator.migration.scanner import parse_entry
from spotify_local_migrator.ui.review import review_report
from spotify_local_migrator.workflow import cleanup_job


def local_copy(raw_local, name="Elizabeth"):
    wrapper = deepcopy(raw_local)
    wrapper["item"]["name"] = name
    wrapper["item"]["uri"] = "spotify:local:Westside+Gunn::" + quote_plus(name) + ":241"
    return wrapper


def mixed(capture, wrappers):
    capture = capture.model_copy(deep=True)
    capture.entries = [parse_entry(raw, index) for index, raw in enumerate(wrappers)]
    capture.playlist.item_count = len(capture.entries)
    return capture


def case(settings, capture, wrappers):
    capture = mixed(capture, wrappers)
    report = cleanup_report(capture, MatchingConfig(), "current-account")
    store = JobStore.create(settings.data_dir, capture)
    store.save("matches.json", report)
    plan = build_plan(capture, report)
    store.save("plan.json", plan)
    api = FakeSpotify(capture)
    return store, capture, report, plan, api


def spotify(track_id=A, title="Elizabeth"):
    return {"item": raw_track(track_id, title), "is_local": False}


def test_cleanup_plan_removes_only_local_copies_and_preserves_every_other_occurrence(
    settings, capture, raw_local
):
    wrappers = [
        local_copy(raw_local),
        spotify(),
        {"item": None},
        {"item": {"type": "episode", "uri": "spotify:episode:" + C}},
        local_copy(raw_local, "Not On Spotify"),
        spotify(),
        local_copy(raw_local),
    ]
    _, capture, report, plan, _ = case(settings, capture, wrappers)
    assert report.operation == plan.operation == "cleanup"
    assert [item.position for item in plan.replacements] == [6, 0]
    assert plan.desired == [identities(capture)[i] for i in (1, 2, 3, 4, 5)]
    assert [request.method for request in plan.requests] == ["DELETE", "DELETE"]
    assert [request.body["positions"] for request in plan.requests] == [[6], [0]]
    assert "spotify:local:" not in json.dumps([request.body for request in plan.requests])


@pytest.mark.parametrize(
    "locals_count,spotify_count", [(1, 0), (1, 1), (2, 1), (2, 2), (3, 2), (1, 3)]
)
def test_cleanup_pairs_occurrences_one_for_one(
    settings, capture, raw_local, locals_count, spotify_count
):
    wrappers = [local_copy(raw_local) for _ in range(locals_count)] + [
        spotify() for _ in range(spotify_count)
    ]
    _, _, _, plan, _ = case(settings, capture, wrappers)
    assert len(plan.replacements) == min(locals_count, spotify_count)
    assert sum(entry.is_local for entry in plan.desired) == max(0, locals_count - spotify_count)
    assert Counter(entry.uri for entry in plan.desired if not entry.is_local) == Counter(
        {"spotify:track:" + A: spotify_count}
    )


@pytest.mark.parametrize(
    "name,artist", [("Unrelated", "Westside Gunn"), ("Elizabeth", "Different Artist")]
)
def test_weak_or_conflicting_identity_does_not_select_an_automatic_deletion(
    settings, capture, raw_local, name, artist
):
    counterpart = spotify(title=name)
    counterpart["item"]["artists"] = [{"name": artist}]
    _, _, report, plan, _ = case(settings, capture, [local_copy(raw_local), counterpart])
    assert not plan.replacements
    if name == "Elizabeth":
        assert report.decisions[0].needs_review


@pytest.mark.parametrize("available", [False, None])
def test_unavailable_or_unconfirmed_counterpart_does_not_select_deletion(
    settings, capture, raw_local, available
):
    counterpart = spotify()
    counterpart["item"]["is_playable"] = available
    _, _, _, plan, _ = case(settings, capture, [local_copy(raw_local), counterpart])
    assert not plan.replacements


def test_cleanup_reuses_embedded_artist_cleanup(settings, capture, raw_local):
    wrapper = local_copy(raw_local, "Westside Gunn - Elizabeth")
    wrapper["item"]["artists"] = [{"name": "Uploader"}]
    _, _, report, plan, _ = case(settings, capture, [wrapper, spotify()])
    assert report.decisions[0].prepared.title == "Elizabeth"
    assert len(plan.replacements) == 1


def test_plan_rejects_outside_playlist_candidates_and_overallocated_pairs(
    settings, capture, raw_local
):
    _, capture, report, _, _ = case(
        settings, capture, [local_copy(raw_local), local_copy(raw_local), spotify()]
    )
    selected = report.decisions[0].candidate
    report.decisions[1].candidate = selected
    report.decisions[1].status = "approved"
    with pytest.raises(StateError, match="unused Spotify occurrence"):
        build_plan(capture, report)
    report.decisions[1].candidate = None
    report.decisions[1].status = "unmatched"
    report.decisions[0].candidate = selected.model_copy(
        update={"spotify_id": B, "uri": "spotify:track:" + B}
    )
    with pytest.raises(StateError, match="unused Spotify occurrence"):
        build_plan(capture, report)


def test_rejecting_a_pair_frees_it_for_another_local_copy(settings, capture, raw_local):
    _, capture, report, _, _ = case(
        settings, capture, [local_copy(raw_local), local_copy(raw_local), spotify()]
    )
    assert report.decisions[0].candidate
    assert not report.decisions[1].candidate
    report.decisions[0].status = "rejected"
    report.decisions[0].candidate = None
    report.decisions[0].review_completed = True
    refresh_cleanup_choices(report, capture)
    assert report.decisions[0].status == "rejected"
    assert report.decisions[1].needs_review
    assert report.decisions[1].candidate is None


def test_review_consumes_one_counterpart_and_does_not_ask_for_the_second_copy(
    settings, capture, raw_local, monkeypatch
):
    counterpart = spotify()
    counterpart["item"]["artists"] = [{"name": "Different Artist"}]
    store, capture, report, _, _ = case(
        settings, capture, [local_copy(raw_local), local_copy(raw_local), counterpart]
    )
    calls = []

    def accept(*args, **kwargs):
        calls.append(1)
        return 1

    monkeypatch.setattr("rich.prompt.IntPrompt.ask", accept)
    review_report(Console(quiet=True), store, report, None)
    saved = store.report()
    assert len(calls) == 1
    assert saved.decisions[0].status == "approved"
    assert saved.decisions[1].candidate is None and not saved.decisions[1].needs_review
    assert len(build_plan(capture, saved).replacements) == 1


def test_manual_lookup_stays_inside_playlist_and_releases_only_current_pair(
    settings, capture, raw_local
):
    _, capture, report, _, _ = case(
        settings, capture, [local_copy(raw_local), spotify(), spotify(B, "Hall")]
    )
    search = PlaylistSearch(report, capture)
    assert not search.query("Elizabeth")  # Reserved for another occurrence.
    search.position = 0
    assert [candidate.spotify_id for candidate in search.query("Elizabeth")] == [A]
    assert [candidate.spotify_id for candidate in search.query("Westside Hall")] == [B]
    assert not search.query("Outside Playlist")


def test_apply_cleanup_never_adds_to_original_and_keeps_catalogue_duplicates(
    settings, capture, raw_local
):
    store, capture, _, plan, api = case(
        settings,
        capture,
        [local_copy(raw_local), spotify(), local_copy(raw_local), spotify(), {"item": None}],
    )
    api.position_mode = "shift"
    journal = MigrationExecutor(api, scanner=api).apply(store)
    assert journal.phase == "COMPLETE"
    assert identities(api.scan(api.original_id)) == plan.desired
    assert not original_calls(api, "add")
    assert [call[2] for call in original_calls(api, "delete")] == [[2], [0]]
    assert [operation["position"] for operation in journal.operations] == [2, 0]
    assert all(operation["kind"] == "delete" for operation in journal.operations)
    before = len(api.calls)
    MigrationExecutor(api, scanner=api).apply(store)
    assert len(api.calls) == before


@pytest.mark.parametrize("committed", [False, True])
def test_cleanup_resume_reconciles_deletion_without_blind_replay(
    settings, capture, raw_local, committed
):
    store, _, _, plan, api = case(
        settings, capture, [local_copy(raw_local), spotify(), local_copy(raw_local), spotify()]
    )
    api.position_mode = "shift"
    api.fail_after = "delete" if committed else None
    api.fail_before = None if committed else "delete"
    execution = MigrationExecutor(api, scanner=api)
    with pytest.raises(SpotifyAPIError):
        execution.apply(store)
    if not committed:
        with pytest.raises(StateError, match="No deletion was replayed"):
            execution.apply(store)
        assert len(original_calls(api, "delete")) == 1
    result = execution.apply(store, retry_unconfirmed_delete=not committed)
    assert result.phase == "COMPLETE"
    assert identities(api.scan(api.original_id)) == plan.desired
    assert len(original_calls(api, "delete")) == len(plan.replacements) + (not committed)
    assert not original_calls(api, "add")


def test_cleanup_cooldown_rechecks_positions_before_retry(
    settings, capture, raw_local, monkeypatch
):
    store, _, _, plan, api = case(settings, capture, [local_copy(raw_local), spotify()])
    api.settings.wait_for_rate_limits = True
    api.wait_for_cooldown = Mock()
    remove = api.remove_positions
    rejected = False

    def reject_once(playlist_id, *args):
        nonlocal rejected
        if playlist_id == api.original_id and not rejected:
            rejected = True
            raise RateLimitError("quota", retry_after=86400)
        return remove(playlist_id, *args)

    monkeypatch.setattr(api, "remove_positions", reject_once)
    assert MigrationExecutor(api, scanner=api).apply(store).phase == "COMPLETE"
    api.wait_for_cooldown.assert_called_once()
    assert identities(api.scan(api.original_id)) == plan.desired
    assert len(original_calls(api, "delete")) == 1


def test_cleanup_stops_if_another_app_changes_the_playlist(settings, capture, raw_local):
    store, _, _, _, api = case(settings, capture, [local_copy(raw_local), spotify()])
    api.entries[api.original_id].reverse()
    api._snapshot(api.original_id)
    with pytest.raises(PlaylistChangedError):
        MigrationExecutor(api, scanner=api).apply(store)
    assert not original_calls(api)


def test_existing_migration_serialization_and_hashes_do_not_change(job):
    store, _, report, plan = job
    assert "operation" not in report.model_dump(mode="json")
    assert "operation" not in plan.model_dump(mode="json")
    assert content_hash(store.report()) == plan.matches_hash
    assert load_plan(store) == plan


def setup_cli(settings, api, monkeypatch):
    @contextmanager
    def services(_):
        yield api.auth, api

    monkeypatch.setattr(cli, "load_settings", lambda _: settings)
    monkeypatch.setattr(cli, "services", services)
    monkeypatch.setattr(
        "spotify_local_migrator.migration.executor.PlaylistScanner", lambda client: api
    )


def test_cli_preview_is_read_only_and_apply_deletes_without_inserting(
    settings, capture, raw_local, monkeypatch
):
    store, _, _, plan, api = case(settings, capture, [local_copy(raw_local), spotify()])
    setup_cli(settings, api, monkeypatch)
    result = CliRunner().invoke(cli.app, ["cleanup", "--job", str(store.directory), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Add Spotify: 0" in result.output and "Delete local: 1" in result.output
    assert not api.calls
    result = CliRunner().invoke(cli.app, ["cleanup", "--job", str(store.directory)], input="y\n")
    assert result.exit_code == 0, result.output
    assert "local copies removed and verified" in result.output
    assert identities(api.scan(api.original_id)) == plan.desired
    assert not original_calls(api, "add")


def test_cleanup_preparation_checks_missing_availability_once_and_never_searches_catalogue(
    settings, capture, raw_local, monkeypatch
):
    counterpart = spotify()
    counterpart["item"].pop("is_playable")
    original = mixed(capture, [local_copy(raw_local), counterpart, local_copy(raw_local)])
    api = FakeSpotify(original)
    lookup = Mock(wraps=api.track)
    api.track = lookup
    api.search_tracks = lambda *args, **kwargs: pytest.fail("Cleanup matching searched catalogue")
    monkeypatch.setattr(
        "spotify_local_migrator.workflow.PlaylistScanner", lambda *args, **kwargs: api
    )
    store = cleanup_job(
        settings, api, Console(quiet=True), playlist_id=original.playlist.playlist_id
    )
    lookup.assert_called_once_with(A)
    assert len(build_plan(store.capture(), store.report()).replacements) == 1
    assert not api.calls


def test_cli_new_cleanup_scans_current_playlist_and_saves_plan(
    settings, capture, raw_local, monkeypatch
):
    original = mixed(capture, [local_copy(raw_local), spotify()])
    api = FakeSpotify(original)
    api.search_tracks = lambda *args, **kwargs: pytest.fail("Cleanup matching searched catalogue")
    setup_cli(settings, api, monkeypatch)
    monkeypatch.setattr(
        "spotify_local_migrator.workflow.PlaylistScanner", lambda *args, **kwargs: api
    )
    result = CliRunner().invoke(
        cli.app, ["cleanup", "-p", original.playlist.playlist_id, "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    store = latest_job(settings.data_dir, operation="cleanup")
    assert store.report().operation == "cleanup"
    assert len(load_plan(store).replacements) == 1
    assert not api.calls


def test_cli_rejects_mixed_job_operations_and_latest_filters_cleanup(
    job, settings, capture, raw_local, monkeypatch
):
    migration_store = job[0]
    cleanup_store, _, _, _, api = case(settings, capture, [local_copy(raw_local), spotify()])
    setup_cli(settings, api, monkeypatch)
    for args in (
        ["cleanup", "--job", str(migration_store.directory)],
        ["migrate", "--job", str(cleanup_store.directory)],
    ):
        result = CliRunner().invoke(cli.app, args)
        assert result.exit_code == 1, result.output
    result = CliRunner().invoke(cli.app, ["cleanup", "--latest", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert not api.calls


def test_offline_cleanup_review_works_without_opening_services(
    settings, capture, raw_local, monkeypatch
):
    counterpart = spotify()
    counterpart["item"]["artists"] = [{"name": "Different Artist"}]
    store, _, _, _, _ = case(settings, capture, [local_copy(raw_local), counterpart])
    monkeypatch.setattr(cli, "load_settings", lambda _: settings)
    monkeypatch.setattr(cli, "services", lambda *args: pytest.fail("Offline cleanup used services"))
    result = CliRunner().invoke(
        cli.app, ["review", "--offline", "--job", str(store.directory)], input="\n"
    )
    assert result.exit_code == 0, result.output
    assert store.report().decisions[0].status == "approved"


def test_cleanup_with_no_locals_has_no_removals(settings, capture):
    _, _, report, plan, _ = case(settings, capture, [spotify(), spotify(), {"item": None}])
    assert report.matching_complete
    assert not report.decisions and not plan.replacements and not plan.requests
    assert plan.original == plan.desired


def test_cleanup_with_stale_metadata_snapshots_keeps_acknowledgments_for_next_delete(
    settings, capture, raw_local, monkeypatch
):
    store, _, _, plan, api = case(
        settings, capture, [local_copy(raw_local), spotify(), local_copy(raw_local), spotify()]
    )
    api.position_mode = "shift"
    scan = api.scan

    def stale(playlist_id):
        result = scan(playlist_id)
        observed = capture.snapshot_id if playlist_id == api.original_id else "cached-probe"
        result.snapshot_id = result.playlist.snapshot_id = observed
        return result

    monkeypatch.setattr(api, "scan", stale)
    journal = MigrationExecutor(api, scanner=api).apply(store)
    assert identities(api.scan(api.original_id)) == plan.desired
    calls = original_calls(api, "delete")
    assert len(calls) == 2
    assert calls[1][3] == journal.operations[0]["acknowledged_snapshot"]
    assert not original_calls(api, "add")


def test_explicit_cleanup_delete_rejection_can_resume_without_unconfirmed_retry(
    settings, capture, raw_local
):
    store, _, _, plan, api = case(settings, capture, [local_copy(raw_local), spotify()])
    api.fail_before = "delete"
    api.failure_status = 403
    execution = MigrationExecutor(api, scanner=api)
    with pytest.raises(SpotifyAPIError):
        execution.apply(store)
    state = json.loads((store.directory / "migration.json").read_text())
    assert state["pending"] is None and state["next_replacement"] == 0
    assert execution.apply(store).phase == "COMPLETE"
    assert identities(api.scan(api.original_id)) == plan.desired
    assert not original_calls(api, "add")


def test_declining_cleanup_apply_leaves_the_playlist_untouched(
    settings, capture, raw_local, monkeypatch
):
    store, _, _, plan, api = case(settings, capture, [local_copy(raw_local), spotify()])
    setup_cli(settings, api, monkeypatch)
    result = CliRunner().invoke(cli.app, ["cleanup", "--job", str(store.directory)], input="n\n")
    assert result.exit_code == 0, result.output
    assert not api.calls
    assert identities(api.scan(api.original_id)) == plan.original
    assert not (store.directory / "migration.json").exists()
