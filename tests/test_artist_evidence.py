from contextlib import contextmanager

import pytest
from rich.console import Console
from test_matching import candidate, local
from test_migration import A, C, raw_track
from test_migration import job as job
from typer.testing import CliRunner

from spotify_local_migrator import cli
from spotify_local_migrator.matching.config import MatchingConfig
from spotify_local_migrator.matching.normalize import prepare_track
from spotify_local_migrator.matching.scoring import decide
from spotify_local_migrator.matching.search import (
    CatalogueSearch,
    candidate_from_api,
    search_queries,
)
from spotify_local_migrator.workflow import match_job


def waco(title="Corbin - Waco", *, artists=None, duration=159000):
    track = local(title, ["heat i want on sc"] if artists is None else artists, duration)
    track.album = "p22"
    result = candidate("Waco", ["Corbin"], 159363)
    result.album = "Ghost With Skin"
    return track, result


@pytest.mark.parametrize("title", ["Corbin - Waco", "CORBIN — Waco", "Uploader - Corbin - Waco"])
def test_corroborated_title_artist_outweighs_uploader_metadata_without_lowering_thresholds(title):
    track, result = waco(title)
    prepared = prepare_track(track, [], [])
    assert prepared.title == title  # The prefix is not stripped on a guess.
    decision = decide(track, prepared, [result], MatchingConfig())
    assert decision.status == "auto"
    assert decision.candidate.score == pytest.approx(0.95)
    assert decision.candidate.score_breakdown["title"] == 1
    assert decision.candidate.score_breakdown["artist_minimum"] == 1
    assert decision.prepared.title == "Waco"
    assert [artist.casefold() for artist in decision.prepared.artists] == ["corbin"]
    assert decision.prepared.artist_source == "title"
    assert decision.prepared.ignored_artist_tags == ["heat i want on sc"]
    assert track.title == title and track.artists == ["heat i want on sc"]


def test_wrong_catalogue_artist_cannot_borrow_an_embedded_artist_credit():
    track, result = waco()
    result.artists = ["Completely Different Person"]
    decision = decide(track, prepare_track(track, [], []), [result], MatchingConfig())
    assert decision.status == "unmatched"
    assert decision.prepared.title == "Corbin - Waco"


def test_ordinary_hyphenated_song_title_is_preserved():
    track = local("Heaven - Hell")
    decision = decide(
        track, prepare_track(track, [], []), [candidate("Heaven - Hell")], MatchingConfig()
    )
    assert decision.status == "auto"
    assert decision.prepared.title == "Heaven - Hell"
    assert decision.prepared.artist_source == "metadata"


def test_artist_name_containing_collaboration_separator_is_one_credit():
    track = local("Hall & Nash - Song", ["Uploader"])
    decision = decide(
        track, prepare_track(track, [], []), [candidate(artists=["Hall & Nash"])], MatchingConfig()
    )
    assert decision.status == "auto"
    assert decision.prepared.artists == ["Hall & Nash"]


@pytest.mark.parametrize("title", ["Corbin - Waco (feat. Guest)", "Corbin feat. Guest - Waco"])
def test_embedded_artist_does_not_erase_explicit_feature_requirements(title):
    track, result = waco(title)
    prepared = prepare_track(track, [], [])
    decision = decide(track, prepared, [result], MatchingConfig())
    assert decision.status != "auto"
    result.artists = ["Corbin", "Guest"]
    decision = decide(track, prepared, [result], MatchingConfig())
    assert decision.status == "auto"
    assert "Guest" in decision.prepared.artists


@pytest.mark.parametrize("suffix", ["", " - Live"])
def test_features_on_both_sides_of_artist_separator_and_versions_remain_required(suffix):
    track, result = waco(f"Corbin feat. Guest - Waco feat. Other{suffix}")
    prepared = prepare_track(track, [], [])
    result.artists = ["Corbin", "Guest"]
    assert decide(track, prepared, [result], MatchingConfig()).status != "auto"
    result.artists.append("Other")
    if suffix:
        assert decide(track, prepared, [result], MatchingConfig()).status != "auto"
        result.title += " - Live"
    decision = decide(track, prepared, [result], MatchingConfig())
    assert decision.status == "auto"
    assert set(decision.prepared.artists) == {"Corbin", "Guest", "Other"}


@pytest.mark.parametrize("qualifier", ["Live", "Remix", "Instrumental", "Sped Up"])
def test_embedded_artist_does_not_bypass_version_conflicts(qualifier):
    track, result = waco(f"Corbin - Waco ({qualifier})")
    decision = decide(track, prepare_track(track, [], []), [result], MatchingConfig())
    assert decision.status != "auto"
    assert decision.candidates[0].score_breakdown["version_conflict"] == 1


def test_corroborated_prefix_still_needs_duration_and_recording_margin():
    track, result = waco(duration=None)
    decision = decide(track, prepare_track(track, [], []), [result], MatchingConfig())
    assert decision.status != "auto"
    assert "Duration is missing" in decision.reasons
    track.duration_ms = 159000
    other = result.model_copy(update={"spotify_id": C, "uri": "spotify:track:" + C})
    decision = decide(track, prepare_track(track, [], []), [result, other], MatchingConfig())
    assert decision.needs_review and decision.candidate is None
    assert "Another recording has a similar score" in decision.reasons


