import json
from io import StringIO

import pytest
from rich.console import Console
from test_matching import candidate, local
from test_migration import A, B, C, raw_track
from test_migration import job as job
from typer.testing import CliRunner

from spotify_local_migrator import cli
from spotify_local_migrator.matching.cache import SearchCache
from spotify_local_migrator.matching.config import MatchingConfig
from spotify_local_migrator.matching.models import MatchDecision
from spotify_local_migrator.matching.normalize import prepare_track
from spotify_local_migrator.matching.scoring import (
    decide,
    group_candidates,
    refresh_automatic_choices,
)
from spotify_local_migrator.matching.search import CatalogueSearch, candidate_from_api
from spotify_local_migrator.migration.executor import load_plan
from spotify_local_migrator.migration.jobs import content_hash
from spotify_local_migrator.ui.review import review_decision


def release(track_id, *, explicit=True, isrc="SAME", order=None, **kwargs):
    result = candidate(track_id=track_id, isrc=isrc, **kwargs)
    result.raw = {"explicit": explicit}
    result.search_order = order
    return result


def match(candidates, track=None):
    track = track or local()
    return decide(track, prepare_track(track, track.artists, []), candidates, MatchingConfig())


def test_album_duplicates_choose_first_search_result_and_keep_all_evidence():
    first = release("Z" * 22, order=0)
    second = release("A" * 22, order=1)
    second.album = "Deluxe Album"
    decision = match([second, first])
    assert decision.status == "auto"
    assert decision.candidate.spotify_id == first.spotify_id
    assert len(group_candidates(decision.candidates)) == 1
    assert {item.spotify_id for item in decision.candidates} == {
        first.spotify_id,
        second.spotify_id,
    }
    # Re-ranking a serialized job must retain Spotify order, not ID/score order.
    saved = MatchDecision.model_validate_json(decision.model_dump_json())
    assert match(saved.candidates).candidate.spotify_id == first.spotify_id


def test_explicit_preferred_then_first_explicit_album_even_with_different_isrc():
    clean = release("B" * 22, explicit=False, isrc="CLEAN", order=0)
    first_explicit = release("Z" * 22, isrc="EXPLICIT", order=1)
    later_explicit = release("A" * 22, isrc="EXPLICIT", order=2)
    decision = match([clean, later_explicit, first_explicit])
    assert decision.status == "auto"
    assert decision.candidate.spotify_id == first_explicit.spotify_id
    assert len(group_candidates(decision.candidates)) == 1


@pytest.mark.parametrize("playable", [False, None])
def test_confirmed_playable_release_takes_priority_over_unavailable_explicit(playable):
    clean = release("B" * 22, explicit=False, isrc="CLEAN")
    explicit = release("C" * 22, isrc="EXPLICIT")
    explicit.is_playable = playable
    decision = match([explicit, clean])
    assert decision.status == "auto"
    assert decision.candidate.spotify_id == clean.spotify_id


def test_explicit_flag_does_not_promote_a_different_song():
    clean = release("Z" * 22, explicit=False)
    different = release("A" * 22, title="Completely Different Song")
    decision = match([different, clean])
    assert len(group_candidates(decision.candidates)) == 2
    assert decision.candidate.spotify_id == clean.spotify_id


@pytest.mark.parametrize(
    "title,artists,duration",
    [
        ("Song (Remix)", ["Artist"], 180000),
        ("Song", ["Different Artist"], 180000),
        ("Song", ["Artist"], 182001),
    ],
)
def test_content_preference_requires_matching_title_version_artists_and_duration(
    title, artists, duration
):
    clean = release("B" * 22, explicit=False, isrc="CLEAN")
    other = release("C" * 22, isrc="EXPLICIT", title=title, artists=artists, duration=duration)
    assert len(group_candidates(match([clean, other]).candidates)) == 2


def test_matching_artist_names_with_conflicting_artist_ids_remain_separate():
    clean = release("B" * 22, explicit=False)
    explicit = release("C" * 22)
    clean.artist_ids = ["artist-one"]
    explicit.artist_ids = ["artist-two"]
    assert len(group_candidates(match([clean, explicit]).candidates)) == 2


