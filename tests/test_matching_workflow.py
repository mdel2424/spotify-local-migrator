from copy import deepcopy

import pytest
from pydantic import ValidationError
from rich.console import Console
from test_migration import A, B, raw_track
from test_migration import job as job

from spotify_local_migrator.errors import SpotifyAPIError, StateError
from spotify_local_migrator.matching.cache import SearchCache
from spotify_local_migrator.matching.config import MatchingConfig, load_matching_config
from spotify_local_migrator.matching.engine import new_report, run_matching
from spotify_local_migrator.matching.normalize import prepare_track
from spotify_local_migrator.matching.scoring import decide
from spotify_local_migrator.matching.search import candidate_from_api
from spotify_local_migrator.migration.jobs import JobStore
from spotify_local_migrator.migration.scanner import parse_entry
from spotify_local_migrator.ui.review import review_report
from spotify_local_migrator.workflow import account_id


def test_current_account_id_preferred_over_legacy_id():
    class Client:
        def current_user(self):
            return {"account_id": "stable", "id": "legacy"}

    assert account_id(Client()) == "stable"


@pytest.mark.parametrize(
    "settings",
    [
        {"review_threshold": 0.95, "auto_threshold": 0.90},
        {"minimum_margin": -0.1},
        {"auto_threshold": 1.1},
        {"weights": {"title": 0}},
        {"weights": {"artist": 0}},
        {"search_pages": 0},
        {"market": "Canada"},
    ],
)
def test_invalid_matching_configuration_is_rejected(settings):
    with pytest.raises(ValidationError):
        MatchingConfig(**settings)


def test_yaml_expected_artists_and_weights(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "auto_threshold: 0.95\nminimum_margin: 0.07\n"
        "weights:\n  title: 0.6\n  artist: 0.25\n  duration: 0.1\n  album: 0.05\n"
        "playlists:\n  dxrt:\n    expected_artists:\n      - 2sdxrt3all\n"
    )
    config = load_matching_config(path)
    assert config.auto_threshold == 0.95
    assert config.playlists["dxrt"].expected_artists == ["2sdxrt3all"]
    assert config.weights.title == 0.6


def test_custom_thresholds_and_ambiguity_margin(capture):
    local = capture.local_tracks[0]
    prepared = prepare_track(local, ["Westside Gunn"], [])
    candidates = [candidate_from_api(raw_track(A)), candidate_from_api(raw_track(B))]
    assert decide(local, prepared, candidates, MatchingConfig()).needs_review
    assert decide(local, prepared, candidates, MatchingConfig(minimum_margin=0)).status == "auto"
    assert (
        decide(local, prepared, candidates[:1], MatchingConfig(auto_threshold=1)).status == "auto"
    )


@pytest.mark.parametrize(
    "title,cleaned",
    [
        ("2sdxrt3all - push (@wizardpem @whyceg)", "push"),
        ("2sdxrt3all - Brr Bow [Dir. @drevegas] *Video Link In Description*", "Brr Bow"),
        ("2sdxrt3all - 2 Souls (p. whyceg) [djslimebxll + eevergrowth]", "2 Souls"),
        ("2sdxrt3all - SIXFLAGS (whyceg + rioleyva)", "SIXFLAGS"),
        ("2sdxrt3all - whoopty whoop (reesy4k x gelatoo)", "whoopty whoop (reesy4k x gelatoo)"),
    ],
)
def test_more_real_scan_annotations(capture, title, cleaned):
    local = capture.local_tracks[0].model_copy(update={"title": title})
    assert prepare_track(local, ["2sdxrt3all"], ["whyceg"]).title == cleaned


def test_exact_title_and_duration_with_wrong_artist_require_human_confirmation(capture):
    local = capture.local_tracks[0]
    raw = raw_track(A)
    raw["artists"] = [{"name": "Completely Different Person"}]
    candidate = candidate_from_api(raw)
    prepared = prepare_track(local, ["Westside Gunn"], [])
    decision = decide(local, prepared, [candidate], MatchingConfig())
    assert decision.status == "unmatched"
    assert decision.needs_review
    assert decision.candidate is None
    assert decision.candidates[0].score < 0.7


def test_cache_expiry_and_permissions(tmp_path):
    with SearchCache(tmp_path / "search.sqlite3", ttl_days=0) as cache:
        cache.put("key", {"tracks": {"items": []}})
        assert cache.get("key") is None
    assert (tmp_path / "search.sqlite3").stat().st_mode & 0o777 == 0o600


