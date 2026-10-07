import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from spotify_local_migrator import cli
from spotify_local_migrator.errors import (
    PlaylistChangedError,
    RequestBudgetError,
    SpotifyAPIError,
    StateError,
)
from spotify_local_migrator.matching.config import MatchingConfig
from spotify_local_migrator.matching.engine import new_report
from spotify_local_migrator.matching.models import MatchDecision
from spotify_local_migrator.matching.normalize import prepare_track
from spotify_local_migrator.matching.search import candidate_from_api
from spotify_local_migrator.migration.executor import MigrationExecutor, load_plan
from spotify_local_migrator.migration.jobs import JobStore
from spotify_local_migrator.migration.planner import build_plan, identities
from spotify_local_migrator.migration.scanner import parse_entry

A, B, C, P = "B" * 22, "C" * 22, "D" * 22, "E" * 22


def raw_track(track_id, name="Elizabeth"):
    return {
        "id": track_id,
        "uri": "spotify:track:" + track_id,
        "type": "track",
        "name": name,
        "artists": [{"id": "artist", "name": "Westside Gunn"}],
        "album": {"name": "Supreme Blientele"},
        "duration_ms": 241000,
        "is_playable": True,
        "external_ids": {"isrc": track_id},
    }


@pytest.fixture
def job(settings, capture, raw_local):
    # Existing A is preserved even when two locals also map to A.
    local_b = deepcopy(raw_local)
    local_b["item"]["name"] = "Hall"
    local_b["item"]["uri"] = "spotify:local:Westside+Gunn::Hall:241"
    wrappers = [
        raw_local,
        {"is_local": False, "item": raw_track(A)},
        local_b,
        {"item": None, "is_local": False},
        deepcopy(raw_local),
    ]
    capture = capture.model_copy(deep=True)
    capture.entries = [parse_entry(raw, index) for index, raw in enumerate(wrappers)]
    capture.playlist.item_count = len(wrappers)
    report = new_report(capture, MatchingConfig(), "current-account", ["Westside Gunn"])
    for local in capture.local_tracks:
        candidate = candidate_from_api(raw_track(B if local.title == "Hall" else A, local.title))
        candidate.score = 1
        report.decisions.append(
            MatchDecision(
                local_track=local,
                prepared=prepare_track(local, ["Westside Gunn"], []),
                candidates=[candidate],
                candidate=candidate,
                status="approved",
                review_completed=True,
            )
        )
    report.matching_complete = True
    store = JobStore.create(settings.data_dir, capture)
    store.save("matches.json", report)
    plan = build_plan(capture, report)
    store.save("plan.json", plan)
    return store, capture, report, plan


