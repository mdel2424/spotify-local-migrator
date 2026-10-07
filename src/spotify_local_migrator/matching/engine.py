from collections.abc import Callable
from datetime import UTC, datetime

from ..errors import StateError
from ..migration.jobs import JobStore, content_hash
from ..models import PlaylistCapture
from .config import MatchingConfig
from .models import MatchDecision, MatchReport
from .normalize import infer_artists, infer_producers, prepare_track
from .scoring import decide
from .search import CatalogueSearch


def new_report(
    capture: PlaylistCapture,
    config: MatchingConfig,
    account_id: str,
    expected_override: list[str] | None = None,
) -> MatchReport:
    hint = config.playlists.get(capture.playlist.playlist_id) or config.playlists.get(
        capture.playlist.name
    )
    expected = expected_override or (hint.expected_artists if hint else [])
    source = "configured" if expected else "inferred"
    if not expected:
        expected = infer_artists(capture.local_tracks)
    if not expected:
        source = "missing"
    producers = infer_producers(capture.local_tracks, config.producer_names)
    return MatchReport(
        playlist_id=capture.playlist.playlist_id,
        playlist_name=capture.playlist.name,
        snapshot_id=capture.snapshot_id,
        capture_hash=content_hash(capture),
        account_id=account_id,
        created_at=datetime.now(UTC),
        expected_artists=expected,
        expected_artist_source=source,
        producer_names=producers,
        matching_config=config.model_dump(mode="json"),
    )


def run_matching(
    store: JobStore,
    report: MatchReport,
    search: CatalogueSearch,
    progress: Callable[[MatchDecision, int, int], None] | None = None,
    *,
    live_api: bool = False,
) -> MatchReport:
    capture = store.capture()
    if report.capture_hash != content_hash(capture) or report.account_id != search.account_id:
        raise StateError("Matching job does not belong to this capture/account.")
    config = MatchingConfig.model_validate(report.matching_config)
    completed = {decision.local_track.playlist_position for decision in report.decisions}
    total = len(capture.local_tracks)
    store.save("matches.json", report)
    for track in capture.local_tracks:
        if track.playlist_position in completed:
            continue
        prepared = prepare_track(
            track,
            report.expected_artists,
            report.producer_names,
            expected_source=report.expected_artist_source,
            uploader_names=config.uploader_names,
        )
        candidates, queries = search.for_track(prepared)
        decision = decide(track, prepared, candidates, config, queries=queries)
        report.decisions.append(decision)
        report.decisions.sort(key=lambda choice: choice.local_track.playlist_position)
        report.real_api_validation = report.real_api_validation or live_api
        report.validation_summary = {
            "completed_tracks": len(report.decisions),
            "candidate_count": sum(len(choice.candidates) for choice in report.decisions),
            "requests_this_session": search.requests,
        }
        store.save("matches.json", report)
        if progress:
            progress(decision, len(report.decisions), total)
    report.matching_complete = True
    store.save("matches.json", report)
    return report
