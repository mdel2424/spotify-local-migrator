from rapidfuzz import fuzz

from ..models import LocalTrack
from .config import MatchingConfig
from .models import MatchDecision, MatchReport, PreparedTrack, SpotifyCandidate
from .normalize import corroborate_title_artist, extract_versions, normalize


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
    prepared = corroborate_title_artist(prepared, candidate.artists)
    candidate_core, candidate_versions = extract_versions(candidate.title)
    title = similarity(prepared.core_title, candidate_core)

    def artist_similarity(artist):
        return max((similarity(artist, other) for other in candidate.artists), default=0)

    if prepared.artist_source in ("configured", "inferred"):
        # Playlist tags describe alternative artists, not a claim that every
        # listed artist appears on every song. Any main or featured credit can
        # supply that context; actual per-track features are still mandatory.
        artist_scores = (
            [max((artist_similarity(artist) for artist in prepared.primary_artists), default=0)]
            if prepared.primary_artists
            else []
        ) + [artist_similarity(artist) for artist in prepared.featured_artists]
    else:
        artist_scores = [artist_similarity(artist) for artist in prepared.artists]
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


def _same_track_metadata(left: SpotifyCandidate, right: SpotifyCandidate) -> bool:
    return bool(
        extract_versions(left.title) == extract_versions(right.title)
        and sorted(normalize(artist) for artist in left.artists)
        == sorted(normalize(artist) for artist in right.artists)
        and (
            not left.artist_ids
            or not right.artist_ids
            or sorted(left.artist_ids) == sorted(right.artist_ids)
        )
        and abs(left.duration_ms - right.duration_ms) <= 2000
    )


def same_recording(left: SpotifyCandidate, right: SpotifyCandidate) -> bool:
    # Album variants can share an ISRC; unknown ISRCs remain ambiguous.
    return bool(
        left.isrc
        and left.isrc == right.isrc
        and left.explicit == right.explicit
        and _same_track_metadata(left, right)
    )


def equivalent_versions(left: SpotifyCandidate, right: SpotifyCandidate) -> bool:
    # Clean/explicit counterparts can have separate ISRCs. Only a known change
    # in that flag with otherwise identical track evidence invokes this preference.
    return same_recording(left, right) or bool(
        left.explicit is not None
        and right.explicit is not None
        and left.explicit != right.explicit
        and _same_track_metadata(left, right)
    )


def group_candidates(candidates: list[SpotifyCandidate]) -> list[list[SpotifyCandidate]]:
    """Rank distinct matches, putting the preferred release first in each group."""
    order = {
        candidate.spotify_id: candidate.search_order
        if candidate.search_order is not None
        else index
        for index, candidate in enumerate(candidates)
    }
    groups: list[list[SpotifyCandidate]] = []
    for candidate in sorted(candidates, key=lambda item: order[item.spotify_id]):
        # Requiring agreement with every member prevents a clean counterpart
        # from bridging two explicit tracks with different/unknown recording IDs.
        group = next(
            (
                group
                for group in groups
                if all(equivalent_versions(candidate, other) for other in group)
            ),
            None,
        )
        if group is None:
            groups.append([candidate])
        else:
            group.append(candidate)
    for group in groups:
        group.sort(
            key=lambda item: (
                0 if item.is_playable is True else 1 if item.is_playable is None else 2,
                0 if item.explicit is True else 1,
                order[item.spotify_id],
            )
        )
    groups.sort(
        key=lambda group: (
            -max(item.score for item in group),
            min(order[item.spotify_id] for item in group),
        )
    )
    return groups