class FakeSpotify:
    """In-memory API emulator with fault injection, including uncertain commits."""

    def __init__(self, capture):
        self.capture_template = capture
        self.original_id = capture.playlist.playlist_id
        self.entries = {self.original_id: deepcopy(capture.entries)}
        self.snapshots = {self.original_id: capture.snapshot_id}
        self.counter = 0
        self.occurrence_counter = 0
        for entry in self.entries[self.original_id]:
            self.occurrence_counter += 1
            entry.raw["_occurrence"] = self.occurrence_counter
        self.history = {
            (self.original_id, capture.snapshot_id): [
                entry.raw["_occurrence"] for entry in self.entries[self.original_id]
            ]
        }
        self.calls = []
        self.deleted_keys = set()
        self.auth = Mock()
        self.fail_before = None
        self.fail_after = None
        self.failure_status = None
        self.position_mode = "correct"
        self.bad_ack = False
        self.removed_probe = False
        self.unavailable = False
        self.scan_failure = False

    def current_user(self):
        return {"account_id": "current-account", "id": "legacy-account"}

    def track(self, track_id):
        raw = raw_track(track_id)
        raw["is_playable"] = not self.unavailable
        return raw

    def search_tracks(self, query):
        return {"tracks": {"items": [raw_track(C)], "next": None}}

    def create_probe_playlist(self, name):
        self.entries[P] = []
        self.snapshots[P] = "empty"
        self.history[(P, "empty")] = []
        self.calls.append(("create-probe", P))
        return {"id": P}

    def unfollow_probe_playlist(self, playlist_id):
        self.calls.append(("unfollow-probe", playlist_id))
        self.removed_probe = True

    def scan(self, playlist_id):
        if self.scan_failure and playlist_id == self.original_id:
            self.scan_failure = False
            raise SpotifyAPIError("readback failed")
        capture = self.capture_template.model_copy(deep=True)
        capture.playlist.playlist_id = playlist_id
        capture.entries = deepcopy(self.entries[playlist_id])
        for position, entry in enumerate(capture.entries):
            entry.playlist_position = position
        capture.snapshot_id = self.snapshots[playlist_id]
        capture.playlist.snapshot_id = capture.snapshot_id
        capture.playlist.item_count = len(capture.entries)
        return capture

    def _fail(self, kind, playlist_id, after=False):
        attr = "fail_after" if after else "fail_before"
        if playlist_id == self.original_id and getattr(self, attr) == kind:
            setattr(self, attr, None)
            raise SpotifyAPIError("injected failure", status_code=self.failure_status)

    def _snapshot(self, playlist_id):
        self.counter += 1
        self.snapshots[playlist_id] = "s" + str(self.counter)
        self.history[(playlist_id, self.snapshots[playlist_id])] = [
            entry.raw["_occurrence"] for entry in self.entries[playlist_id]
        ]
        return self.snapshots[playlist_id]

    def add_tracks(self, playlist_id, uris, position):
        self.calls.append(("add", playlist_id, position, uris))
        self._fail("add", playlist_id)
        for index, uri in enumerate(uris):
            raw = raw_track(uri.split(":")[-1])
            self.occurrence_counter += 1
            wrapper = {"item": raw, "_occurrence": self.occurrence_counter}
            self.entries[playlist_id].insert(position + index, parse_entry(wrapper, position))
        snapshot = self._snapshot(playlist_id)
        self._fail("add", playlist_id, after=True)
        if self.bad_ack and playlist_id == self.original_id:
            return "wrong-snapshot"
        return snapshot

    def remove_positions(self, playlist_id, positions, snapshot_id):
        self.calls.append(("delete", playlist_id, positions, snapshot_id))
        self._fail("delete", playlist_id)
        if playlist_id == P and self.position_mode == "reject":
            raise SpotifyAPIError("unsupported position request", status_code=400)
        key = (playlist_id, snapshot_id, tuple(positions))
        repeated = key in self.deleted_keys
        if not repeated or self.position_mode == "shift":
            if self.position_mode == "no-op" and playlist_id == P:
                pass
            elif self.position_mode == "all-duplicates" and playlist_id == P:
                uri = self.entries[P][positions[0]].uri
                self.entries[P] = [entry for entry in self.entries[P] if entry.uri != uri]
            else:
                if self.position_mode in ("shift", "idempotent-but-current-position"):
                    for position in sorted(positions, reverse=True):
                        del self.entries[playlist_id][position]
                else:
                    targets = {self.history[(playlist_id, snapshot_id)][p] for p in positions}
                    self.entries[playlist_id] = [
                        entry
                        for entry in self.entries[playlist_id]
                        if entry.raw["_occurrence"] not in targets
                    ]
            self.deleted_keys.add(key)
            snapshot = self._snapshot(playlist_id)
        else:
            snapshot = self.snapshots[playlist_id]
        self._fail("delete", playlist_id, after=True)
        return snapshot


def executor(job):
    store, capture, report, plan = job
    api = FakeSpotify(capture)
    return MigrationExecutor(api, scanner=api), api


def original_calls(api, kind=None):
    return [
        call
        for call in api.calls
        if call[1] == api.original_id and (kind is None or call[0] == kind)
    ]


def test_plan_descending_positions_duplicates_and_nulls(job):
    _, capture, _, plan = job
    assert [item.position for item in plan.replacements] == [4, 2, 0]
    assert len(plan.original) == len(plan.desired) == 5
    assert plan.desired[1].uri == "spotify:track:" + A
    assert plan.desired[3] == identities(capture)[3]
    assert [request.method for request in plan.requests] == ["POST", "DELETE"] * 3
    assert plan.requests[0].body == {"uris": ["spotify:track:" + A], "position": 4}
    assert plan.requests[1].body["positions"] == [5]
    assert all("spotify:local:" not in json.dumps(request.body) for request in plan.requests)
    assert plan.duplicate_warnings
    assert "final count 3" in plan.duplicate_warnings[0]


@pytest.mark.parametrize("field", ["matching_complete", "snapshot_id", "capture_hash"])
def test_invalid_matching_report_is_not_planned(job, field):
    _, capture, report, _ = job
    setattr(report, field, False if field == "matching_complete" else "altered")
    with pytest.raises(StateError):
        build_plan(capture, report)