@pytest.mark.parametrize("clean_first", [False, True])
def test_clean_counterpart_cannot_bridge_two_distinct_explicit_recordings(clean_first):
    clean = release("B" * 22, explicit=False, isrc="CLEAN")
    first = release("C" * 22, isrc="RECORDING-ONE")
    second = release("D" * 22, isrc="RECORDING-TWO")
    candidates = [clean, first, second] if clean_first else [first, clean, second]
    decision = match(candidates)
    assert len(group_candidates(decision.candidates)) == 2
    assert decision.needs_review
    assert "Another recording has a similar score" in decision.reasons


def test_unknown_isrcs_do_not_collapse_album_duplicates():
    decision = match([release("B" * 22, isrc=None), release("C" * 22, isrc=None)])
    assert len(group_candidates(decision.candidates)) == 2
    assert decision.needs_review


@pytest.mark.parametrize("flag", [None, "true", "false", 0, 1])
def test_unknown_or_malformed_content_rating_does_not_identify_clean_counterpart(flag):
    unknown = release("B" * 22, explicit=flag, isrc="UNKNOWN")
    explicit = release("C" * 22, isrc="EXPLICIT")
    assert unknown.explicit is None
    assert len(group_candidates(match([unknown, explicit]).candidates)) == 2


def test_requested_corey_lingo_example_groups_albums_but_keeps_title_review():
    track = local('Hit You Up "got it bad" (prod. Maxim)', ["Corey Lingo"], 181000)
    releases = [
        release(
            track_id,
            title="Hit You Up",
            artists=["Corey Lingo"],
            duration=181714,
            isrc="QZS662415195",
        )
        for track_id in [
            "1O0baUnPG9hOF9HDE5jPNE",
            "5HPaHqOxyggijogyz5SdOQ",
            "5p2IwjuuzwmgRsxOvnZrJA",
        ]
    ]
    for result, album in zip(
        releases,
        ["For What It's Worth", "Hit You Up", "For What It's Worth (Deluxe Edition)"],
        strict=True,
    ):
        result.album = album
    decision = match(releases, track)
    groups = group_candidates(decision.candidates)
    assert len(groups) == 1
    assert groups[0][0].album == "For What It's Worth"
    assert groups[0][0].explicit is True
    assert groups[0][0].score == pytest.approx(0.81, abs=0.01)
    assert decision.needs_review and decision.candidate is None
    assert "Title evidence is insufficient" in decision.reasons


def test_preferences_do_not_bypass_confidence_when_preferred_album_scores_lower():
    track = local()
    track.album = "Preferred Album"
    first = release("Z" * 22)
    first.album = "Unrelated Album Name"
    second = release("A" * 22)
    second.album = track.album
    decision = decide(
        track,
        prepare_track(track, ["Artist"], []),
        [first, second],
        MatchingConfig(auto_threshold=0.99),
    )
    assert decision.candidates[0].spotify_id == first.spotify_id
    assert decision.candidates[0].score < decision.candidates[1].score
    assert decision.needs_review


def test_search_order_is_first_seen_across_queries_duplicate_pages_and_cache(tmp_path):
    class Client:
        calls = 0

        def search_tracks(self, query, *, market, offset):
            self.calls += 1
            ids = [B, A] if self.calls == 1 else [A, C, B]
            return {
                "tracks": {
                    "items": [raw_track(item) for item in ids],
                    "next": "next-page" if self.calls == 1 else None,
                }
            }

    client = Client()
    with SearchCache(tmp_path / "search.sqlite3") as cache:
        search = CatalogueSearch(client, MatchingConfig(), "account", cache=cache)
        prepared = prepare_track(local(), ["Artist"], [])
        candidates, queries = search.for_track(prepared)
        assert client.calls == 4
        cached, _ = search.for_track(prepared)
        assert client.calls == 4
        assert cached == candidates
    assert [item.spotify_id for item in candidates] == [B, A, C]
    assert [item.search_order for item in candidates] == [0, 1, 2]
    assert candidates[0].queries == queries


def test_refresh_saved_automatic_choices_preserves_human_approvals_and_rejections(job):
    _, _, report, _ = job
    decision = report.decisions[0]
    clean = candidate_from_api({**raw_track(A), "explicit": False})
    explicit = candidate_from_api({**raw_track(C), "explicit": True})
    decision.candidates = [clean, explicit]
    decision.candidate = clean
    decision.status = "auto"
    decision.review_completed = False
    human_approved = report.decisions[1].model_copy(deep=True)
    report.decisions[2].status = "rejected"
    report.decisions[2].candidate = None
    human_rejected = report.decisions[2].model_copy(deep=True)
    assert refresh_automatic_choices(report)
    assert report.decisions[0].candidate.spotify_id == C
    assert report.decisions[1] == human_approved
    assert report.decisions[2] == human_rejected
    assert not refresh_automatic_choices(report)


