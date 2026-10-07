import httpx
import pytest

from spotify_local_migrator.matching.cache import SearchCache
from spotify_local_migrator.matching.config import MatchingConfig
from spotify_local_migrator.matching.models import SpotifyCandidate
from spotify_local_migrator.matching.normalize import (
    extract_versions,
    infer_artists,
    normalize,
    prepare_track,
)
from spotify_local_migrator.matching.scoring import decide, duration_similarity, score_candidate
from spotify_local_migrator.matching.search import CatalogueSearch, search_queries
from spotify_local_migrator.models import LocalTrack


def local(title="Song", artists=None, duration=180000):
    return LocalTrack(
        playlist_position=0,
        uri="spotify:local:Artist::Song:180",
        title=title,
        artists=["Artist"] if artists is None else artists,
        duration_ms=duration,
    )


def candidate(title="Song", artists=None, duration=180000, track_id="B" * 22, isrc=None):
    return SpotifyCandidate(
        spotify_id=track_id,
        uri="spotify:track:" + track_id,
        title=title,
        artists=["Artist"] if artists is None else artists,
        album="Album",
        duration_ms=duration,
        is_playable=True,
        isrc=isrc,
    )


@pytest.mark.parametrize(
    "raw, cleaned",
    [
        ("2sdxrt3all - oh (prod. whyceg) [djslimebxll exclusive]", "oh"),
        (
            "~ Slump Audios Radio ~ - 2sdxrt3all - crazy people (whyceg) "
            "[slump audios exclusive + dj banned + dj gren8de]",
            "crazy people",
        ),
        ("2Sdxrt3all - Retirement", "Retirement"),
        ("4for4 (Prod. Whyceg, Goxan, Souljaspirits)", "4for4"),
        ("How To Feel (Prod. Whyceg) *Music Vid In Desc*", "How To Feel"),
        ("Tunnel Vision (Prod. Whyceg) *Vid in Disc*", "Tunnel Vision"),
        ("2SDXRT3ALL - RARE PROD WHYCEG + AYELAVISH", "RARE"),
        ("2sdxrt3all - PAPARAZZI whyceg", "PAPARAZZI"),
        ("2sdxrt3all start (resonance)", "start"),
    ],
)
def test_real_scan_title_formats(raw, cleaned):
    track = local(raw, ["~ Slump Audios Radio ~"])
    prepared = prepare_track(
        track, ["2sdxrt3all"], ["whyceg", "goxan", "souljaspirits", "resonance"]
    )
    assert prepared.title == cleaned
    assert "2sdxrt3all" in [name.lower() for name in prepared.artists]
    assert "~ Slump Audios Radio ~" in prepared.ignored_artist_tags


def test_collaborator_prefix_and_unicode_artist_separator():
    prepared = prepare_track(
        local("3hard x 2sdxrt3all - Trap Heisman", ["2sdxrt3all， Whyceg"]),
        ["2sdxrt3all"],
        ["whyceg"],
    )
    assert prepared.title == "Trap Heisman"
    assert prepared.artists == ["3hard", "2sdxrt3all"]


def test_features_are_kept_as_artist_evidence():
    prepared = prepare_track(
        local("Lyfe of The Party (feat. Nino Paid)", ["2Sdxrt3all & Nino Paid"]),
        ["2sdxrt3all"],
        [],
    )
    assert prepared.title == "Lyfe of The Party"
    assert prepared.featured_artists == ["Nino Paid"]
    assert prepared.artists == ["2Sdxrt3all", "Nino Paid"]


@pytest.mark.parametrize(
    "left, right",
    [
        ("Devil In A New Dress", "Devil in a New Dress"),
        ("Mr T", "Mr. T"),
        ("Can't", "Can’t"),
        ("Hall & Nash", "Hall and Nash"),
    ],
)
def test_normalization(left, right):
    assert normalize(left) == normalize(right)


@pytest.mark.parametrize("title", ["One Life To Live", "Long Live Twxn", "Live and Learn"])
def test_live_word_in_actual_title_is_not_a_qualifier(title):
    core, qualifiers = extract_versions(title)
    assert core == normalize(title)
    assert qualifiers == []


@pytest.mark.parametrize(
    "title, qualifier",
    [
        ("Song (Remastered 2018)", "remaster:2018"),
        ("Song - Live", "live"),
        ("Song (Remix)", "remix"),
        ("Song (Instrumental)", "instrumental"),
        ("Song (Sped Up)", "sped up"),
        ("Song (Slowed)", "slowed"),
        ("Song (Acoustic)", "acoustic"),
        ("Song (Radio Edit)", "radio edit"),
    ],
)
def test_qualifiers_preserved_and_conflicts_never_automatic(title, qualifier):
    track = local()
    prepared = prepare_track(track, ["Artist"], [])
    possible = candidate(title)
    assert extract_versions(title)[1] == [qualifier]
    decision = decide(track, prepared, [possible], MatchingConfig())
    assert decision.status != "auto"
    assert decision.candidates[0].score_breakdown["version_conflict"] == 1
    assert decision.candidates[0].score < 1


