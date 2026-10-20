"""Application orchestration; matching never mutates Spotify."""

from pathlib import Path

from rich.console import Console
from rich.text import Text

from .config import Settings
from .errors import StateError
from .matching.cache import SearchCache
from .matching.config import MatchingConfig, load_matching_config
from .matching.engine import new_report, run_matching
from .matching.normalize import prepare_track, unique_names
from .matching.scoring import decide
from .matching.search import CatalogueSearch, search_queries
from .migration.jobs import JobStore
from .migration.scanner import PlaylistScanner
from .migration.state import CaptureStore
from .spotify.client import SpotifyClient
from .ui.review import show_results


def account_id(client: SpotifyClient) -> str:
    profile = client.current_user()
    value = profile.get("account_id") or profile.get("id")
    if not isinstance(value, str) or not value:
        raise StateError("Spotify did not return the current account identifier.")
    return value


def match_job(
    settings: Settings,
    client: SpotifyClient,
    console: Console,
    *,
    store: JobStore | None = None,
    playlist_id: str | None = None,
    from_scan: Path | None = None,
    config_path: Path = Path("config.yaml"),
    expected_artists: list[str] | None = None,
    no_cache: bool = False,
) -> JobStore:
    account = account_id(client)
    if store is None:
        if from_scan:
            capture = CaptureStore.load(from_scan)
        elif playlist_id:
            capture = PlaylistScanner(client, attempts=settings.scan_attempts).scan(playlist_id)
            CaptureStore(settings.data_dir).save(capture)
        else:
            raise StateError("Select a playlist or pass --from-scan.")
        store = JobStore.create(settings.data_dir, capture)
        report = new_report(capture, load_matching_config(config_path), account, expected_artists)
        store.save("matches.json", report)
    else:
        report = store.report()
        if report.operation == "cleanup":
            raise StateError("This is a cleanup job. Use cleanup --job, review or resume.")
        if report.account_id != account:
            raise StateError("This matching job belongs to another Spotify account.")
        if (store.directory / "migration.json").exists():
            raise StateError("Apply already started. Use resume for this job.")
        if expected_artists:
            # Explicit new tags can refine a saved matching job. The retained
            # decision loop below preserves completed human approvals/rejections.
            report.expected_artists = unique_names(expected_artists)
            report.expected_artist_source = "configured"
    console.print(Text(f"Job: {store.directory}"))
    config = MatchingConfig.model_validate(report.matching_config)
    with (
        store.lock(),
        SearchCache(
            settings.data_dir / ".cache" / "search.sqlite3", ttl_days=config.cache_ttl_days
        ) as cache,
    ):
        search = CatalogueSearch(client, config, account, cache=cache, no_cache=no_cache)
        # Recompute unattended choices from stored candidates when resuming after
        # a scoring update. Explicit human decisions are retained.
        retained = []
        for decision in report.decisions:
            if decision.review_completed:
                retained.append(decision)
                continue
            prepared = prepare_track(
                decision.local_track,
                report.expected_artists,
                report.producer_names,
                expected_source=report.expected_artist_source,
                uploader_names=config.uploader_names,
            )
            if search_queries(prepared) != decision.searched_queries:
                # Updated cleanup requires all query variants to be searched
                # again before the automatic ambiguity gate can be trusted.
                continue
            retained.append(
                decide(
                    decision.local_track,
                    prepared,
                    decision.candidates,
                    config,
                    queries=decision.searched_queries,
                )
            )
        report.decisions = retained
        if len(retained) != len(store.capture().local_tracks):
            report.matching_complete = False

        def progress(decision, done, total):
            best = decision.candidates[0] if decision.candidates else None
            label = (
                f"{', '.join(best.artists)} - {best.title} ({best.score:.0%})"
                if best
                else "No results"
            )
            console.print(
                Text(
                    f"[{done}/{total}] {decision.prepared.title} -> {label} "
                    f"[{'review' if decision.needs_review else decision.status}]"
                )
            )

        report = run_matching(store, report, search, progress, live_api=True)
    show_results(console, report)
    return store


def cleanup_job(
    settings: Settings,
    client: SpotifyClient,
    console: Console,
    *,
    store: JobStore | None = None,
    playlist_id: str | None = None,
    from_scan: Path | None = None,
    config_path: Path = Path("config.yaml"),
    expected_artists: list[str] | None = None,
) -> JobStore:
    from .matching.cleanup import cleanup_report, playlist_candidates, refresh_cleanup_choices
    from .matching.scoring import review_evidence
    from .matching.search import candidate_from_api

    account = account_id(client)
    if store is None:
        capture = (
            CaptureStore.load(from_scan)
            if from_scan
            else PlaylistScanner(client, attempts=settings.scan_attempts).scan(playlist_id)
        )
        store = JobStore.create(settings.data_dir, capture)
        report = cleanup_report(
            capture, load_matching_config(config_path), account, expected_artists
        )
    else:
        report = store.report()
        if report.operation != "cleanup" or report.account_id != account:
            raise StateError("Select a cleanup job belonging to this Spotify account.")
        capture = store.capture()
        if expected_artists:
            report.expected_artists = unique_names(expected_artists)
            report.expected_artist_source = "configured"
            enriched = {
                candidate.uri: candidate
                for decision in report.decisions
                for candidate in decision.candidates
            }
            pool = [enriched.get(item.uri, item) for item in playlist_candidates(capture)]
            for decision in report.decisions:
                if not decision.review_completed and decision.status not in (
                    "approved",
                    "rejected",
                ):
                    decision.candidates = pool
            refresh_cleanup_choices(report, capture, initial=True)
    console.print(Text(f"Cleanup job: {store.directory}"))
    with store.lock():
        if (store.directory / "migration.json").exists():
            raise StateError("Cleanup already started. Use resume for this job.")
        store.save("matches.json", report)
        config = MatchingConfig.model_validate(report.matching_config)
        unchecked = {
            candidate.uri: candidate
            for decision in report.decisions
            for candidate in decision.candidates
            if candidate.is_playable is None
            and review_evidence(decision.local_track, candidate, config)
        }
        for uri, candidate in unchecked.items():
            checked = candidate_from_api(client.track(candidate.spotify_id))
            if checked is None or checked.uri != uri:
                checked = candidate.model_copy(update={"is_playable": False})
            checked.search_order = candidate.search_order
            for decision in report.decisions:
                decision.candidates = [
                    checked if item.uri == uri else item for item in decision.candidates
                ]
            refresh_cleanup_choices(report, capture, initial=True)
            store.save("matches.json", report)
        report.real_api_validation = True
        refresh_cleanup_choices(report, capture, initial=True)
        store.save("matches.json", report)
    show_results(console, report)
    return store