def review_evidence(
    local: LocalTrack, candidate: SpotifyCandidate, config: MatchingConfig
) -> str | None:
    """Require independent identity evidence, not just a weighted total."""
    if candidate.is_playable is False:
        return None
    title = candidate.score_breakdown.get("title", 0)
    artist = candidate.score_breakdown.get("artist_minimum", 0)
    if title >= 0.90 and artist >= 0.90:
        # SoundCloud uploads can include intros/outros or use another edit.
        # Duration and version differences need a human, not silent rejection.
        return "Strong title and artist agreement warrants review despite other differences."
    if title >= 0.94 and local.duration_ms:
        difference = abs(local.duration_ms - candidate.duration_ms)
        if difference <= 10000 and difference / local.duration_ms <= 0.05:
            # Stage names, aliases and uploader tags can disagree. This is
            # evidence for human review only; it never establishes artist identity.
            return "Strong title and close duration warrant review; confirm the artist."
    if candidate.score >= config.review_threshold and title >= 0.60 and artist >= 0.60:
        return "Title and artist evidence support review."
    if (
        candidate.score >= config.auto_threshold
        and title >= config.auto_title_minimum
        and artist >= config.auto_artist_minimum
    ):
        # Respect explicitly customized automatic evidence thresholds too.
        return "Candidate meets the configured automatic title and artist requirements."
    return None


def rank_candidate_groups(
    local: LocalTrack,
    prepared: PreparedTrack,
    candidates: list[SpotifyCandidate],
    config: MatchingConfig,
) -> list[list[SpotifyCandidate]]:
    scored = [score_candidate(local, prepared, candidate, config) for candidate in candidates]
    next_order = (
        max((item.search_order for item in scored if item.search_order is not None), default=-1) + 1
    )
    for candidate in scored:
        if candidate.search_order is None:
            # Older jobs have no search order; retain their saved display order.
            candidate.search_order = next_order
            next_order += 1
    groups = group_candidates(scored)
    # A plausible lower-scoring recording must not be hidden by a higher total
    # assembled from weak title/artist evidence. Preserve release preferences
    # and score order within each eligibility tier.
    groups.sort(key=lambda group: review_evidence(local, group[0], config) is None)
    return groups


def rank_candidates(
    local: LocalTrack,
    prepared: PreparedTrack,
    candidates: list[SpotifyCandidate],
    config: MatchingConfig,
) -> list[SpotifyCandidate]:
    return [
        candidate
        for group in rank_candidate_groups(local, prepared, candidates, config)
        for candidate in group
    ]


def decide(
    local: LocalTrack,
    prepared: PreparedTrack,
    candidates: list[SpotifyCandidate],
    config: MatchingConfig,
    *,
    queries: list[str] | None = None,
) -> MatchDecision:
    groups = rank_candidate_groups(local, prepared, candidates, config)
    ranked = [candidate for group in groups for candidate in group]
    decision = MatchDecision(
        local_track=local, prepared=prepared, candidates=ranked, searched_queries=queries or []
    )
    if ranked:
        decision.prepared = corroborate_title_artist(prepared, ranked[0].artists)
        prepared = decision.prepared
    if not ranked:
        decision.reasons = ["No candidates found."]
        return decision
    best = ranked[0]
    runner_up_score = max((other.score for group in groups[1:] for other in group), default=None)
    margin = best.score - runner_up_score if runner_up_score is not None else 1.0
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
    elif evidence := review_evidence(local, best, config):
        decision.needs_review = True
        if best.score < config.review_threshold:
            decision.reasons.append(evidence)
    else:
        decision.reasons = ["No candidate has sufficient supporting evidence for review."]
    return decision


def refresh_unreviewed_choices(report: MatchReport) -> bool:
    """Refresh stored evidence offline without changing completed human decisions."""
    config = MatchingConfig.model_validate(report.matching_config)
    changed = False
    for index, decision in enumerate(report.decisions):
        if decision.review_completed or decision.status in ("approved", "rejected"):
            continue
        updated = decide(
            decision.local_track,
            decision.prepared,
            decision.candidates,
            config,
            queries=decision.searched_queries,
        )
        if decision.status != "auto" and updated.status == "auto":
            # Review may have only older or manually collected search results.
            # Reopening that queue must not silently accept a previously
            # unselected track; matching can establish a fresh automatic choice.
            updated.status = "unmatched"
            updated.candidate = None
            updated.needs_review = True
            updated.reasons = ["Stored candidate now meets automatic criteria; confirm the match."]
        if updated != decision:
            report.decisions[index] = updated
            changed = True
    return changed