def test_matching_resumes_without_researching_checkpointed_positions(settings, capture, raw_local):
    capture = capture.model_copy(deep=True)
    second = deepcopy(raw_local)
    second["item"]["name"] = "Other Song"
    second["item"]["uri"] = "spotify:local:Westside+Gunn::Other+Song:241"
    capture.entries.append(parse_entry(second, 1))
    store = JobStore.create(settings.data_dir, capture)
    report = new_report(capture, MatchingConfig(), "account", ["Westside Gunn"])

    class Search:
        account_id = "account"
        requests = 0
        fail = True
        queries = []

        def for_track(self, prepared):
            self.queries.append(prepared.title)
            if prepared.title == "Other Song" and self.fail:
                raise SpotifyAPIError("search interrupted")
            return [candidate_from_api(raw_track(A, prepared.title))], [prepared.core_title]

    search = Search()
    with pytest.raises(SpotifyAPIError):
        run_matching(store, report, search, live_api=True)
    checkpoint = store.report()
    assert len(checkpoint.decisions) == 1
    assert not checkpoint.matching_complete
    search.fail = False
    run_matching(store, checkpoint, search, live_api=True)
    assert search.queries.count("Elizabeth") == 1
    assert store.report().matching_complete
    assert store.report().real_api_validation


def test_matching_resume_account_binding(settings, capture):
    store = JobStore.create(settings.data_dir, capture)
    report = new_report(capture, MatchingConfig(), "account")

    class Search:
        account_id = "someone-else"

    with pytest.raises(StateError, match="account"):
        run_matching(store, report, Search())


def test_interactive_approval_persists_and_resume_does_not_repeat(job, monkeypatch):
    from rich.prompt import IntPrompt

    store, _, report, _ = job
    decision = report.decisions[0]
    decision.status = "unmatched"
    decision.candidate = None
    decision.needs_review = True
    decision.review_completed = False
    monkeypatch.setattr(IntPrompt, "ask", lambda *args, **kwargs: 1)

    class Search:
        def query(self, query):
            raise AssertionError("Selecting existing candidate must not search.")

    review_report(Console(file=None, quiet=True), store, report, Search())
    saved = store.report().decisions[0]
    assert saved.status == "approved"
    assert saved.review_completed and not saved.needs_review
    monkeypatch.setattr(IntPrompt, "ask", lambda *args, **kwargs: pytest.fail("already reviewed"))
    review_report(Console(quiet=True), store, store.report(), Search())


def test_manual_search_and_rejection_are_saved(job, monkeypatch):
    from rich.prompt import IntPrompt, Prompt

    store, _, report, _ = job
    decision = report.decisions[0]
    decision.needs_review = True
    decision.candidate = None
    decision.status = "unmatched"
    decision.review_completed = False
    choices = iter([2, 1])  # one candidate -> manual option 2 -> approve result 1
    monkeypatch.setattr(IntPrompt, "ask", lambda *args, **kwargs: next(choices))
    monkeypatch.setattr(Prompt, "ask", lambda *args, **kwargs: "another query")

    class Search:
        def query(self, query):
            assert query == "another query"
            return [candidate_from_api(raw_track(B))]

    review_report(Console(quiet=True), store, report, Search())
    saved = store.report().decisions[0]
    assert saved.candidate.spotify_id == B
    assert "another query" in saved.searched_queries


def test_review_locks_decisions_after_apply_starts(job):
    store, _, report, _ = job
    store.save("migration.json", {})
    with pytest.raises(StateError, match="locked"):
        review_report(Console(quiet=True), store, report, None)


@pytest.mark.parametrize(
    "raw",
    [
        {"type": "track", "id": A, "uri": "spotify:track:" + A, "artists": None},
        {**raw_track(A), "artists": [None, "bad"]},
        {**raw_track(A), "album": []},
        {**raw_track(A), "duration_ms": True},
    ],
)
def test_malformed_catalogue_items_are_ignored(raw):
    assert candidate_from_api(raw) is None


def test_score_breakdown_explains_missing_evidence_confidence_cap(capture):
    from spotify_local_migrator.matching.scoring import score_candidate

    local = capture.local_tracks[0].model_copy(update={"duration_ms": None})
    prepared = prepare_track(local, ["Westside Gunn"], [])
    scored = score_candidate(local, prepared, candidate_from_api(raw_track(A)), MatchingConfig())
    values = scored.score_breakdown
    assert values["weighted_total"] == pytest.approx(1)
    assert values["confidence_adjustment"] == pytest.approx(-0.11)
    assert values["weighted_total"] + values["penalties"] + values[
        "confidence_adjustment"
    ] == pytest.approx(scored.score)
