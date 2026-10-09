"""Review recall without turning similar runtimes into automatic matches."""

import pytest
from test_matching import candidate, local
from test_migration import A, C
from test_migration import job as job
from typer.testing import CliRunner

from spotify_local_migrator import cli
from spotify_local_migrator.matching.config import MatchingConfig
from spotify_local_migrator.matching.normalize import prepare_track
from spotify_local_migrator.matching.scoring import decide, refresh_unreviewed_choices


def match(track, *results):
    return decide(track, prepare_track(track, ["Corbin"], []), list(results), MatchingConfig())


def test_real_destrooy_is_reviewed_despite_artist_conflict_and_69_percent_cap():
    track = local("destrooy", ["corbin"], 268000)
    track.album = "p22"
    result = candidate("Destrooy", ["Spooky Black"], 264071)
    result.album = "Destrooy"
    decision = match(track, result)
    assert decision.candidates[0].score == pytest.approx(0.69)
    assert decision.candidates[0].score_breakdown["title"] == 1
    assert decision.needs_review and decision.candidate is None
    assert decision.status == "unmatched"
    assert "Artist evidence is insufficient" in decision.reasons
    assert any("confirm the artist" in reason for reason in decision.reasons)


@pytest.mark.parametrize(
    "title,duration,spotify_title,artists,spotify_duration",
    [
        ("kill all whites", 217000, "Clown On Stage", ["Corbin"], 207114),
        ("nowhere", 169000, "Nowhere", ["Charlotte Cornfield"], 194005),
        (
            "early/quiet (Remaster)",
            360000,
            "Early Quiet",
            ["Lofi Sleep", "Lofi Hip-Hop Beats", "lofi.cat"],
            150600,
        ),
        ("withdrawals voicemail ft. bobby raps", 164000, "voicemail", ["ded reception"], 146995),
        (
            "Ecco2k X Corbin (Happily Ever After X Dragon Chaser) [DJHAYLZ Mix]",
            159000,
            "Jax & Pomni Song | Trying To Forget You (The Amazing Digital Circus)",
            ["Earendil"],
            160000,
        ),
        ("wasting love", 171000, "wasting your love", ["Emile Mosseri"], 163611),
    ],
)
def test_real_weak_candidates_stay_unmatched(
    title, duration, spotify_title, artists, spotify_duration
):
    track = local(title, ["corbin"], duration)
    track.album = "p22"
    result = candidate(spotify_title, artists, spotify_duration)
    decision = match(track, result)
    assert decision.status == "unmatched"
    assert not decision.needs_review and decision.candidate is None


def test_partial_title_and_confirmed_artist_still_reach_ordinary_review():
    decision = match(
        local("calmdown", ["Corbin"], 210000), candidate("Come Down", ["Corbin"], 229321)
    )
    assert decision.needs_review and decision.candidate is None
    assert "Title evidence is insufficient" in decision.reasons


@pytest.mark.parametrize("extra_time", [30000, 60000, 180000])
@pytest.mark.parametrize("version", ["", " (Remix)"])
def test_strong_title_artist_survive_runtime_and_version_differences(extra_time, version):
    decision = match(
        local("Song" + version, ["Corbin"], 180000),
        candidate("Song", ["Corbin"], 180000 + extra_time),
    )
    assert decision.needs_review and decision.candidate is None
    assert decision.status != "auto"
    assert "Duration differs by more than 10 seconds" in decision.reasons
    if version:
        assert "Version qualifiers differ" in decision.reasons
        assert decision.candidates[0].score < 0.7


@pytest.mark.parametrize("difference,review", [(0, True), (10000, True), (10001, False)])
def test_artist_uncertainty_needs_close_absolute_runtime(difference, review):
    decision = match(
        local("Destrooy", ["Corbin"], 200000),
        candidate("Destrooy", ["Spooky Black"], 200000 + difference),
    )
    assert decision.needs_review is review
    assert decision.candidate is None


@pytest.mark.parametrize("difference,review", [(1000, True), (1001, False), (8000, False)])
def test_artist_uncertainty_needs_close_relative_runtime_for_short_clips(difference, review):
    decision = match(
        local("Destrooy", ["Corbin"], 20000),
        candidate("Destrooy", ["Spooky Black"], 20000 + difference),
    )
    assert decision.needs_review is review
    assert decision.candidate is None


@pytest.mark.parametrize("artists,review", [(["Corbin"], True), (["Spooky Black"], False)])
def test_missing_duration_needs_artist_agreement_for_review(artists, review):
    decision = match(local("Destrooy", ["Corbin"], None), candidate("Destrooy", artists))
    assert decision.needs_review is review
    assert decision.candidate is None


