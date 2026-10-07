import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from spotify_local_migrator.config import READ_SCOPES, WRITE_SCOPES
from spotify_local_migrator.errors import AuthenticationError, SpotifyAPIError

PLAYLIST = "A" * 22
TRACK = "spotify:track:" + "B" * 22


def allow_writes(auth):
    token = auth.store.load()
    token.scopes = list(READ_SCOPES + WRITE_SCOPES)
    auth.store.save(token)


def test_write_login_is_explicit_and_keeps_pkce(make_api):
    _, auth, _ = make_api(lambda request: httpx.Response(500))
    read = parse_qs(urlsplit(auth.begin_login().url).query)
    write = parse_qs(urlsplit(auth.begin_login(write=True).url).query)
    assert set(read["scope"][0].split()) == set(READ_SCOPES)
    assert set(write["scope"][0].split()) == set(READ_SCOPES + WRITE_SCOPES)
    assert write["code_challenge_method"] == ["S256"]


def test_missing_write_scopes_blocks_before_request(make_api):
    calls = []
    api, _, _ = make_api(lambda request: calls.append(request))
    with pytest.raises(AuthenticationError, match="login --write"):
        api.add_tracks(PLAYLIST, [TRACK], 0)
    assert calls == []


def test_insert_uses_new_endpoint_and_position(make_api):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(201, json={"snapshot_id": "added"})

    api, auth, _ = make_api(handler)
    allow_writes(auth)
    assert api.add_tracks(PLAYLIST, [TRACK], 3) == "added"
    assert calls[0].method == "POST"
    assert calls[0].url.path == f"/v1/playlists/{PLAYLIST}/items"
    assert json.loads(calls[0].content) == {"uris": [TRACK], "position": 3}


def test_delete_is_position_only_and_binds_snapshot(make_api):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"snapshot_id": "deleted"})

    api, auth, _ = make_api(handler)
    allow_writes(auth)
    api.remove_positions(PLAYLIST, [4], "verified-post-add")
    assert calls[0].method == "DELETE"
    payload = json.loads(calls[0].content)
    assert payload == {"items": [], "positions": [4], "snapshot_id": "verified-post-add"}
    assert "uri" not in payload


def test_private_probe_creation_and_current_unfollow_endpoint(make_api):
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"id": PLAYLIST})
        return httpx.Response(200)

    api, auth, _ = make_api(handler)
    allow_writes(auth)
    api.create_probe_playlist("test")
    api.unfollow_probe_playlist(PLAYLIST)
    assert calls[0].url.path == "/v1/me/playlists"
    assert json.loads(calls[0].content)["public"] is False
    assert calls[1].url.path == "/v1/me/library"
    assert calls[1].url.params["uris"] == "spotify:playlist:" + PLAYLIST


@pytest.mark.parametrize("failure", ["network", "500", "invalid-json", "missing-snapshot"])
def test_uncertain_mutation_not_blindly_retried(make_api, failure):
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "network":
            raise httpx.ReadTimeout("response lost", request=request)
        if failure == "500":
            return httpx.Response(500)
        if failure == "invalid-json":
            return httpx.Response(201, text="not JSON")
        return httpx.Response(201, json={})

    api, auth, _ = make_api(handler)
    allow_writes(auth)
    with pytest.raises(SpotifyAPIError):
        api.add_tracks(PLAYLIST, [TRACK], 0)
    assert len(calls) == 1


def test_write_429_respects_retry_after(make_api):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={"error": {}})
        return httpx.Response(201, json={"snapshot_id": "added"})

    api, auth, waits = make_api(handler)
    allow_writes(auth)
    api.add_tracks(PLAYLIST, [TRACK], 0)
    assert waits == [3]
    assert len(calls) == 2


def test_write_401_refreshes_once_without_logging_tokens(make_api, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "accounts.spotify.com":
            return httpx.Response(
                200,
                json={
                    "access_token": "renewed-secret",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        if request.headers["Authorization"] == "Bearer access-secret":
            return httpx.Response(401)
        return httpx.Response(201, json={"snapshot_id": "added"})

    api, auth, _ = make_api(handler)
    allow_writes(auth)
    api.add_tracks(PLAYLIST, [TRACK], 0)
    assert len(calls) == 3
    assert "renewed-secret" not in caplog.text


@pytest.mark.parametrize(
    "uri", ["spotify:local:artist::song:100", "spotify:episode:" + "B" * 22, "bad"]
)
def test_local_and_invalid_uris_cannot_be_inserted(make_api, uri):
    calls = []
    api, auth, _ = make_api(lambda request: calls.append(request))
    allow_writes(auth)
    with pytest.raises(SpotifyAPIError):
        api.add_tracks(PLAYLIST, [uri], 0)
    assert not calls


@pytest.mark.parametrize("positions", [[], [-1], [True], ["4"]])
def test_invalid_positions_rejected(make_api, positions):
    calls = []
    api, auth, _ = make_api(lambda request: calls.append(request))
    allow_writes(auth)
    with pytest.raises(SpotifyAPIError):
        api.remove_positions(PLAYLIST, positions, "snapshot")
    assert not calls


def test_write_login_rejects_missing_granted_scopes(make_api, token_body):
    _, auth, _ = make_api(lambda request: httpx.Response(200, json=token_body))
    request = auth.begin_login(write=True)
    callback = request.redirect_uri + "?code=test&state=" + request.state
    with pytest.raises(AuthenticationError, match="permissions were not granted"):
        auth.finish_login(request, callback)