def test_corroborated_artist_recovers_missing_tags_but_not_filename_identity():
    track, result = waco(artists=[])
    decision = decide(track, prepare_track(track, [], []), [result], MatchingConfig())
    assert decision.status == "auto"
    assert not any("No reliable artist" in warning for warning in decision.prepared.warnings)
    track.title = "Corbin - Track 01"
    result.title = "Track 01"
    decision = decide(track, prepare_track(track, [], []), [result], MatchingConfig())
    assert decision.status != "auto"
    assert decision.prepared.filename_like


@pytest.mark.parametrize("artists", [["Corbin"], ["Shlohmo"], ["Other", "Corbin"]])
def test_playlist_artist_tags_are_alternatives_in_main_or_featured_credits(artists):
    track, result = waco("Waco")
    result.artists = artists
    prepared = prepare_track(track, ["Corbin", "Shlohmo"], [], expected_source="configured")
    decision = decide(track, prepared, [result], MatchingConfig())
    assert decision.status == "auto"
    assert decision.candidate.score_breakdown["artist_minimum"] == 1


def test_playlist_alternatives_still_reject_wrong_artists_and_missing_track_features():
    track, result = waco("Waco (feat. Guest)")
    prepared = prepare_track(track, ["Corbin", "Shlohmo"], [], expected_source="configured")
    result.artists = ["Corbin"]
    assert decide(track, prepared, [result], MatchingConfig()).status != "auto"
    result.artists = ["Unrelated Person", "Guest"]
    assert decide(track, prepared, [result], MatchingConfig()).status != "auto"
    result.artists = ["Corbin", "Guest"]
    assert decide(track, prepared, [result], MatchingConfig()).status == "auto"


def test_prefix_search_finds_correct_track_and_retains_original_title_queries():
    track, _ = waco()
    prepared = prepare_track(track, [], [])

    class Client:
        calls = []

        def search_tracks(self, query, **kwargs):
            self.calls.append(query)
            results = []
            if query == 'track:"waco" artist:"corbin"':
                results = [
                    {
                        **raw_track(A, "Waco"),
                        "artists": [{"name": "Corbin"}],
                        "duration_ms": 159363,
                    }
                ]
            return {"tracks": {"items": results, "next": None}}

    client = Client()
    search = CatalogueSearch(client, MatchingConfig(), "account")
    results, queries = search.for_track(prepared)
    assert 'track:"waco" artist:"corbin"' in client.calls
    assert 'track:"corbin waco"' in client.calls
    assert queries == client.calls
    assert decide(track, prepared, results, MatchingConfig()).status == "auto"


@pytest.mark.parametrize("command", ["match", "migrate"])
def test_artist_flag_aliases_reach_matching_for_new_and_saved_jobs(
    command, job, settings, monkeypatch
):
    store, capture, _, _ = job
    monkeypatch.setattr(cli, "load_settings", lambda env_file: settings)
    seen = []

    @contextmanager
    def services(*args):
        yield None, object()

    def matching(*args, **kwargs):
        seen.append(kwargs)
        return store

    monkeypatch.setattr(cli, "services", services)
    monkeypatch.setattr("spotify_local_migrator.workflow.match_job", matching)
    args = (
        ["match", "-p", capture.playlist.playlist_id]
        if command == "match"
        else ["migrate", "--job", str(store.directory), "--dry-run", "--no-review"]
    )
    result = CliRunner().invoke(
        cli.app, [*args, "--artist", "Corbin", "--expected-artist", "Shlohmo"]
    )
    assert result.exit_code == 0, result.output
    assert len(seen) == 1
    assert seen[0]["expected_artists"] == ["Corbin", "Shlohmo"]
    if command == "migrate":
        assert seen[0]["store"].directory == store.directory


def test_existing_job_can_update_tags_without_repeating_human_reviews(job, settings, monkeypatch):
    store, _, report, _ = job
    human = [decision.model_copy(deep=True) for decision in report.decisions[1:]]
    decision = report.decisions[0]
    decision.status = "unmatched"
    decision.candidate = None
    decision.review_completed = False
    store.save("matches.json", report)

    class Client:
        def current_user(self):
            return {"account_id": "current-account"}

    class Search:
        account_id = "current-account"
        requests = 0
        calls = []

        def __init__(self, *args, **kwargs):
            pass

        def for_track(self, prepared):
            self.calls.append(prepared.title)
            return [candidate_from_api(raw_track(A, prepared.title))], search_queries(prepared)

    monkeypatch.setattr("spotify_local_migrator.workflow.CatalogueSearch", Search)
    match_job(
        settings,
        Client(),
        Console(quiet=True),
        store=store,
        expected_artists=["Westside Gunn", "Corbin"],
    )
    saved = store.report()
    assert saved.expected_artists == ["Westside Gunn", "Corbin"]
    assert saved.expected_artist_source == "configured"
    assert saved.decisions[1:] == human
    assert Search.calls == ["Elizabeth"]
    assert saved.decisions[0].status == "auto"
