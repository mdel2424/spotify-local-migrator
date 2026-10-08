import json

import httpx
import pytest
from test_pacing import Clock, configure_clock

from spotify_local_migrator.errors import RateLimitError
from spotify_local_migrator.spotify.client import SpotifyClient
from spotify_local_migrator.spotify.rate_limits import CooldownStore


@pytest.fixture
def automatic_api(make_api, settings, token_body):
    settings.wait_for_rate_limits = True

    def factory(handler, *, interval=0):
        clock = Clock()

        def transport(request):
            if request.url.host == "accounts.spotify.com":
                return httpx.Response(200, json=token_body)
            return handler(request, clock)

        api, _, _ = make_api(transport)
        configure_clock(api, clock, interval=interval)
        return api, clock

    return factory


def test_day_long_cooldown_finishes_and_refreshes_token_before_retry(automatic_api):
    calls = []

    def handler(request, clock):
        calls.append((clock.now, request.headers["Authorization"]))
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "86400"})
        return httpx.Response(200, json={"id": "account"})

    api, clock = automatic_api(handler)
    api.auth.clock = clock
    assert api.current_user() == {"id": "account"}
    assert calls == [(1000, "Bearer access-secret"), (87400, "Bearer new-access-secret")]
    assert sum(clock.delays) == 86400
    assert max(clock.delays) == 60
    assert api.pacer.usage() == 1  # Yesterday's rejected request expired.
    assert json.loads(api.cooldown.path.read_text())["until"] == 87400
    assert api.cooldown_generation == 1


def test_restarted_client_waits_for_existing_cooldown_without_extending_it(automatic_api):
    calls = []

    def handler(request, clock):
        calls.append(clock.now)
        return httpx.Response(200, json={})

    api, clock = automatic_api(handler)
    api.cooldown.save(120)
    restarted = SpotifyClient(api.settings, api.auth, api.client, clock=clock, sleep=clock.sleep)
    restarted.current_user()
    assert calls == [1120]
    assert sum(clock.delays) == 120
    assert json.loads(api.cooldown.path.read_text())["until"] == 1120


def test_repeated_rate_limits_do_not_use_up_network_retry_budget(automatic_api):
    calls = []

    def handler(request, clock):
        calls.append(clock.now)
        if len(calls) <= 5:
            return httpx.Response(429, headers={"Retry-After": "5"})
        return httpx.Response(200, json={})

    api, clock = automatic_api(handler)
    api.current_user()
    assert calls == [1000, 1005, 1010, 1015, 1020, 1025]
    assert sum(clock.delays) == 25


@pytest.mark.parametrize("quota", [False, True])
def test_missing_reset_time_uses_increasing_persisted_backoff(automatic_api, quota):
    calls = []

    def handler(request, clock):
        calls.append(clock.now)
        if len(calls) <= 2:
            return httpx.Response(
                429, json={"error": {"reason": "QUOTA_EXCEEDED"} if quota else {}}
            )
        return httpx.Response(200, json={})

    api, clock = automatic_api(handler)
    api.current_user()
    initial = 3600 if quota else 60
    assert calls == [1000, 1000 + initial, 1000 + initial * 3]
    record = json.loads(api.cooldown.path.read_text())
    assert record["estimated"]
    assert record["retry_after"] == initial * 2
    assert record["reason"] == ("QUOTA_EXCEEDED" if quota else None)


@pytest.mark.parametrize("quota", [False, True])
def test_short_burst_throttling_slows_pacing_but_quota_does_not(automatic_api, quota):
    calls = []

    def handler(request, clock):
        calls.append(clock.now)
        if len(calls) == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "2"},
                json={"error": {"reason": "QUOTA_EXCEEDED"} if quota else {}},
            )
        return httpx.Response(200, json={})

    api, clock = automatic_api(handler, interval=3)
    api.current_user()
    assert calls == [1000, 1003 if quota else 1006]
    assert api.pacer.effective_interval() == (3 if quota else 6)
    assert max(clock.delays) <= 60


def test_ctrl_c_keeps_persisted_cooldown_for_next_run(automatic_api):
    calls = []

    def handler(request, clock):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "86400"})

    api, clock = automatic_api(handler)

    def stop(delay):
        clock.now += 10
        raise KeyboardInterrupt

    api.sleep = stop
    with pytest.raises(KeyboardInterrupt):
        api.current_user()
    assert len(calls) == 1
    assert api.cooldown.remaining() == 86390
    with pytest.raises(RateLimitError):
        api.cooldown.check()


def test_concurrent_extension_is_honored_and_shorter_wait_cannot_overwrite_it(tmp_path):
    clock = Clock()
    cooldown = CooldownStore(tmp_path, "client", clock=clock)
    cooldown.save(30)

    def sleep(delay):
        clock.sleep(delay)
        if clock.now == 1030:
            other = CooldownStore(tmp_path, "client", clock=clock)
            other.save(120)
            cooldown.save(5)

    cooldown.wait(sleep=sleep, notify=lambda _: None)
    assert clock.now == 1150
    assert max(clock.delays) <= 60


def test_missing_quota_reset_backoff_is_bounded_at_one_day(tmp_path):
    clock = Clock()
    cooldown = CooldownStore(tmp_path, "client", clock=clock)
    for _ in range(10):
        delay = cooldown.fallback_delay("QUOTA_EXCEEDED")
        cooldown.save(delay, reason="QUOTA_EXCEEDED", estimated=True)
        clock.now += delay
    assert delay == 86400


def test_cooldown_during_pacing_wait_is_observed_before_dispatch(automatic_api):
    calls = []

    def handler(request, clock):
        calls.append(clock.now)
        return httpx.Response(200, json={})

    api, clock = automatic_api(handler, interval=3)
    api.current_user()
    extended = False

    def sleep(delay):
        nonlocal extended
        clock.sleep(delay)
        if not extended:
            extended = True
            api.cooldown.save(120)

    api.pacer.sleep = sleep
    api.current_user()
    assert calls == [1000, 1123]
