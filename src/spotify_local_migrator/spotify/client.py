import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from ..config import Settings
from ..errors import AuthenticationError, PlaylistChangedError, RateLimitError, SpotifyAPIError
from ..migration.state import validate_playlist_id
from .auth import SpotifyAuth
from .http import request_json as transport_request_json
from .pacing import RequestPacer
from .rate_limits import CooldownStore

API_ROOT = "https://api.spotify.com/v1"


class SpotifyClient:
    """2026 Web API adapter; writes require explicit OAuth scopes."""

    def __init__(
        self,
        settings: Settings,
        auth: SpotifyAuth,
        client: httpx.Client,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.auth = auth
        self.client = client
        self.sleep = sleep
        self.cooldown = CooldownStore(settings.data_dir, settings.require_client_id(), clock=clock)
        self.pacer = RequestPacer(
            settings.data_dir,
            interval=settings.request_interval_seconds,
            budget=settings.request_budget_24h,
            clock=clock,
            sleep=sleep,
        )

    def _before_request(self) -> None:
        self.cooldown.check()
        self.pacer.before_request()
        # A different command may have received a 429 while pacing waited.
        self.cooldown.check()

    def _request_json(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        self.cooldown.check()
        try:
            return transport_request_json(*args, before_request=self._before_request, **kwargs)
        except RateLimitError as exc:
            if exc.retry_after is not None:
                self.cooldown.save(exc.retry_after)
            raise

    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self.auth.access_token()
        for auth_attempt in range(2):
            status, body = self._request_json(
                self.client,
                "GET",
                API_ROOT + endpoint,
                label=f"Spotify GET {endpoint}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                max_retries=self.settings.max_retries,
                max_retry_wait=self.settings.max_retry_wait,
                sleep=self.sleep,
            )
            if status == 401:
                if auth_attempt == 0:
                    token = self.auth.access_token(force_refresh=True)
                    continue
                raise AuthenticationError(
                    "Spotify rejected refreshed authentication. Run login again."
                )
            if status == 403:
                raise SpotifyAPIError(
                    "Spotify denied access (HTTP 403). Playlist items require ownership or "
                    "actual collaborator access. Also check the app's user allowlist, "
                    "Premium app owner and read scopes.",
                    status_code=403,
                )
            if not 200 <= status < 300:
                raise SpotifyAPIError(
                    f"Spotify GET {endpoint} failed (HTTP {status}).", status_code=status
                )
            return body
        raise AssertionError("Unreachable authentication state")

    def playlist(self, playlist_id: str) -> dict[str, Any]:
        return self._get(f"/playlists/{validate_playlist_id(playlist_id)}")

    def _pages(
        self, endpoint: str, extra: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        offset = 0
        expected_total: int | None = None
        while True:
            page = self._get(endpoint, {"limit": 50, "offset": offset, **(extra or {})})
            items = page.get("items")
            total = page.get("total")
            if (
                not isinstance(items, list)
                or not isinstance(total, int)
                or isinstance(total, bool)
                or total < 0
                or page.get("offset") != offset
                or "next" not in page
            ):
                raise SpotifyAPIError("Spotify returned malformed pagination; scan not saved.")
            if expected_total is None:
                expected_total = total
            if total != expected_total:
                raise PlaylistChangedError("The collection changed while reading its pages.")
            if len(items) > 50 or offset + len(items) > total:
                raise SpotifyAPIError("Spotify returned an inconsistent page size; scan not saved.")
            yield page
            offset += len(items)
            if page["next"] is None:
                if offset != total:
                    raise PlaylistChangedError(
                        "Spotify pagination ended before all entries were read."
                    )
                break
            if not isinstance(page["next"], str) or not items or offset >= total:
                raise SpotifyAPIError("Spotify pagination did not progress; scan not saved.")
            # Do not follow arbitrary URLs from responses with a Bearer token.
            # Calculate offsets on the same fixed trusted endpoint instead.

    def playlists(self) -> Iterator[dict[str, Any]]:
        for page in self._pages("/me/playlists"):
            for playlist in page["items"]:
                if not isinstance(playlist, dict):
                    raise SpotifyAPIError("Spotify returned an invalid playlist listing.")
                yield playlist

    def playlist_item_pages(self, playlist_id: str) -> Iterator[dict[str, Any]]:
        playlist_id = validate_playlist_id(playlist_id)
        yield from self._pages(
            f"/playlists/{playlist_id}/items", {"additional_types": "track,episode"}
        )

    def current_user(self) -> dict[str, Any]:
        return self._get("/me")

    def search_tracks(
        self, query: str, *, market: str | None = None, offset: int = 0
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"q": query, "type": "track", "limit": 10, "offset": offset}
        if market:
            params["market"] = market
        return self._get("/search", params)

    def _mutate(
        self,
        method: str,
        endpoint: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        # Mutating network/5xx failures are uncertain outcomes. The durable
        # executor reconciles them; request_json must never blindly retry them.
        self.auth.require_write_scopes()
        token = self.auth.access_token()
        for attempt in range(2):
            status, body = self._request_json(
                self.client,
                method,
                API_ROOT + endpoint,
                label=f"Spotify {method} {endpoint}",
                headers={"Authorization": f"Bearer {token}"},
                json=json,
                params=params,
                allow_empty=allow_empty,
                max_retries=self.settings.max_retries,
                max_retry_wait=self.settings.max_retry_wait,
                sleep=self.sleep,
            )
            if status == 401 and attempt == 0:
                token = self.auth.access_token(force_refresh=True)
                continue
            if not 200 <= status < 300:
                raise SpotifyAPIError(
                    f"Spotify {method} failed (HTTP {status}). "
                    "Progress is saved; use resume after resolving access.",
                    status_code=status,
                )
            return body
        raise AssertionError("Unreachable authentication state")

    def track(self, track_id: str) -> dict[str, Any]:
        return self._get(f"/tracks/{validate_playlist_id(track_id)}")

    def create_probe_playlist(self, name: str) -> dict[str, Any]:
        return self._mutate(
            "POST",
            "/me/playlists",
            json={
                "name": name,
                "public": False,
                "description": "Temporary local-track migrator compatibility check.",
            },
        )

    def add_tracks(self, playlist_id: str, uris: list[str], position: int) -> str:
        import re

        if (
            not uris
            or len(uris) > 100
            or position < 0
            or any(not re.fullmatch(r"spotify:track:[A-Za-z0-9]{22}", uri) for uri in uris)
        ):
            raise SpotifyAPIError("Invalid catalogue-only insertion request.")
        body = self._mutate(
            "POST",
            f"/playlists/{validate_playlist_id(playlist_id)}/items",
            json={"uris": uris, "position": position},
        )
        return self._snapshot(body)

    @staticmethod
    def _snapshot(body: dict[str, Any]) -> str:
        value = body.get("snapshot_id")
        if not isinstance(value, str) or not value:
            raise SpotifyAPIError("Mutation returned no snapshot; reconcile with resume.")
        return value

    def remove_positions(self, playlist_id: str, positions: list[int], snapshot_id: str) -> str:
        """Undocumented positional shape: usable ONLY after executor preflight.

        Spotify's playlist concepts mandate position+snapshot for local items,
        while the 2026 DELETE reference omits the position-only JSON schema.
        Never substitute local URIs or broaden this to DELETE-by-URI.
        """
        if (
            not positions
            or len(positions) > 100
            or not snapshot_id
            or any(isinstance(p, bool) or not isinstance(p, int) or p < 0 for p in positions)
        ):
            raise SpotifyAPIError("Invalid positional removal request.")
        body = self._mutate(
            "DELETE",
            f"/playlists/{validate_playlist_id(playlist_id)}/items",
            json={"items": [], "positions": positions, "snapshot_id": snapshot_id},
        )
        return self._snapshot(body)

    def unfollow_probe_playlist(self, playlist_id: str) -> None:
        self._mutate(
            "DELETE",
            "/me/library",
            params={"uris": "spotify:playlist:" + validate_playlist_id(playlist_id)},
            allow_empty=True,
        )
