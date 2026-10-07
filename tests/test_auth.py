import json
import logging
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import httpx
import pytest

from spotify_local_migrator.config import READ_SCOPES
from spotify_local_migrator.errors import AuthenticationError
from spotify_local_migrator.spotify.auth import (
    AuthorizationRequest,
    CallbackServer,
    SpotifyAuth,
    TokenStore,
)


def callback(request, **query):
    return request.redirect_uri + "?" + urlencode({"state": request.state, "code": "code", **query})


def test_pkce_rfc7636_vector_and_read_only_scopes(settings):
    request = AuthorizationRequest(
        settings.client_id,
        settings.redirect_uri,
        "state",
        "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    )
    params = parse_qs(urlsplit(request.url).query)
    assert params["code_challenge"] == ["E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["scope"] == [" ".join(READ_SCOPES)]
    assert "client_secret" not in params
    assert not any("modify" in scope for scope in READ_SCOPES)


def test_random_verifier_and_state(settings):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected HTTP"))
    ) as client:
        auth = SpotifyAuth(settings, client)
        first, second = auth.begin_login(), auth.begin_login()
    assert first.state != second.state
    assert first.verifier != second.verifier
    assert 43 <= len(first.verifier) <= 128
    assert first.verifier not in repr(first)


@pytest.mark.parametrize(
    "suffix",
    [
        "?state=wrong&code=code",
        "?code=code",
        "?state=right&state=right&code=code",
        "?state=right&code=code&code=other",
        "?state=right&error=access_denied",
        "?state=right&code=",
    ],
)
def test_invalid_callbacks_never_exchange_tokens(settings, suffix):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected HTTP"))
    ) as client:
        auth = SpotifyAuth(settings, client)
        request = AuthorizationRequest(settings.client_id, settings.redirect_uri, "right", "v" * 64)
        with pytest.raises(AuthenticationError):
            auth.finish_login(request, settings.redirect_uri + suffix)


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.com/callback?state=s&code=c",
        "http://127.0.0.1:8765/wrong?state=s&code=c",
        "http://127.0.0.1:9999/callback?state=s&code=c",
    ],
)
def test_callback_origin_and_path_must_match(settings, uri):
    request = AuthorizationRequest(settings.client_id, settings.redirect_uri, "s", "v" * 64)
    with pytest.raises(AuthenticationError):
        request.callback_code(uri)


def test_login_exchange_form_and_secure_token_store(settings, token_body):
    def handler(request):
        assert str(request.url) == "https://accounts.spotify.com/api/token"
        assert request.method == "POST"
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["authorization_code"]
        assert form["code_verifier"] == ["v" * 64]
        assert form["redirect_uri"] == [settings.redirect_uri]
        assert "client_secret" not in form
        return httpx.Response(200, json=token_body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        auth = SpotifyAuth(settings, client, clock=lambda: 1000)
        request = AuthorizationRequest(settings.client_id, settings.redirect_uri, "s", "v" * 64)
        token = auth.finish_login(request, callback(request))
    assert token.expires_at == 4600
    assert token.access_token.get_secret_value() == "new-access-secret"
    assert "new-access-secret" not in repr(token)
    assert settings.token_path.stat().st_mode & 0o777 == 0o600
    assert settings.token_path.parent.stat().st_mode & 0o777 == 0o700
    assert json.loads(settings.token_path.read_text())["refresh_token"] == "new-refresh-secret"
    assert TokenStore(settings.token_path).load() == token


def test_refresh_retains_omitted_refresh_token_and_scope(settings, token, token_body):
    token.expires_at = 999
    TokenStore(settings.token_path).save(token)
    body = {
        key: value for key, value in token_body.items() if key not in ("refresh_token", "scope")
    }

    def handler(request):
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["refresh_token"]
        assert form["refresh_token"] == ["refresh-secret"]
        assert form["client_id"] == ["test-client"]
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        auth = SpotifyAuth(settings, client, clock=lambda: 1000)
        assert auth.access_token() == "new-access-secret"
    stored = TokenStore(settings.token_path).load()
    assert stored.refresh_token.get_secret_value() == "refresh-secret"
    assert stored.scopes == list(READ_SCOPES)


def test_invalid_grant_has_safe_actionable_error(settings, token):
    TokenStore(settings.token_path).save(token)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(400, json={"error": "invalid_grant", "secret": "never-print"})
        )
    ) as client:
        auth = SpotifyAuth(settings, client, clock=lambda: 1000)
        with pytest.raises(AuthenticationError, match="Run login again") as error:
            auth.access_token(force_refresh=True)
    assert "never-print" not in str(error.value)
    assert TokenStore(settings.token_path).load() == token


