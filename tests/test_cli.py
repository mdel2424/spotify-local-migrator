import json
from contextlib import contextmanager

import httpx
import pytest
from typer.testing import CliRunner

from spotify_local_migrator import cli
from spotify_local_migrator.migration.state import CaptureStore

runner = CliRunner()
PLAYLIST_ID = "A" * 22


@pytest.fixture
def cli_setup(settings, make_api, monkeypatch, raw_local, tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/v1/me/playlists":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": PLAYLIST_ID,
                            "name": "Westside Gunn",
                            "items": {"total": 1},
                            "owner": {"id": "owner"},
                        }
                    ],
                    "offset": 0,
                    "total": 1,
                    "next": None,
                },
            )
        if request.url.path.endswith("/items"):
            return httpx.Response(
                200,
                json={
                    "items": [raw_local],
                    "offset": 0,
                    "total": 1,
                    "next": None,
                },
            )
        return httpx.Response(
            200,
            json={
                "id": PLAYLIST_ID,
                "name": "Westside Gunn",
                "snapshot_id": "stable",
                "items": {"total": 1},
            },
        )

    api, auth, _ = make_api(handler)

    @contextmanager
    def services(_settings):
        yield auth, api

    monkeypatch.setattr(cli, "services", services)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"SPOTIFY_CLIENT_ID=test-client\nSPOTIFY_DATA_DIR={settings.data_dir}\n"
        f"SPOTIFY_TOKEN_PATH={settings.token_path}\n"
    )
    return ["--env-file", str(env_file)], calls, settings


def test_help_lists_read_matching_review_and_migration_commands():
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0, result.output
    for command in ("login", "playlists", "scan", "status", "match", "review", "migrate", "resume"):
        assert command in result.output


def test_scan_prints_real_metadata_and_saves_complete_capture(cli_setup):
    options, calls, settings = cli_setup
    result = runner.invoke(cli.app, options + ["scan", "--playlist", PLAYLIST_ID, "--raw"])
    assert result.exit_code == 0, result.output
    assert "Elizabeth" in result.output
    assert "Supreme Blientele" in result.output
    assert "4:01" in result.output
    assert "Original Spotify item wrapper" in result.output
    original = settings.data_dir / PLAYLIST_ID / "original.json"
    assert CaptureStore.load(original).local_tracks[0].title == "Elizabeth"
    assert all(request.method == "GET" for request in calls)


def test_json_output_is_machine_readable(cli_setup):
    options, _, _ = cli_setup
    result = runner.invoke(cli.app, options + ["scan", "--playlist", PLAYLIST_ID, "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["phase"] == "SCAN"
    assert data["entries"][0]["local_track"]["title"] == "Elizabeth"
    assert "saved" in result.stderr


def test_interactive_default_selects_playlist(cli_setup):
    options, calls, _ = cli_setup
    result = runner.invoke(cli.app, options, input="1\n")
    assert result.exit_code == 0, result.output
    assert "Select playlist" in result.output
    assert "Elizabeth" in result.output
    assert any(request.url.path == "/v1/me/playlists" for request in calls)


def test_local_counts_read_playlists_without_saving(cli_setup):
    options, calls, settings = cli_setup
    result = runner.invoke(cli.app, options + ["playlists", "--count-local"])
    assert result.exit_code == 0, result.output
    assert "Westside Gunn" in result.output
    assert len(calls) == 4
    assert CaptureStore(settings.data_dir).originals() == []
    assert not (settings.data_dir / PLAYLIST_ID).exists()


def test_status_does_not_contact_spotify(cli_setup):
    options, calls, _ = cli_setup
    result = runner.invoke(cli.app, options + ["status"])
    assert result.exit_code == 0, result.output
    assert "SCAN" in result.output or "scan baselines" in result.output
    assert calls == []
    assert "3s between API attempts" in result.output
    assert "0/400" in result.output


def test_json_requires_explicit_playlist(cli_setup):
    options, calls, _ = cli_setup
    result = runner.invoke(cli.app, options + ["scan", "--json"])
    assert result.exit_code != 0
    assert "requires" in result.output
    assert calls == []


@pytest.mark.parametrize(
    "value",
    [
        "A" * 22,
        "spotify:playlist:" + "A" * 22,
        "https://open.spotify.com/playlist/" + "A" * 22 + "?si=test",
    ],
)
def test_playlist_identifiers(value):
    assert cli.parse_playlist_argument(value) == PLAYLIST_ID


def test_no_credentials_stops_with_setup_instructions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["scan", "-p", PLAYLIST_ID])
    assert result.exit_code == 1
    assert "SPOTIFY_CLIENT_ID" in result.output
    assert "Client Secret" in result.output
    assert not (tmp_path / "data").exists()


def test_failed_scan_publishes_no_state(cli_setup, monkeypatch):
    options, _, settings = cli_setup
    from spotify_local_migrator.errors import PlaylistChangedError

    def fail(*args, **kwargs):
        raise PlaylistChangedError("No scan saved.")

    monkeypatch.setattr(cli.PlaylistScanner, "scan", fail)
    result = runner.invoke(cli.app, options + ["scan", "-p", PLAYLIST_ID])
    assert result.exit_code == 1
    assert not settings.data_dir.exists()


def test_debug_scan_logs_status_without_token_disclosure(cli_setup):
    options, _, _ = cli_setup
    result = runner.invoke(cli.app, options + ["-vv", "scan", "-p", PLAYLIST_ID])
    assert result.exit_code == 0, result.output
    assert "HTTP 200" in result.stderr
    assert "access-secret" not in result.output
    assert "refresh-secret" not in result.output
