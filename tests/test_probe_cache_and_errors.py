import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_migration import executor, original_calls
from test_migration import job as job
from test_write_api import PLAYLIST, TRACK, allow_writes

from spotify_local_migrator.errors import SpotifyAPIError, StateError
from spotify_local_migrator.migration.planner import identities


def test_write_403_reports_sanitized_spotify_message_and_endpoint(make_api, caplog):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            403,
            json={
                "error": {
                    "message": "Insufficient client scope\nBearer access-secret https://example.com/private",
                    "reason": "INSUFFICIENT_SCOPE",
                    "secret": "never-print",
                },
                "other_private_field": "never-print",
            },
        )

    api, auth, _ = make_api(handler)
    allow_writes(auth)
    with pytest.raises(SpotifyAPIError) as error:
        api.add_tracks(PLAYLIST, [TRACK], 0)
    assert error.value.status_code == 403
    assert error.value.reason == "INSUFFICIENT_SCOPE"
    assert "Insufficient client scope" in str(error.value)
    assert f"/playlists/{PLAYLIST}/items" in str(error.value)
    assert "automatically repeated" in str(error.value)
    assert "access-secret" not in str(error.value) + caplog.text
    assert "never-print" not in str(error.value) + caplog.text
    assert "https://example.com/private" not in str(error.value)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "body", ["Forbidden", "<html>secret body</html>", "[]", '{"error":"secret"}']
)
def test_write_denials_do_not_expose_raw_error_bodies(make_api, body):
    api, auth, _ = make_api(lambda request: httpx.Response(403, text=body))
    allow_writes(auth)
    with pytest.raises(SpotifyAPIError) as error:
        api.add_tracks(PLAYLIST, [TRACK], 0)
    assert error.value.status_code == 403
    assert "secret" not in str(error.value)
    assert error.value.api_message is None


def test_successful_probe_is_reused_without_creating_or_mutating_another_playlist(job):
    store, _, _, plan = job
    execution, api = executor(job)
    execution.preflight(store, plan)
    previous = len(api.calls)
    execution.preflight(store, plan.model_copy())
    assert len(api.calls) == previous
    assert (store.directory / "compatibility.json").stat().st_mode & 0o777 == 0o600
    proof = json.loads((store.directory / "compatibility.json").read_text())
    assert proof["phase"] == "PASSED"
    assert proof["account_id"] == plan.account_id
    assert proof["matches_hash"] == plan.matches_hash


@pytest.mark.parametrize(
    "change",
    [
        {"account_id": "someone-else"},
        {"client_id": "different-app"},
        {"matches_hash": "changed"},
        {"capture_hash": "changed"},
        {"compatibility_version": 99},
        {"phase": "FAILED"},
        {"checks": {"single_occurrence": True, "nonzero_position": False}},
        {"tested_at": "not-a-date"},
    ],
)
def test_cached_check_cannot_bypass_account_app_job_or_capability_validation(job, change):
    store, _, _, plan = job
    execution, api = executor(job)
    execution.preflight(store, plan)
    proof = json.loads((store.directory / "compatibility.json").read_text())
    proof.update(change)
    store.save("compatibility.json", proof)
    assert execution._cached_check(store, plan) is None
    previous = len(api.calls)
    execution.preflight(store, plan)
    assert len(api.calls) > previous


def test_expired_check_requires_new_probe_for_apply_but_can_resume_started_job(job):
    store, _, _, plan = job
    execution, _ = executor(job)
    execution.preflight(store, plan)
    proof = json.loads((store.directory / "compatibility.json").read_text())
    proof["tested_at"] = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    store.save("compatibility.json", proof)
    assert execution._cached_check(store, plan) is None
    assert execution._cached_check(store, plan, allow_expired=True) == proof


def test_later_failed_probe_does_not_destroy_successful_record(job):
    store, _, _, plan = job
    execution, api = executor(job)
    execution.preflight(store, plan)
    proof = json.loads((store.directory / "compatibility.json").read_text())
    proof["tested_at"] = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    store.save("compatibility.json", proof)
    api.position_mode = "no-op"
    with pytest.raises(StateError):
        execution.preflight(store, plan)
    assert json.loads((store.directory / "compatibility.json").read_text()) == proof
    assert json.loads((store.directory / "probe.json").read_text())["phase"] == "FAILED"


def test_apply_uses_successful_proof_even_if_last_attempt_record_failed(job):
    store, _, _, plan = job
    execution, api = executor(job)
    execution.preflight(store, plan)
    store.save("probe.json", {"phase": "FAILED", "reason": "later access denial"})
    execution.apply(store)
    assert sum(call[0] == "create-probe" for call in api.calls) == 1
    assert identities(api.scan(api.original_id)) == plan.desired


def test_probe_and_cleanup_record_denial_details_without_touching_original(job, monkeypatch):
    store, _, _, plan = job
    execution, api = executor(job)
    add = api.add_tracks
    additions = 0

    def denied_shift(playlist_id, uris, position):
        nonlocal additions
        additions += 1
        if additions == 2:
            raise SpotifyAPIError(
                "Spotify: write access denied.",
                403,
                api_message="write access denied",
                reason="FORBIDDEN",
            )
        return add(playlist_id, uris, position)

    def denied_cleanup(playlist_id):
        raise SpotifyAPIError("Spotify: cleanup denied.", 403, api_message="cleanup denied")

    monkeypatch.setattr(api, "add_tracks", denied_shift)
    monkeypatch.setattr(api, "unfollow_probe_playlist", denied_cleanup)
    with pytest.raises(StateError, match="write access denied"):
        execution.apply(store)
    probe = json.loads((store.directory / "probe.json").read_text())
    assert probe["failed_operation"] == "insert position-shift test track"
    assert probe["status_code"] == 403
    assert probe["api_message"] == "write access denied"
    assert probe["api_reason"] == "FORBIDDEN"
    assert "cleanup denied" in probe["cleanup_error"]
    assert probe["cleanup_status_code"] == 403
    assert not probe["removed_from_library"]
    assert not original_calls(api)
    assert not (store.directory / "compatibility.json").exists()
