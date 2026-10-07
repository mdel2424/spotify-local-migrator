from rapidfuzz import fuzz

from ..models import LocalTrack
from .config import MatchingConfig
from .models import MatchDecision, PreparedTrack, SpotifyCandidate
from .normalize import extract_versions, normalize


def similarity(left: str | None, right: str | None) -> float:
    left, right = normalize(left), normalize(right)
    if not left or not right:
        return 0.0
    # token_set_ratio would score "Song" vs "Song Completely Different" as 100.
    return max(fuzz.ratio(left, right), fuzz.token_sort_ratio(left, right)) / 100


def duration_similarity(local_ms: int, candidate_ms: int) -> float:
    difference = abs(local_ms - candidate_ms) / 1000
    points = [(0, 1.0), (2, 1.0), (5, 0.95), (10, 0.80), (30, 0.35), (60, 0.05)]
    for (left, start), (right, end) in zip(points, points[1:], strict=False):
        if difference <= right:
            return start + (end - start) * (difference - left) / (right - left)
    return 0.0


def score_candidate(
    local: LocalTrack, prepared: PreparedTrack, candidate: SpotifyCandidate, config: MatchingConfig
) -> SpotifyCandidate:
    candidate = candidate.model_copy(deep=True)
    candidate_core, candidate_versions = extract_versions(candidate.title)
    title = similarity(prepared.core_title, candidate_core)
    artist_scores = [
        max((similarity(artist, other) for other in candidate.artists), default=0)
        for artist in prepared.artists
    ]
    artist = sum(artist_scores) / len(artist_scores) if artist_scores else 0
    values = {"title": title}
    if prepared.artists:
        values["artist"] = artist
    if local.duration_ms:
        values["duration"] = duration_similarity(local.duration_ms, candidate.duration_ms)
    if local.album and candidate.album:
        values["album"] = similarity(local.album, candidate.album)

    difference = set(prepared.qualifiers).symmetric_difference(candidate_versions)
    penalty = 0.0
    reasons = []
    if difference:
        penalty = (
            config.remaster_penalty
            if all(version.startswith("remaster") for version in difference)
            else config.version_penalty
        )
        reasons.append("Conflicting version qualifiers: " + ", ".join(sorted(difference)))
    if prepared.artist_source == "inferred":
        penalty += 0.02
    if not prepared.artists:
        reasons.append("No reliable local artist evidence.")
    if not local.duration_ms:
        reasons.append("No local duration evidence.")
    if prepared.filename_like:
        reasons.append("Filename-like title.")
    if candidate.is_playable is False:
        reasons.append("Candidate is unavailable to this account.")
    if local.duration_ms and abs(local.duration_ms - candidate.duration_ms) > 30000:
        reasons.append("Duration differs by more than 30 seconds.")

    weights = config.weights.model_dump()
    active_weight = sum(weights[key] for key in values)
    weighted_total = sum(value * weights[key] for key, value in values.items()) / active_weight
    total = max(0.0, min(1.0, weighted_total - penalty))
    before_caps = total
    if not prepared.artists or not local.duration_ms or prepared.filename_like:
        total = min(total, 0.89)
    if prepared.artists and min(artist_scores, default=0) < 0.60:
        total = min(total, 0.69)
        reasons.append("Artist evidence conflicts with the local track.")
    if candidate.is_playable is False:
        total = 0.0
    candidate.score = total
    candidate.score_breakdown = {
        **values,
        **{key + "_weight": weights[key] / active_weight for key in values},
        "evidence_weight": active_weight,
        "weighted_total": weighted_total,
        "confidence_adjustment": total - before_caps,
        "penalties": -penalty,
        "total": total,
        "version_conflict": float(bool(difference)),
        "artist_minimum": min(artist_scores, default=0),
    }
    candidate.reasons = reasons
    return candidate


def same_recording(left: SpotifyCandidate, right: SpotifyCandidate) -> bool:
    # Album variants can share an ISRC; unknown ISRCs remain ambiguous.
    return bool(
        left.isrc
        and left.isrc == right.isrc
        and extract_versions(left.title) == extract_versions(right.title)
        and sorted(normalize(artist) for artist in left.artists)
        == sorted(normalize(artist) for artist in right.artists)
        and left.raw.get("explicit") == right.raw.get("explicit")
        and abs(left.duration_ms - right.duration_ms) <= 2000
    )


def decide(
    local: LocalTrack,
    prepared: PreparedTrack,
    candidates: list[SpotifyCandidate],
    config: MatchingConfig,
    *,
    queries: list[str] | None = None,
) -> MatchDecision:
    ranked = sorted(
        [score_candidate(local, prepared, candidate, config) for candidate in candidates],
        key=lambda candidate: (-candidate.score, candidate.spotify_id),
    )
    decision = MatchDecision(
        local_track=local, prepared=prepared, candidates=ranked, searched_queries=queries or []
    )
    if not ranked or ranked[0].score < config.review_threshold:
        decision.reasons = ["No candidate reached the review threshold."]
        return decision
    best = ranked[0]
    runner_up = next((other for other in ranked[1:] if not same_recording(best, other)), None)
    margin = best.score - runner_up.score if runner_up else 1.0
    gates = {
        "Score below automatic threshold": best.score >= config.auto_threshold,
        "Another recording has a similar score": margin + 1e-9 >= config.minimum_margin,
        "Title evidence is insufficient": best.score_breakdown["title"]
        >= config.auto_title_minimum,
        "Artist evidence is insufficient": (
            best.score_breakdown["artist_minimum"] >= config.auto_artist_minimum
        ),
        "Duration is missing": bool(local.duration_ms),
        "Duration differs by more than 10 seconds": (
            bool(local.duration_ms) and abs(local.duration_ms - best.duration_ms) <= 10000
        ),
        "Version qualifiers differ": not best.score_breakdown["version_conflict"],
        "Title resembles a filename": not prepared.filename_like,
        "Playability has not been confirmed": best.is_playable is True,
    }
    decision.reasons = [reason for reason, passed in gates.items() if not passed]
    if not decision.reasons:
        decision.status = "auto"
        decision.candidate = best
    else:
        decision.needs_review = True
    return decision