def test_modified_local_metadata_rejected(job):
    _, capture, report, _ = job
    report.decisions[0].local_track = report.decisions[0].local_track.model_copy(
        update={"title": "tampered"}
    )
    with pytest.raises(StateError):
        build_plan(capture, report)


def test_apply_preserves_exact_order_and_occurrence_count(job):
    store, _, _, plan = job
    execution, api = executor(job)
    journal = execution.apply(store)
    assert journal.phase == "COMPLETE"
    assert identities(api.scan(api.original_id)) == plan.desired
    assert [call[2] for call in original_calls(api, "add")] == [4, 2, 0]
    assert [call[2] for call in original_calls(api, "delete")] == [[5], [3], [1]]
    assert len(journal.operations) == 6
    assert api.removed_probe
    calls_before = len(api.calls)
    execution.apply(store)
    assert len(api.calls) == calls_before  # Completed resume has no mutations.


@pytest.mark.parametrize(
    "mode", ["reject", "no-op", "all-duplicates", "shift", "idempotent-but-current-position"]
)
def test_incompatible_removal_api_never_touches_original(job, mode):
    store, _, _, plan = job
    execution, api = executor(job)
    api.position_mode = mode
    with pytest.raises(StateError, match="compatibility check failed"):
        execution.apply(store)
    assert not original_calls(api)
    assert identities(api.scan(api.original_id)) == plan.original
    assert not (store.directory / "migration.json").exists()
    assert api.removed_probe


@pytest.mark.parametrize("kind", ["add", "delete"])
def test_resume_after_committed_request_with_lost_response(job, kind):
    store, _, _, plan = job
    execution, api = executor(job)
    api.fail_after = kind
    with pytest.raises(SpotifyAPIError):
        execution.apply(store)
    state = json.loads((store.directory / "migration.json").read_text())
    assert state["pending"]["kind"] == kind
    execution.apply(store)
    assert identities(api.scan(api.original_id)) == plan.desired
    assert len(original_calls(api, "add")) == 3
    assert len(original_calls(api, "delete")) == 3


def test_unconfirmed_uncommitted_add_stops_until_explicit_retry(job):
    store, _, _, plan = job
    execution, api = executor(job)
    api.fail_before = "add"
    with pytest.raises(SpotifyAPIError):
        execution.apply(store)
    with pytest.raises(StateError, match="unconfirmed"):
        execution.apply(store)
    assert len(original_calls(api, "add")) == 1
    assert not original_calls(api, "delete")
    execution.apply(store, retry_unconfirmed_add=True)
    assert identities(api.scan(api.original_id)) == plan.desired


def test_unconfirmed_uncommitted_delete_retries_same_snapshot(job):
    store, _, _, plan = job
    execution, api = executor(job)
    api.fail_before = "delete"
    with pytest.raises(SpotifyAPIError):
        execution.apply(store)
    execution.apply(store)
    deletes = original_calls(api, "delete")
    assert deletes[0][3] == deletes[1][3]
    assert len(original_calls(api, "add")) == 3
    assert identities(api.scan(api.original_id)) == plan.desired


@pytest.mark.parametrize(
    "kind,status", [("add", 403), ("add", 429), ("delete", 403), ("delete", 429)]
)
def test_explicit_rejection_is_retryable_without_duplicate(job, kind, status):
    store, _, _, plan = job
    execution, api = executor(job)
    api.fail_before = kind
    api.failure_status = status
    with pytest.raises(SpotifyAPIError):
        execution.apply(store)
    assert json.loads((store.directory / "migration.json").read_text())["pending"] is None
    execution.apply(store)
    assert identities(api.scan(api.original_id)) == plan.desired


@pytest.mark.parametrize("kind", ["add", "delete"])
def test_local_budget_pause_resumes_without_unconfirmed_insertion(job, monkeypatch, kind):
    store, _, _, plan = job
    execution, api = executor(job)
    dispatch = api.add_tracks if kind == "add" else api.remove_positions
    paused = False

    def pause_once(playlist_id, *args):
        nonlocal paused
        if playlist_id == api.original_id and not paused:
            paused = True
            raise RequestBudgetError("Local budget reached; request was not dispatched.")
        return dispatch(playlist_id, *args)

    monkeypatch.setattr(api, "add_tracks" if kind == "add" else "remove_positions", pause_once)
    with pytest.raises(RequestBudgetError):
        execution.apply(store)
    journal = json.loads((store.directory / "migration.json").read_text())
    assert journal["pending"] is None
    execution.apply(store)
    assert identities(api.scan(api.original_id)) == plan.desired


