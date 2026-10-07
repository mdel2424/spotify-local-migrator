import pytest
from pydantic import ValidationError

from spotify_local_migrator.config import Settings, load_settings
from spotify_local_migrator.errors import ConfigurationError


def test_env_file_loading_and_process_override(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SPOTIFY_CLIENT_ID=file-client\n"
        "SPOTIFY_REDIRECT_URI=http://127.0.0.1:8765/callback\n"
        "SPOTIFY_DATA_DIR=custom-data\n"
    )
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "process-client")
    settings = load_settings(env_file)
    assert settings.client_id == "process-client"
    assert str(settings.data_dir) == "custom-data"
    assert "process-client" not in repr(settings)


@pytest.mark.parametrize(
    "redirect",
    [
        "http://localhost:8765/callback",
        "http://0.0.0.0:8765/callback",
        "http://127.0.0.1/callback",
        "https://example.com/callback",
        "http://127.0.0.1:8765/callback?x=1",
        "http://127.0.0.1:8765/callback#fragment",
        "http://user@127.0.0.1:8765/callback",
    ],
)
def test_reject_unsupported_redirects(redirect):
    with pytest.raises(ValidationError):
        Settings(redirect_uri=redirect)


def test_no_client_id_has_setup_message():
    with pytest.raises(ConfigurationError, match="SPOTIFY_CLIENT_ID"):
        Settings().require_client_id()


def test_invalid_env_configuration_does_not_dump_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("SPOTIFY_REDIRECT_URI=http://secret@example.com/callback\n")
    with pytest.raises(ConfigurationError) as error:
        load_settings(env_file)
    assert "secret@" not in str(error.value)
