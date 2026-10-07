import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import httpx
import pytest

from spotify_local_migrator.config import READ_SCOPES, WRITE_SCOPES
from spotify_local_migrator.errors import RequestBudgetError, StateError
from spotify_local_migrator.matching.cache import SearchCache
from spotify_local_migrator.matching.config import MatchingConfig
from spotify_local_migrator.matching.engine import new_report, run_matching
from spotify_local_migrator.matching.normalize import prepare_track
from spotify_local_migrator.matching.search import CatalogueSearch
from spotify_local_migrator.migration.jobs import JobStore
from spotify_local_migrator.migration.scanner import parse_entry
from spotify_local_migrator.spotify import pacing
from spotify_local_migrator.spotify.pacing import WINDOW_SECONDS, RequestPacer


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.delays = []

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.delays.append(delay)
        self.now += delay


def pacer(data_dir, clock, *, budget=400, interval=3):
    return RequestPacer(data_dir, interval=interval, budget=budget, clock=clock, sleep=clock.sleep)


def configure_clock(api, clock, *, interval=3):
    api.pacer.clock = api.cooldown.clock = clock
    api.pacer.sleep = api.sleep = clock.sleep
    api.pacer.interval = interval


def test_spacing_survives_restarts_and_idle_time(tmp_path):
    clock = Clock()
    first = pacer(tmp_path, clock)
    first.before_request()
    clock.now += 1
    restarted = pacer(tmp_path, clock)
    restarted.before_request()
    clock.now += 10
    restarted.before_request()
    assert clock.delays == [2]
    assert restarted.usage() == (3, None)
    assert json.loads(first.path.read_text())["requests"] == [1000, 1003, 1013]
    assert first.path.stat().st_mode & 0o777 == 0o600
    assert first.path.with_suffix(".lock").stat().st_mode & 0o777 == 0o600


def test_rolling_budget_stops_without_sleep_and_expires_at_boundary(tmp_path):
    clock = Clock()
    limiter = pacer(tmp_path, clock, budget=2)
    limiter.before_request()
    limiter.before_request()
    restarted = pacer(tmp_path, clock, budget=2)
    with pytest.raises(RequestBudgetError, match="No API request was sent"):
        restarted.before_request()
    assert restarted.usage() == (2, 1000 + WINDOW_SECONDS)
    assert clock.delays == [3]
    clock.now = 1000 + WINDOW_SECONDS
    restarted.before_request()
    assert json.loads(restarted.path.read_text())["requests"] == [1003, clock.now]
    assert clock.delays == [3]


def test_recent_cache_seeds_budget_and_reports_correct_slot_when_over_budget(tmp_path):
    clock = Clock()
    clock.now = 100000
    limiter = pacer(tmp_path, clock, budget=2)
    timestamps = [1000, 99900, 99910, 99920, 99930]
    with SearchCache(limiter.cache_path) as cache:
        for index, stamp in enumerate(timestamps):
            cache.put(str(index), {"tracks": {"items": []}})
            cache.connection.execute(
                "UPDATE searches SET created = ? WHERE key = ?", (stamp, str(index))
            )
        cache.connection.commit()
    assert limiter.usage() == (4, 99920 + WINDOW_SECONDS)
    assert not limiter.path.exists()  # Status remains read-only.
    with pytest.raises(RequestBudgetError):
        limiter.before_request()
    clock.now = 99920 + WINDOW_SECONDS
    limiter.before_request()
    assert limiter.usage()[0] == 2


@pytest.mark.parametrize(
    "contents",
    [
        "private-corrupt-content",
        "{}",
        '{"requests": [1000, 999]}',
        '{"requests": [NaN]}',
        '{"requests": [-1]}',
        '{"schema_version": 2, "requests": []}',
    ],
)
def test_corrupt_history_stops_without_resetting_usage(tmp_path, contents):
    limiter = pacer(tmp_path, Clock())
    limiter.path.parent.mkdir(parents=True)
    limiter.path.write_text(contents)
    with pytest.raises(StateError) as error:
        limiter.before_request()
    assert "private-corrupt-content" not in str(error.value)
    assert limiter.path.read_text() == contents


def test_failed_usage_write_blocks_http(make_api, monkeypatch):
    calls = []
    api, _, _ = make_api(lambda request: calls.append(request))

    def fail_write(*args):
        raise OSError("private-disk-detail")

    monkeypatch.setattr(pacing, "atomic_write_json", fail_write)
    with pytest.raises(StateError, match="no API request was sent"):
        api.current_user()
    assert calls == []


def test_simultaneous_clients_share_budget_without_lost_updates(tmp_path):
    def reserve(_):
        limiter = pacer(tmp_path, Clock(), budget=8, interval=0)
        try:
            limiter.before_request()
            return True
        except RequestBudgetError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(reserve, range(16)))
    assert sum(outcomes) == 8
    assert pacer(tmp_path, Clock(), budget=8).usage()[0] == 8