@pytest.mark.parametrize(
    "change",
    [
        {"expires_in": -1},
        {"expires_in": "nan"},
        {"access_token": ""},
        {"scope": "playlist-modify-public"},
        {"refresh_token": None},
    ],
)
def test_bad_token_response_never_saved(settings, token_body, change):
    token_body.update(change)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=token_body))
    ) as client:
        auth = SpotifyAuth(settings, client)
        request = AuthorizationRequest(settings.client_id, settings.redirect_uri, "s", "v" * 64)
        with pytest.raises(AuthenticationError):
            auth.finish_login(request, callback(request))
    assert not settings.token_path.exists()


def test_client_id_binding(settings, token):
    token.client_id = "another-client"
    TokenStore(settings.token_path).save(token)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected HTTP"))
    ) as client:
        with pytest.raises(AuthenticationError, match="another Client ID"):
            SpotifyAuth(settings, client, clock=lambda: 1000).access_token()


def test_absent_and_corrupt_token_cache(settings):
    store = TokenStore(settings.token_path)
    with pytest.raises(AuthenticationError, match="Not logged in"):
        store.load()
    settings.token_path.parent.mkdir()
    settings.token_path.write_text('{"access_token": "secret-corrupt"}')
    with pytest.raises(AuthenticationError) as error:
        store.load()
    assert "secret-corrupt" not in str(error.value)


def test_loopback_callback_and_invalid_state_does_not_end_login(settings, token_body):
    request = AuthorizationRequest(settings.client_id, "http://127.0.0.1:0/callback", "s", "v" * 64)
    with CallbackServer(request) as server:
        port = server.server_address[1]
        result = []
        thread = Thread(target=lambda: result.append(server.wait_for_callback(3)))
        thread.start()
        try:
            with pytest.raises(HTTPError) as error:
                urlopen(f"http://127.0.0.1:{port}/callback?state=wrong&code=c", timeout=2)
            assert error.value.code == 400
            assert server.callback_url is None
            with urlopen(f"http://127.0.0.1:{port}/callback?state=s&code=c", timeout=2) as response:
                assert response.status == 200
        finally:
            thread.join(timeout=4)
        assert not thread.is_alive()
        assert result == [request.redirect_uri + "?state=s&code=c"]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=token_body))
    ) as client:
        auth = SpotifyAuth(settings, client)
        auth.finish_login(request, result[0])
    assert settings.token_path.exists()


def test_callback_timeout(settings):
    request = AuthorizationRequest(settings.client_id, "http://127.0.0.1:0/callback", "s", "v" * 64)
    with CallbackServer(request) as server:
        with pytest.raises(AuthenticationError, match="timed out"):
            server.wait_for_callback(0)


def test_auth_debug_logs_do_not_include_tokens(settings, token_body, caplog):
    caplog.set_level(logging.DEBUG, logger="spotify_local_migrator")
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=token_body))
    ) as client:
        auth = SpotifyAuth(settings, client)
        request = AuthorizationRequest(
            settings.client_id, settings.redirect_uri, "s", "secret-verifier"
        )
        auth.finish_login(request, callback(request))
    assert "secret-verifier" not in caplog.text
    assert "new-access-secret" not in caplog.text
    assert "new-refresh-secret" not in caplog.text


def test_unicode_state_and_malformed_callback_are_safe_errors(settings):
    request = AuthorizationRequest(settings.client_id, settings.redirect_uri, "s", "v" * 64)
    with pytest.raises(AuthenticationError, match="state mismatch"):
        request.callback_code(callback(request, state="☃"))
    with pytest.raises(AuthenticationError, match="Malformed callback"):
        request.callback_code("http://[")


def test_code_exchange_network_failure_is_not_retried(settings):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("private detail", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        auth = SpotifyAuth(settings, client)
        request = auth.begin_login()
        from spotify_local_migrator.errors import SpotifyAPIError

        with pytest.raises(SpotifyAPIError) as error:
            auth.finish_login(request, callback(request))
    assert len(calls) == 1
    assert "private detail" not in str(error.value)


@pytest.mark.parametrize(
    "field, value",
    [
        ("access_token", ""),
        ("refresh_token", ""),
        ("expires_at", "NaN"),
    ],
)
def test_invalid_cached_token_fields_are_rejected(settings, token, field, value):
    TokenStore(settings.token_path).save(token)
    data = json.loads(settings.token_path.read_text())
    data[field] = value
    settings.token_path.write_text(json.dumps(data))
    with pytest.raises(AuthenticationError, match="Run login again"):
        TokenStore(settings.token_path).load()