def test_unavailable_exact_title_and_duration_is_not_reviewed():
    result = candidate("Destrooy", ["Spooky Black"], 268000)
    result.is_playable = False
    decision = match(local("Destrooy", ["Corbin"], 268000), result)
    assert not decision.needs_review and decision.candidate is None


def test_missing_named_feature_can_be_reviewed_but_never_accepted_automatically():
    decision = match(
        local("Song (feat. Guest)", ["Corbin"], 180000), candidate("Song", ["Corbin"], 180000)
    )
    assert decision.needs_review and decision.candidate is None
    assert "Artist evidence is insufficient" in decision.reasons


def test_plausible_lower_total_is_not_hidden_by_weak_identity_evidence():
    track = local("wasting love (Remix)", ["Corbin"], 171000)
    wrong = candidate("wasting your love (Remix)", ["Emile Mosseri"], 163611, track_id=A)
    plausible = candidate("Wasting Love", ["Corbin"], 350000, track_id=C)
    decision = match(track, wrong, plausible)
    assert decision.candidates[0].spotify_id == C
    assert decision.candidates[0].score < decision.candidates[1].score
    assert decision.needs_review and decision.candidate is None


def test_ambiguity_gate_includes_recordings_ranked_after_the_reviewable_tier():
    track = local()
    config = MatchingConfig(
        weights={"title": 0.05, "artist": 0.30, "duration": 0.60, "album": 0.05}
    )
    decision = decide(
        track,
        prepare_track(track, ["Artist"], []),
        [
            candidate(track_id="A" * 22),
            candidate("Song Two", duration=186000, track_id="B" * 22),
            candidate("Unrelated", track_id="C" * 22),
        ],
        config,
    )
    assert [item.spotify_id for item in decision.candidates] == ["A" * 22, "B" * 22, "C" * 22]
    assert decision.candidates[2].score > decision.candidates[1].score
    assert decision.needs_review and decision.candidate is None
    assert "Another recording has a similar score" in decision.reasons


def test_refresh_reopens_saved_unmatched_without_changing_human_decisions(job):
    _, _, report, _ = job
    decision = report.decisions[0]
    decision.local_track = local("destrooy", ["corbin"], 268000)
    decision.prepared = prepare_track(decision.local_track, ["Corbin"], [])
    decision.candidates = [candidate("Destrooy", ["Spooky Black"], 264071)]
    decision.status = "unmatched"
    decision.candidate = None
    decision.needs_review = False
    decision.review_completed = False
    human_approved = report.decisions[1].model_copy(deep=True)
    report.decisions[2].status = "rejected"
    report.decisions[2].candidate = None
    human_rejected = report.decisions[2].model_copy(deep=True)
    assert refresh_unreviewed_choices(report)
    assert report.decisions[0].needs_review
    assert report.decisions[1] == human_approved
    assert report.decisions[2] == human_rejected
    assert not refresh_unreviewed_choices(report)


def test_refresh_does_not_silently_promote_old_unmatched_to_automatic(job):
    _, _, report, _ = job
    decision = report.decisions[0]
    decision.status = "unmatched"
    decision.candidate = None
    decision.needs_review = False
    decision.review_completed = False
    assert refresh_unreviewed_choices(report)
    decision = report.decisions[0]
    assert decision.status == "unmatched"
    assert decision.needs_review and decision.candidate is None
    assert not refresh_unreviewed_choices(report)


def test_offline_review_refreshes_new_queue_and_enter_selects_plausible_top(
    job, settings, monkeypatch
):
    store, _, report, _ = job
    decision = report.decisions[0]
    decision.local_track = local("wasting love (Remix)", ["Corbin"], 171000)
    decision.prepared = prepare_track(decision.local_track, ["Corbin"], [])
    decision.candidates = [
        candidate("wasting your love (Remix)", ["Emile Mosseri"], 163611, track_id=A),
        candidate("Wasting Love", ["Corbin"], 350000, track_id=C),
    ]
    decision.candidate = None
    decision.status = "unmatched"
    decision.needs_review = False
    decision.review_completed = False
    report.matching_complete = False
    store.save("matches.json", report)
    monkeypatch.setattr(cli, "load_settings", lambda env_file: settings)
    monkeypatch.setattr(
        cli, "services", lambda *args: pytest.fail("Offline review opened services")
    )
    result = CliRunner().invoke(
        cli.app, ["review", "--offline", "--job", str(store.directory)], input="\n"
    )
    assert result.exit_code == 0, result.output
    saved = store.report()
    assert not saved.matching_complete
    assert saved.decisions[0].status == "approved"
    assert saved.decisions[0].candidate.spotify_id == C
