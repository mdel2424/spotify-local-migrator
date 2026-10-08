import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, Field, ValidationError, field_validator

from .errors import ConfigurationError

WRITE_SCOPES = ("playlist-modify-public", "playlist-modify-private")

READ_SCOPES = ("playlist-read-private", "playlist-read-collaborative")


class Settings(BaseModel):
    client_id: str | None = Field(default=None, repr=False)
    redirect_uri: str = "http://127.0.0.1:8765/callback"
    data_dir: Path = Path("data")
    token_path: Path = Path(".spotify-local-migrate/tokens.json")
    http_timeout: float = Field(default=30, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)
    max_retry_wait: float = Field(default=60, gt=0)
    scan_attempts: int = Field(default=3, ge=1, le=10)
    request_interval_seconds: float = Field(default=3, ge=0, le=60, allow_inf_nan=False)
    wait_for_rate_limits: bool = True

    @field_validator("client_id", mode="before")
    @classmethod
    def clean_client_id(cls, value: str | None) -> str | None:
        return (value.strip() or None) if value else None

    @field_validator("redirect_uri")
    @classmethod
    def validate_redirect(cls, value: str) -> str:
        parsed = urlsplit(value)
        # The local callback implementation binds only explicit IPv4 loopback.
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or not parsed.port
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/")
        ):
            raise ValueError("Use http://127.0.0.1:<port>/<path> for the local callback.")
        return value

    def require_client_id(self) -> str:
        if not self.client_id:
            raise ConfigurationError(
                "Set SPOTIFY_CLIENT_ID in .env after creating a Spotify developer app. "
                "Register http://127.0.0.1:8765/callback; see README.md. "
                "PKCE does not need a Client Secret."
            )
        return self.client_id


def load_settings(env_file: Path = Path(".env")) -> Settings:
    """Read only the selected env file; process environment takes precedence."""
    keys = {
        "SPOTIFY_CLIENT_ID": "client_id",
        "SPOTIFY_REDIRECT_URI": "redirect_uri",
        "SPOTIFY_DATA_DIR": "data_dir",
        "SPOTIFY_TOKEN_PATH": "token_path",
        "SPOTIFY_REQUEST_INTERVAL_SECONDS": "request_interval_seconds",
        "SPOTIFY_WAIT_FOR_RATE_LIMITS": "wait_for_rate_limits",
    }
    try:
        values = dict(dotenv_values(env_file)) if env_file.is_file() else {}
        values.update({key: os.environ[key] for key in keys if key in os.environ})
        return Settings(**{field: values[key] for key, field in keys.items() if values.get(key)})
    except (OSError, ValueError, ValidationError) as exc:
        # ValidationError can include input values; do not expose those.
        raise ConfigurationError(
            "Cannot load configuration. Check .env paths and use a literal IPv4 "
            "loopback redirect URI such as http://127.0.0.1:8765/callback."
        ) from exc