def test_old_report_serialization_preserves_saved_plan_hashes(job):
    store, _, report, plan = job
    legacy = report.model_dump(mode="json")
    for decision in legacy["decisions"]:
        for item in decision["candidates"] + (
            [decision["candidate"]] if decision["candidate"] else []
        ):
            item.pop("search_order", None)
    store.save("matches.json", legacy)
    plan.matches_hash = content_hash(legacy)
    store.save("plan.json", plan)
    assert content_hash(store.report()) == plan.matches_hash
    assert load_plan(store).matches_hash == plan.matches_hash


def test_saved_offline_review_groups_duplicates_and_approves_explicit_without_api(
    job, settings, monkeypatch
):
    store, _, report, _ = job
    decision = report.decisions[0]
    decision.candidates = [
        candidate_from_api({**raw_track(A), "explicit": False}),
        candidate_from_api({**raw_track(C), "explicit": True}),
    ]
    decision.candidate = None
    decision.status = "unmatched"
    decision.needs_review = True
    decision.review_completed = False
    store.save("matches.json", report)
    monkeypatch.setattr(cli, "load_settings", lambda env_file: settings)
    monkeypatch.setattr(cli, "services", lambda *args: pytest.fail("must not contact Spotify"))
    result = CliRunner().invoke(
        cli.app, ["review", "--offline", "--job", str(store.directory)], input="1\n"
    )
    assert result.exit_code == 0, result.output
    assert "Explicit | 1 alternate release(s)" in result.output
    assert "2. Search manually" in result.output
    saved = store.report().decisions[0]
    assert saved.candidate.spotify_id == C
    assert saved.review_completed and saved.status == "approved"
    assert len(saved.candidates) == 2


def test_manual_search_groups_releases_before_limiting_review_choices(monkeypatch):
    from rich.prompt import IntPrompt, Prompt

    decision = match([candidate()])
    queries = []
    explicit = release("Z" * 22, isrc="EXPLICIT")
    explicit.album = "First Explicit Album"
    later = [release(f"{index:022d}", isrc="EXPLICIT") for index in range(11)]
    clean = release("C" * 22, explicit=False, isrc="CLEAN")

    class Search:
        def query(self, query):
            queries.append(query)
            return [clean, *later, explicit]

    # One existing choice -> search; one grouped match -> scores, then approve.
    choices = iter([2, 4, 1])
    monkeypatch.setattr(IntPrompt, "ask", lambda *args, **kwargs: next(choices))
    monkeypatch.setattr(Prompt, "ask", lambda *args, **kwargs: "song artist")
    output = StringIO()
    review_decision(Console(file=output, width=200), decision, Search(), MatchingConfig())
    assert queries == ["song artist"]
    assert decision.candidate.spotify_id == later[0].spotify_id
    assert decision.candidate.explicit is True
    assert len(decision.candidates) == 14  # all manual results, including beyond 10
    assert "First Explicit Album" in output.getvalue()  # alternate release in scores


def test_saved_dry_run_refreshes_auto_content_preference_without_api(job, settings, monkeypatch):
    store, _, report, _ = job
    decision = report.decisions[0]
    clean = candidate_from_api({**raw_track(A), "explicit": False})
    explicit = candidate_from_api({**raw_track(C), "explicit": True})
    decision.candidates = [clean, explicit]
    decision.candidate = clean
    decision.status = "auto"
    decision.review_completed = False
    store.save("matches.json", report)
    monkeypatch.setattr(cli, "load_settings", lambda env_file: settings)
    monkeypatch.setattr(cli, "services", lambda *args: pytest.fail("must not contact Spotify"))
    result = CliRunner().invoke(
        cli.app, ["migrate", "--job", str(store.directory), "--dry-run", "--no-review"]
    )
    assert result.exit_code == 0, result.output
    assert store.report().decisions[0].candidate.spotify_id == C
    saved_plan = json.loads((store.directory / "plan.json").read_text())
    assert saved_plan["desired"][0]["uri"] == "spotify:track:" + C
