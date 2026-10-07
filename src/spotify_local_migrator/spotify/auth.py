import base64
import hashlib
import secrets
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from pydantic import SecretStr, ValidationError

from ..config import READ_SCOPES, WRITE_SCOPES, Settings
from ..errors import AuthenticationError
from ..migration.state import atomic_write_json
from ..models import TokenData
from .http import request_json

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"


@dataclass(frozen=True)
class AuthorizationRequest:
    client_id: str
    redirect_uri: str
    state: str = field(repr=False)
    verifier: str = field(repr=False)
    scopes: tuple[str, ...] = READ_SCOPES

    @property
    def url(self) -> str:
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode("ascii")).digest())
            .decode("ascii")
            .rstrip("=")
        )
        return (
            AUTHORIZE_URL
            + "?"
            + urlencode(
                {
                    "client_id": self.client_id,
                    "response_type": "code",
                    "redirect_uri": self.redirect_uri,
                    "scope": " ".join(self.scopes),
                    "state": self.state,
                    "code_challenge_method": "S256",
                    "code_challenge": challenge,
                }
            )
        )

    def callback_code(self, callback_url: str) -> str:
        try:
            received = urlsplit(callback_url)
        except ValueError as exc:
            raise AuthenticationError("Malformed callback URL; restart login.") from exc
        expected = urlsplit(self.redirect_uri)
        if (
            received.scheme != expected.scheme
            or received.netloc != expected.netloc
            or received.path != expected.path
            or received.fragment
        ):
            raise AuthenticationError("Callback URL does not match the configured redirect URI.")
        query = parse_qs(received.query, keep_blank_values=True)
        states = query.get("state", [])
        if len(states) != 1 or not secrets.compare_digest(
            states[0].encode("utf-8"), self.state.encode("utf-8")
        ):
            raise AuthenticationError("OAuth state mismatch; restart login.")
        if query.get("error"):
            raise AuthenticationError("Spotify authorization was denied. Run login again.")
        codes = query.get("code", [])
        if len(codes) != 1 or not codes[0]:
            raise AuthenticationError("Spotify callback did not include one authorization code.")
        return codes[0]


class TokenStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> TokenData:
        try:
            return TokenData.model_validate_json(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AuthenticationError("Not logged in. Run spotify-local-migrate login.") from exc
        except (OSError, ValueError, ValidationError) as exc:
            raise AuthenticationError("Cannot read saved tokens. Run login again.") from exc

    def save(self, token: TokenData) -> None:
        data = token.model_dump(mode="json")
        data["access_token"] = token.access_token.get_secret_value()
        data["refresh_token"] = token.refresh_token.get_secret_value()
        try:
            atomic_write_json(self.path, data)
        except (OSError, ValueError) as exc:
            raise AuthenticationError(
                "Cannot save tokens; check token directory permissions."
            ) from exc


class SpotifyAuth:
    def __init__(
        self,
        settings: Settings,
        client: httpx.Client,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.client = client
        self.clock = clock
        self.sleep = sleep
        self.store = TokenStore(settings.token_path)

    def begin_login(self, *, write: bool = False) -> AuthorizationRequest:
        return AuthorizationRequest(
            self.settings.require_client_id(),
            self.settings.redirect_uri,
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(64),
            READ_SCOPES + WRITE_SCOPES if write else READ_SCOPES,
        )

    def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        status, body = request_json(
            self.client,
            "POST",
            TOKEN_URL,
            label="Spotify token exchange",
            data=data,
            max_retries=self.settings.max_retries,
            max_retry_wait=self.settings.max_retry_wait,
            sleep=self.sleep,
        )
        if not 200 <= status < 300:
            raise AuthenticationError(
                f"Spotify token exchange failed (HTTP {status}). "
                "Run login again; check the Client ID and registered redirect URI."
            )
        return body

    def _save_response(self, body: dict[str, Any], previous: TokenData | None = None) -> TokenData:
        try:
            refresh = body.get("refresh_token") or (
                previous.refresh_token.get_secret_value() if previous else None
            )
            if not isinstance(body.get("access_token"), str) or not body["access_token"]:
                raise ValueError
            if not isinstance(refresh, str) or not refresh:
                raise ValueError
            lifetime = float(body["expires_in"])
            if not 0 < lifetime < float("inf"):
                raise ValueError
            scope = body.get("scope")
            if scope is None and previous:
                scopes = previous.scopes
            elif isinstance(scope, str):
                scopes = scope.split()
            else:
                raise ValueError
            if not set(READ_SCOPES).issubset(scopes):
                raise AuthenticationError("Required read scopes were not granted. Run login again.")
            token = TokenData(
                client_id=self.settings.require_client_id(),
                access_token=SecretStr(body["access_token"]),
                refresh_token=SecretStr(refresh),
                expires_at=self.clock() + lifetime,
                scopes=scopes,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthenticationError(
                "Spotify returned an incomplete token response. Retry login."
            ) from exc
        self.store.save(token)
        return token

    def finish_login(self, request: AuthorizationRequest, callback_url: str) -> TokenData:
        code = request.callback_code(callback_url)
        body = self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": request.client_id,
                "code": code,
                "redirect_uri": request.redirect_uri,
                "code_verifier": request.verifier,
            }
        )
        token = self._save_response(body)
        if not set(request.scopes).issubset(token.scopes):
            raise AuthenticationError(
                "Requested playlist permissions were not granted. Run login again."
            )
        return token

    def require_write_scopes(self) -> None:
        self.access_token()
        if not set(WRITE_SCOPES).issubset(self.store.load().scopes):
            raise AuthenticationError(
                "Write permissions are missing. Run spotify-local-migrate login --write "
                "then rerun migrate or resume. No playlist changes were made."
            )

    def access_token(self, *, force_refresh: bool = False) -> str:
        client_id = self.settings.require_client_id()
        token = self.store.load()
        if token.client_id != client_id:
            raise AuthenticationError("Saved tokens belong to another Client ID. Run login again.")
        if not set(READ_SCOPES).issubset(token.scopes):
            raise AuthenticationError("Saved tokens lack required read scopes. Run login again.")
        if force_refresh or token.expires_at <= self.clock() + 60:
            body = self._token_request(
                {
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": token.refresh_token.get_secret_value(),
                }
            )
            token = self._save_response(body, previous=token)
        return token.access_token.get_secret_value()


class CallbackServer(HTTPServer):
    """Loopback-only OAuth callback; request URLs are never logged."""

    def __init__(self, request: AuthorizationRequest):
        self.callback_url: str | None = None
        self.oauth_error: AuthenticationError | None = None
        expected_path = urlsplit(request.redirect_uri).path
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                if urlsplit(self.path).path != expected_path:
                    self.send_error(404)
                    return
                callback = request.redirect_uri + "?" + urlsplit(self.path).query
                try:
                    request.callback_code(callback)
                except AuthenticationError as exc:
                    # A stray/invalid state cannot terminate the legitimate pending login.
                    states = parse_qs(urlsplit(self.path).query).get("state", [])
                    if len(states) == 1 and secrets.compare_digest(
                        states[0].encode("utf-8"), request.state.encode("utf-8")
                    ):
                        owner.oauth_error = exc
                    self.send_response(400)
                    message = b"Authorization could not be completed. Return to your terminal."
                else:
                    owner.callback_url = callback
                    self.send_response(200)
                    message = b"Spotify authorization received. You can close this tab."
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(message)))
                self.end_headers()
                self.wfile.write(message)

        try:
            super().__init__(("127.0.0.1", urlsplit(request.redirect_uri).port), Handler)
        except OSError as exc:
            raise AuthenticationError(
                "Cannot bind the callback port. Close the other listener or change "
                "SPOTIFY_REDIRECT_URI and the matching Spotify app setting."
            ) from exc
        self.timeout = 1

    def wait_for_callback(self, timeout: float = 300) -> str:
        deadline = time.monotonic() + timeout
        while self.callback_url is None and self.oauth_error is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AuthenticationError("Login timed out. Run login again.")
            self.timeout = min(1, remaining)
            self.handle_request()
        if self.oauth_error is not None:
            raise self.oauth_error
        assert self.callback_url is not None
        return self.callback_url


def login_with_callback(
    auth: SpotifyAuth,
    show_url: Callable[[str], None],
    *,
    open_browser: bool = True,
    timeout: float = 300,
    write: bool = False,
) -> TokenData:
    request = auth.begin_login(write=write)
    # Bind before browser navigation so fast redirects cannot race the listener.
    with CallbackServer(request) as server:
        show_url(request.url)
        if open_browser:
            webbrowser.open(request.url)
        callback = server.wait_for_callback(timeout)
    return auth.finish_login(request, callback)
