import httpx
import pytest

from spotify_local_migrator.errors import RateLimitError
from spotify_local_migrator.spotify.rate_limits import CooldownStore


def test_long_retry_after_is_persisted_and_next_call_sends_no_request(make_api, settings):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "86400"}, json={"error": {}})

    api, _, waits = make_api(handler)
    with pytest.raises(RateLimitError):
        api.search_tracks("query")
    assert len(calls) == 1
    assert waits == []
    assert (settings.data_dir / ".cache" / "rate-limit.json").exists()
    with pytest.raises(RateLimitError, match="no API request was sent"):
        api.search_tracks("other query")
    assert len(calls) == 1


def test_cooldown_survives_new_instance_and_expires(tmp_path):
    now = [1000.0]
    cooldown = CooldownStore(tmp_path, "client", clock=lambda: now[0])
    cooldown.save(60)
    with pytest.raises(RateLimitError):
        CooldownStore(tmp_path, "client", clock=lambda: now[0]).check()
    now[0] += 61
    CooldownStore(tmp_path, "client", clock=lambda: now[0]).check()


def test_different_client_does_not_use_other_client_cooldown(tmp_path):
    CooldownStore(tmp_path, "one").save(60)
    CooldownStore(tmp_path, "two").check()


def test_zero_retry_window_not_persisted(tmp_path):
    cooldown = CooldownStore(tmp_path, "client")
    cooldown.save(0)
    assert not cooldown.path.exists()
