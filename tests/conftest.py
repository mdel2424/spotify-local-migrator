from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from spotify_local_migrator.config import READ_SCOPES, Settings
from spotify_local_migrator.migration.scanner import parse_entry
from spotify_local_migrator.models import PlaylistCapture, PlaylistSummary, TokenData
from spotify_local_migrator.spotify.auth import SpotifyAuth, TokenStore
from spotify_local_migrator.spotify.client import SpotifyClient

PLAYLIST_ID = "A" * 22


@pytest.fixture(autouse=True)
def clear_config_environment(monkeypatch):
    for key in (
        "SPOTIFY_CLIENT_ID",
        "SPOTIFY_REDIRECT_URI",
        "SPOTIFY_DATA_DIR",
        "SPOTIFY_TOKEN_PATH",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        client_id="test-client",
        token_path=tmp_path / "auth" / "tokens.json",
        data_dir=tmp_path / "data",
    )


@pytest.fixture
def token():
    return TokenData(
        client_id="test-client",
        access_token=SecretStr("access-secret"),
        refresh_token=SecretStr("refresh-secret"),
        expires_at=4600,
        scopes=list(READ_SCOPES),
    )


@pytest.fixture
def token_body():
    return {
        "access_token": "new-access-secret",
        "refresh_token": "new-refresh-secret",
        "expires_in": 3600,
        "scope": " ".join(READ_SCOPES),
        "token_type": "Bearer",
    }


@pytest.fixture
def make_api(settings, token):
    clients = []

    def factory(handler: Callable[[httpx.Request], httpx.Response]):
        TokenStore(settings.token_path).save(token)
        transport = httpx.Client(transport=httpx.MockTransport(handler))
        clients.append(transport)
        delays = []
        auth = SpotifyAuth(settings, transport, clock=lambda: 1000, sleep=delays.append)
        api = SpotifyClient(settings, auth, transport, sleep=delays.append)
        return api, auth, delays

    yield factory
    for client in clients:
        client.close()


@pytest.fixture
def raw_local():
    return {
        "added_at": None,
        "added_by": None,
        "is_local": True,
        "item": {
            "id": None,
            "type": "track",
            "uri": "spotify:local:Westside+Gunn:Supreme+Blientele:Elizabeth:241",
            "name": "Elizabeth",
            "artists": [{"id": None, "name": "Westside Gunn"}],
            "album": {"id": None, "name": "Supreme Blientele"},
            "duration_ms": 241000,
        },
    }


@pytest.fixture
def capture(raw_local):
    metadata = {
        "id": PLAYLIST_ID,
        "name": "Westside Gunn",
        "snapshot_id": "snapshot-one",
        "items": {"total": 1},
    }
    return PlaylistCapture(
        captured_at=datetime(2026, 10, 7, tzinfo=UTC),
        playlist=PlaylistSummary(
            playlist_id=PLAYLIST_ID,
            name="Westside Gunn",
            item_count=1,
            snapshot_id="snapshot-one",
        ),
        snapshot_id="snapshot-one",
        entries=[parse_entry(raw_local, 0)],
        raw_playlist_before=metadata,
        raw_playlist_after=metadata,
        raw_pages=[{"items": [raw_local], "offset": 0, "total": 1, "next": None}],
    )