def test_wait_rechecks_usage_reserved_by_another_command(tmp_path):
    clock = Clock()
    limiter = pacer(tmp_path, clock)
    limiter.before_request()
    interrupted = False

    def sleep(delay):
        nonlocal interrupted
        clock.sleep(delay)
        if not interrupted:
            interrupted = True
            pacer(tmp_path, clock).before_request()

    limiter.sleep = sleep
    limiter.before_request()
    assert clock.delays == [3, 3]
    assert json.loads(limiter.path.read_text())["requests"] == [1000, 1003, 1006]


@pytest.mark.parametrize("first_response", ["success", "network", "503", "429", "401"])
def test_every_actual_attempt_is_paced_including_retries(make_api, token_body, first_response):
    clock = Clock()
    calls = []

    def handler(request):
        if request.url.host == "accounts.spotify.com":
            return httpx.Response(200, json=token_body)
        calls.append(clock.now)
        if len(calls) == 1:
            if first_response == "network":
                raise httpx.ConnectError("offline", request=request)
            if first_response == "429":
                return httpx.Response(429, headers={"Retry-After": "7"})
            if first_response in {"503", "401"}:
                return httpx.Response(int(first_response))
        return httpx.Response(200, json={"id": "account"})

    api, _, _ = make_api(handler)
    configure_clock(api, clock)
    api.current_user()
    if first_response == "success":
        api.current_user()
    assert calls == [1000, 1007 if first_response == "429" else 1003]
    assert api.pacer.usage()[0] == 2


def test_reads_and_writes_share_spacing_and_budget_across_clients(make_api, settings):
    clock = Clock()
    calls = []

    def handler(request):
        calls.append((request.method, clock.now))
        return httpx.Response(200, json={"snapshot_id": "changed"})

    settings.request_budget_24h = 3
    first, auth, _ = make_api(handler)
    configure_clock(first, clock)
    token = auth.store.load()
    token.scopes = list(READ_SCOPES + WRITE_SCOPES)
    auth.store.save(token)
    first.current_user()
    first.add_tracks("A" * 22, ["spotify:track:" + "B" * 22], 0)
    first.remove_positions("A" * 22, [1], "changed")
    second, _, _ = make_api(handler)
    configure_clock(second, clock)
    with pytest.raises(RequestBudgetError):
        second.current_user()
    assert calls == [("GET", 1000), ("POST", 1003), ("DELETE", 1006)]
    assert not (settings.data_dir / ".cache/rate-limit.json").exists()


def test_retry_cannot_exceed_local_budget(make_api, settings):
    calls = []
    settings.request_budget_24h = 1

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    api, _, _ = make_api(handler)
    with pytest.raises(RequestBudgetError):
        api.current_user()
    assert len(calls) == 1


def test_cached_candidates_need_no_request_budget(make_api, settings, capture):
    calls = []
    settings.request_budget_24h = 3

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"tracks": {"items": [], "next": None}})

    api, _, _ = make_api(handler)
    prepared = prepare_track(capture.local_tracks[0], ["Westside Gunn"], [])
    with SearchCache(settings.data_dir / ".cache/search.sqlite3") as cache:
        search = CatalogueSearch(api, MatchingConfig(), "account", cache=cache)
        search.for_track(prepared)
        search.for_track(prepared)
        assert cache.hits == 3
    assert len(calls) == 3
    assert api.pacer.usage()[0] == 3


def test_matching_budget_pause_preserves_checkpoint_and_partial_searches(
    make_api, settings, capture, raw_local
):
    calls = []
    clock = Clock()
    settings.request_budget_24h = 4

    def handler(request):
        calls.append(request.url.params["q"])
        return httpx.Response(200, json={"tracks": {"items": [], "next": None}})

    api, _, _ = make_api(handler)
    configure_clock(api, clock, interval=0)
    capture = capture.model_copy(deep=True)
    other = deepcopy(raw_local)
    other["item"]["name"] = "Other Song"
    other["item"]["uri"] = "spotify:local:Westside+Gunn::Other+Song:241"
    capture.entries.append(parse_entry(other, 1))
    store = JobStore.create(settings.data_dir, capture)
    config = MatchingConfig()
    report = new_report(capture, config, "account", ["Westside Gunn"])
    with SearchCache(settings.data_dir / ".cache/search.sqlite3") as cache:
        search = CatalogueSearch(api, config, "account", cache=cache)
        with pytest.raises(RequestBudgetError):
            run_matching(store, report, search)
        checkpoint = store.report()
        assert len(checkpoint.decisions) == 1
        assert not checkpoint.matching_complete
        clock.now += WINDOW_SECONDS
        restarted, _, _ = make_api(handler)
        configure_clock(restarted, clock, interval=0)
        resumed_search = CatalogueSearch(restarted, config, "account", cache=cache)
        run_matching(store, checkpoint, resumed_search)
    assert store.report().matching_complete
    assert len(store.report().decisions) == 2
    assert len(calls) == 6  # Completed track and partial-query cache were reused.