def test_local_remix_does_not_match_original_automatically():
    track = local("Song (Remix)")
    prepared = prepare_track(track, ["Artist"], [])
    assert prepared.qualifiers == ["remix"]
    assert decide(track, prepared, [candidate()], MatchingConfig()).status != "auto"


@pytest.mark.parametrize(
    "seconds, score", [(0, 1), (2, 1), (5, 0.95), (10, 0.8), (30, 0.35), (60, 0.05)]
)
def test_duration_tolerance(seconds, score):
    assert duration_similarity(180000, 180000 + seconds * 1000) == pytest.approx(score)


def test_exact_match_is_automatic_and_weights_are_transparent():
    track = local()
    prepared = prepare_track(track, ["Artist"], [])
    decision = decide(track, prepared, [candidate()], MatchingConfig())
    assert decision.status == "auto"
    assert decision.candidate.score == 1
    breakdown = decision.candidate.score_breakdown
    assert breakdown["title_weight"] == pytest.approx(0.5 / 0.95)
    assert breakdown["evidence_weight"] == 0.95


def test_near_tied_different_recordings_require_review():
    track = local()
    prepared = prepare_track(track, ["Artist"], [])
    decision = decide(
        track,
        prepared,
        [candidate(), candidate(track_id="C" * 22, duration=181000)],
        MatchingConfig(),
    )
    assert decision.needs_review
    assert decision.candidate is None
    assert "Another recording has a similar score" in decision.reasons


def test_same_isrc_album_variants_are_one_recording_for_margin():
    track = local()
    prepared = prepare_track(track, ["Artist"], [])
    decision = decide(
        track,
        prepared,
        [candidate(isrc="SAME"), candidate(track_id="C" * 22, isrc="SAME")],
        MatchingConfig(),
    )
    assert decision.status == "auto"


@pytest.mark.parametrize(
    "track",
    [
        local(duration=None),
        local(artists=[]),
        local("04_track.mp3"),
    ],
)
def test_insufficient_metadata_cannot_be_automatic(track):
    prepared = prepare_track(track, [] if not track.artists else ["Artist"], [])
    decision = decide(track, prepared, [candidate(title=prepared.title)], MatchingConfig())
    assert decision.status != "auto"


def test_expected_artist_need_not_be_first_candidate_artist():
    track = local()
    prepared = prepare_track(track, ["Artist"], [])
    assert (
        decide(track, prepared, [candidate(artists=["Guest", "Artist"])], MatchingConfig()).status
        == "auto"
    )


def test_missing_known_feature_requires_review():
    track = local("Song (feat. Guest)")
    prepared = prepare_track(track, ["Artist"], [])
    assert decide(track, prepared, [candidate()], MatchingConfig()).status != "auto"


def test_unplayable_candidate_is_unmatched():
    track = local()
    prepared = prepare_track(track, ["Artist"], [])
    possible = candidate()
    possible.is_playable = False
    assert decide(track, prepared, [possible], MatchingConfig()).status == "unmatched"


def test_filename_cleanup_does_not_damage_song_names_starting_with_digits():
    prepared = prepare_track(local("01_Mr._T.mp3"), ["Artist"], [])
    assert prepared.title == "Mr. T"
    assert not prepare_track(local("4for4"), ["Artist"], []).filename_like


def test_product_is_not_a_production_credit():
    assert prepare_track(local("Product of My Environment"), ["Artist"], []).title == (
        "Product of My Environment"
    )


def test_dominant_artist_is_inferred_from_repeated_combined_tags():
    tracks = [local(artists=["2sdxrt3all， Whyceg"]) for _ in range(8)]
    tracks += [local(artists=["Uploader"]) for _ in range(2)]
    assert infer_artists(tracks)[0].lower() == "2sdxrt3all"


def test_query_variants_and_cache_avoid_repeat_calls(make_api, tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/v1/search"
        assert request.url.params["limit"] == "10"
        return httpx.Response(200, json={"tracks": {"items": [], "next": None}})

    api, _, _ = make_api(handler)
    track = local()
    prepared = prepare_track(track, ["Artist"], [])
    assert search_queries(prepared) == [
        'track:"song" artist:"artist"',
        "song artist",
        'track:"song"',
    ]
    with SearchCache(tmp_path / "cache.sqlite3") as cache:
        search = CatalogueSearch(api, MatchingConfig(), "account", cache=cache)
        search.for_track(prepared)
        search.for_track(prepared)
        assert len(requests) == 3
        assert cache.hits == 3
        search.no_cache = True
        search.for_track(prepared)
        assert len(requests) == 6
        other = CatalogueSearch(api, MatchingConfig(), "other-account", cache=cache)
        other.for_track(prepared)
        assert len(requests) == 9


def test_feature_annotation_in_catalogue_title_does_not_reduce_title_score():
    track = local("Song (feat. Guest)", ["Artist"])
    prepared = prepare_track(track, ["Artist"], [])
    possible = candidate("Song (feat. Guest)", ["Artist", "Guest"])
    assert (
        score_candidate(track, prepared, possible, MatchingConfig()).score_breakdown["title"] == 1
    )