def test_response_acknowledgement_must_match_verified_snapshot(job):
    store, _, _, _ = job
    execution, api = executor(job)
    api.bad_ack = True
    with pytest.raises(PlaylistChangedError):
        execution.apply(store)
    assert len(original_calls(api, "add")) == 1
    assert not original_calls(api, "delete")


def test_external_edit_before_apply_prevents_all_writes(job):
    store, _, _, _ = job
    execution, api = executor(job)
    api.snapshots[api.original_id] = "someone-edited"
    with pytest.raises(PlaylistChangedError):
        execution.apply(store)
    assert api.calls == []


def test_external_edit_during_interruption_prevents_further_writes(job):
    store, _, _, _ = job
    execution, api = executor(job)
    api.fail_after = "add"
    with pytest.raises(SpotifyAPIError):
        execution.apply(store)
    api.entries[api.original_id].reverse()
    api._snapshot(api.original_id)
    before = len(api.calls)
    with pytest.raises(PlaylistChangedError):
        execution.apply(store)
    assert len(api.calls) == before
    assert not original_calls(api, "delete")


def test_unavailable_candidate_keeps_local(job):
    store, _, _, plan = job
    execution, api = executor(job)
    api.unavailable = True
    with pytest.raises(StateError, match="no longer available"):
        execution.apply(store)
    assert not original_calls(api)
    assert identities(api.scan(api.original_id)) == plan.original


def test_crash_before_ack_save_reconciles_without_duplicate(job, monkeypatch):
    store, _, _, plan = job
    execution, api = executor(job)
    original_save = store.save
    crashed = False

    def crash(name, data):
        nonlocal crashed
        if (
            name == "migration.json"
            and data.pending
            and data.pending.acknowledged_snapshot
            and not crashed
        ):
            crashed = True
            raise SystemExit("killed before acknowledgement save")
        original_save(name, data)

    monkeypatch.setattr(store, "save", crash)
    with pytest.raises(SystemExit):
        execution.apply(store)
    monkeypatch.setattr(store, "save", original_save)
    execution.apply(store)
    assert len(original_calls(api, "add")) == 3
    assert identities(api.scan(api.original_id)) == plan.desired


def test_capture_and_plan_tampering_detected(job):
    store, _, _, _ = job
    plan = load_plan(store)
    plan.replacements[0].position = 0
    store.save("plan.json", plan)
    execution, api = executor(job)
    with pytest.raises(StateError, match="altered"):
        execution.apply(store)
    assert api.calls == []


def test_offline_dry_run_generates_plan_without_services(job, settings, monkeypatch):
    store, _, _, _ = job
    monkeypatch.setattr(cli, "load_settings", lambda env_file: settings)

    def forbidden(*args):
        raise AssertionError("No API or OAuth required for saved-job dry run.")

    monkeypatch.setattr(cli, "services", forbidden)
    result = CliRunner().invoke(
        cli.app,
        [
            "migrate",
            "--job",
            str(store.directory),
            "--dry-run",
            "--no-review",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "No Spotify mutations" in result.output
    assert not (store.directory / "migration.json").exists()


def test_apply_confirmation_defaults_to_no(job, settings, monkeypatch):
    store, _, _, _ = job
    monkeypatch.setattr(cli, "load_settings", lambda env_file: settings)

    def forbidden(*args):
        raise AssertionError("Default No must not open API write services.")

    monkeypatch.setattr(cli, "services", forbidden)
    result = CliRunner().invoke(
        cli.app, ["migrate", "--job", str(store.directory), "--no-review"], input="\n"
    )
    assert result.exit_code == 0, result.output
    assert "Playlist unchanged" in result.output
    assert not (store.directory / "migration.json").exists()


def test_process_locks_prevent_overlapping_apply(job):
    from spotify_local_migrator.migration.executor import playlist_lock

    store, _, _, _ = job
    with store.lock():
        with pytest.raises(StateError, match="Another process"):
            with store.lock():
                pass
    with playlist_lock(store):
        with pytest.raises(StateError, match="Another migration"):
            with playlist_lock(store):
                pass
