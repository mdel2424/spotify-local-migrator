import httpx
import pytest

from spotify_local_migrator.errors import (
    AuthenticationError,
    PlaylistChangedError,
    RateLimitError,
    SpotifyAPIError,
)

PLAYLIST_ID = "A" * 22


def test_current_endpoint_and_all_item_types(make_api):
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == f"/v1/playlists/{PLAYLIST_ID}/items"
        assert request.url.params["limit"] == "50"
        assert request.url.params["additional_types"] == "track,episode"
        return httpx.Response(200, json={"items": [], "offset": 0, "total": 0, "next": None})

    api, _, _ = make_api(handler)
    assert len(list(api.playlist_item_pages(PLAYLIST_ID))) == 1


def test_playlist_list_pagination_does_not_follow_untrusted_next(make_api):
    offsets = []

    def handler(request):
        assert request.url.host == "api.spotify.com"
        assert request.url.path == "/v1/me/playlists"
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        return httpx.Response(
            200,
            json={
                "items": [{"id": str(offset)}],
                "offset": offset,
                "total": 2,
                "next": "https://attacker.example/collect" if offset == 0 else None,
            },
        )

    api, _, _ = make_api(handler)
    assert [item["id"] for item in api.playlists()] == ["0", "1"]
    assert offsets == [0, 1]


def test_401_refresh_once_then_success(make_api, token_body):
    api_calls = []
    refreshes = []

    def handler(request):
        if request.url.host == "accounts.spotify.com":
            refreshes.append(request)
            return httpx.Response(200, json=token_body)
        api_calls.append(request.headers["Authorization"])
        return httpx.Response(401 if len(api_calls) == 1 else 200, json={"id": PLAYLIST_ID})

    api, _, _ = make_api(handler)
    assert api.playlist(PLAYLIST_ID)["id"] == PLAYLIST_ID
    assert api_calls == ["Bearer access-secret", "Bearer new-access-secret"]
    assert len(refreshes) == 1


def test_repeated_401_stops(make_api, token_body):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200 if request.url.host == "accounts.spotify.com" else 401,
            json=token_body if request.url.host == "accounts.spotify.com" else {},
        )

    api, _, _ = make_api(handler)
    with pytest.raises(AuthenticationError, match="Run login again"):
        api.playlist(PLAYLIST_ID)
    assert len(calls) == 3


def test_403_explains_current_access_restrictions(make_api):
    api, _, delays = make_api(lambda _: httpx.Response(403))
    with pytest.raises(SpotifyAPIError, match="collaborator") as error:
        api.playlist(PLAYLIST_ID)
    assert error.value.status_code == 403
    assert delays == []


def test_429_respects_retry_after(make_api):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={})
        return httpx.Response(200, json={"id": PLAYLIST_ID})

    api, _, delays = make_api(handler)
    api.playlist(PLAYLIST_ID)
    assert delays == [7]
    assert len(calls) == 2


@pytest.mark.parametrize("retry_after", ["nan", "-2", "garbage"])
def test_invalid_retry_after_uses_backoff(make_api, retry_after):
    calls = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": retry_after})
            if len(calls) == 1
            else httpx.Response(200, json={})
        )

    api, _, delays = make_api(handler)
    api.playlist(PLAYLIST_ID)
    assert delays == [1]


def test_long_retry_after_stops_without_early_retry(make_api):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    api, _, delays = make_api(handler)
    with pytest.raises(RateLimitError) as error:
        api.playlist(PLAYLIST_ID)
    assert error.value.retry_after == 120
    assert len(calls) == 1
    assert delays == []


def test_repeated_rate_limit_is_bounded(make_api):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "2"})

    api, _, delays = make_api(handler)
    with pytest.raises(RateLimitError):
        api.playlist(PLAYLIST_ID)
    assert len(calls) == 4
    assert delays == [2, 2, 2]


def test_development_quota_without_retry_time_stops(make_api):
    api, _, delays = make_api(
        lambda _: httpx.Response(429, json={"error": {"reason": "QUOTA_EXCEEDED"}})
    )
    with pytest.raises(RateLimitError, match="quota") as error:
        api.playlist(PLAYLIST_ID)
    assert error.value.reason == "QUOTA_EXCEEDED"
    assert delays == []


def test_get_network_and_server_failure_retries(make_api):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("private-network-detail", request=request)
        return httpx.Response(503 if len(calls) == 2 else 200, json={})

    api, _, delays = make_api(handler)
    api.playlist(PLAYLIST_ID)
    assert len(calls) == 3
    assert delays == [1, 2]


def test_server_failure_exhaustion(make_api):
    api, _, delays = make_api(lambda _: httpx.Response(503))
    with pytest.raises(SpotifyAPIError) as error:
        api.playlist(PLAYLIST_ID)
    assert error.value.status_code == 503
    assert delays == [1, 2, 4]


@pytest.mark.parametrize("body", ["not-json", '["unexpected"]'])
def test_invalid_success_body(make_api, body):
    api, _, _ = make_api(lambda _: httpx.Response(200, text=body))
    with pytest.raises(SpotifyAPIError):
        api.playlist(PLAYLIST_ID)


@pytest.mark.parametrize(
    "page",
    [
        {"items": [], "offset": 0, "total": 1, "next": "next"},
        {"items": [], "offset": 2, "total": 0, "next": None},
        {"items": {}, "offset": 0, "total": 0, "next": None},
        {"items": [], "offset": 0, "total": 0},
        {"items": [None], "offset": 0, "total": 0, "next": None},
    ],
)
def test_malformed_pagination_is_rejected(make_api, page):
    api, _, _ = make_api(lambda _: httpx.Response(200, json=page))
    with pytest.raises(SpotifyAPIError):
        list(api.playlist_item_pages(PLAYLIST_ID))


def test_truncated_pagination_is_rejected(make_api):
    api, _, _ = make_api(
        lambda _: httpx.Response(200, json={"items": [None], "offset": 0, "total": 2, "next": None})
    )
    with pytest.raises(PlaylistChangedError):
        list(api.playlist_item_pages(PLAYLIST_ID))


def test_redirects_are_not_followed_and_are_reported(make_api):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://example.com"}, json={})

    api, _, _ = make_api(handler)
    with pytest.raises(SpotifyAPIError) as error:
        api.playlist(PLAYLIST_ID)
    assert error.value.status_code == 302
    assert len(calls) == 1
