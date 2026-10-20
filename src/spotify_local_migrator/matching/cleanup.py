"""Match local copies only to catalogue occurrences already in a playlist."""

from collections import Counter

from ..errors import StateError
from ..models import PlaylistCapture
from .config import MatchingConfig
from .engine import new_report
from .models import MatchReport, SpotifyCandidate
from .normalize import normalize, prepare_track
from .scoring import decide, review_evidence
from .search import candidate_from_api


def playlist_candidates(capture: PlaylistCapture) -> list[SpotifyCandidate]:
    candidates = {}
    for entry in capture.entries:
        if entry.is_local or not isinstance(entry.raw, dict):
            continue
        raw = entry.raw.get("item") if "item" in entry.raw else entry.raw.get("track")
        candidate = candidate_from_api(raw)
        if candidate and candidate.uri == entry.uri and candidate.uri not in candidates:
            candidate.search_order = entry.playlist_position
            candidates[candidate.uri] = candidate
    return list(candidates.values())


def inventory(capture: PlaylistCapture) -> Counter:
    valid = {candidate.uri for candidate in playlist_candidates(capture)}
    return Counter(
        entry.uri for entry in capture.entries if not entry.is_local and entry.uri in valid
    )


def remaining_candidates(
    report: MatchReport, capture: PlaylistCapture, position: int
) -> list[SpotifyCandidate]:
    remaining = inventory(capture)
    for decision in report.decisions:
        if (
            decision.local_track.playlist_position != position
            and decision.status in ("auto", "approved")
            and decision.candidate
        ):
            remaining[decision.candidate.uri] -= 1
    # Retain availability checked during job preparation, without more API calls.
    enriched = {
        candidate.uri: candidate
        for decision in report.decisions
        for candidate in decision.candidates
    }
    return [
        enriched.get(candidate.uri, candidate)
        for candidate in playlist_candidates(capture)
        if remaining[candidate.uri] > 0
    ]


def refresh_cleanup_choices(
    report: MatchReport, capture: PlaylistCapture, *, initial: bool = False
) -> bool:
    if report.operation != "cleanup":
        raise StateError("This is not a duplicate cleanup job.")
    config = MatchingConfig.model_validate(report.matching_config)
    remaining = inventory(capture)
    for decision in report.decisions:
        if decision.candidate and (decision.review_completed or decision.status == "approved"):
            remaining[decision.candidate.uri] -= 1
            if remaining[decision.candidate.uri] < 0:
                raise StateError(
                    "Reviewed cleanup choices exceed the existing Spotify occurrences."
                )
    changed = False
    # Completed human choices take priority, then existing automatic choices.
    order = sorted(
        range(len(report.decisions)),
        key=lambda i: (
            report.decisions[i].status != "auto",
            report.decisions[i].local_track.playlist_position,
        ),
    )
    for index in order:
        decision = report.decisions[index]
        if decision.review_completed or decision.status in ("approved", "rejected"):
            continue
        prepared = prepare_track(
            decision.local_track,
            report.expected_artists,
            report.producer_names,
            expected_source=report.expected_artist_source,
            uploader_names=config.uploader_names,
        )
        updated = decide(
            decision.local_track,
            prepared,
            [candidate for candidate in decision.candidates if remaining[candidate.uri] > 0],
            config,
            queries=decision.searched_queries,
        )
        if not initial and decision.status != "auto" and updated.status == "auto":
            updated.status = "unmatched"
            updated.candidate = None
            updated.needs_review = True
            updated.reasons = [
                "Existing Spotify counterpart now meets automatic criteria; confirm it."
            ]
        if updated.candidate:
            remaining[updated.candidate.uri] -= 1
        # Keep reviewable matches and every potential automatic ambiguity rival.
        # Manual lookup still has access to the whole saved playlist catalogue.
        blocked = [candidate for candidate in decision.candidates if remaining[candidate.uri] <= 0]
        updated.candidates = [
            candidate
            for rank, candidate in enumerate(updated.candidates)
            if rank < 3
            or review_evidence(decision.local_track, candidate, config)
            or candidate.score >= config.auto_threshold - config.minimum_margin
        ]
        known = {candidate.uri for candidate in updated.candidates}
        updated.candidates.extend(candidate for candidate in blocked if candidate.uri not in known)
        if updated != decision:
            report.decisions[index] = updated
            changed = True
    return changed


def cleanup_report(
    capture: PlaylistCapture,
    config: MatchingConfig,
    account: str,
    expected_artists: list[str] | None = None,
) -> MatchReport:
    report = new_report(capture, config, account, expected_artists)
    report.operation = "cleanup"
    candidates = playlist_candidates(capture)
    for track in capture.local_tracks:
        prepared = prepare_track(
            track,
            report.expected_artists,
            report.producer_names,
            expected_source=report.expected_artist_source,
            uploader_names=config.uploader_names,
        )
        decision = decide(track, prepared, candidates, config)
        decision.candidates = [
            candidate
            for rank, candidate in enumerate(decision.candidates)
            if rank < 3
            or review_evidence(track, candidate, config)
            or candidate.score >= config.auto_threshold - config.minimum_margin
        ]
        report.decisions.append(decision)
    refresh_cleanup_choices(report, capture, initial=True)
    report.matching_complete = True
    report.validation_summary = {"existing_spotify_tracks": sum(inventory(capture).values())}
    return report


class PlaylistSearch:
    """Manual review lookup stays inside the captured playlist and occurrence budget."""

    scope = "playlist"

    def __init__(self, report: MatchReport, capture: PlaylistCapture):
        self.report = report
        self.capture = capture
        self.position = -1

    def query(self, query: str) -> list[SpotifyCandidate]:
        words = normalize(query).split()
        if not words:
            return []
        return [
            candidate
            for candidate in remaining_candidates(self.report, self.capture, self.position)
            if all(
                word
                in normalize(" ".join([candidate.title, *candidate.artists, candidate.album or ""]))
                for word in words
            )
        ]
