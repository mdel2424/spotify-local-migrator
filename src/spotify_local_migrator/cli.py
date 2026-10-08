import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.prompt import IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from .config import Settings, load_settings
from .errors import MigratorError, SpotifyAPIError
from .migration.scanner import PlaylistScanner, playlist_summary
from .migration.state import CaptureStore, validate_playlist_id
from .spotify.auth import SpotifyAuth, TokenStore, login_with_callback
from .spotify.client import SpotifyClient
from .ui.inspection import show_capture, show_playlists

console = Console()
error_console = Console(stderr=True)
app = typer.Typer(
    help="Match and safely migrate Spotify local-file playlist occurrences.",
    no_args_is_help=False,
    add_completion=False,
)


def user_errors(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except MigratorError as exc:
            error_console.print(Text(f"Error: {exc}", style="red"))
            raise typer.Exit(1) from None
        except OSError:
            error_console.print("Error: local I/O failed; check file permissions and connection.")
            raise typer.Exit(1) from None

    return wrapped


@contextmanager
def services(settings: Settings) -> Iterator[tuple[SpotifyAuth, SpotifyClient]]:
    settings.require_client_id()
    with httpx.Client(timeout=settings.http_timeout, follow_redirects=False) as transport:
        auth = SpotifyAuth(settings, transport)
        yield auth, SpotifyClient(settings, auth, transport)


def parse_playlist_argument(value: str) -> str:
    if value.startswith("spotify:playlist:"):
        value = value.removeprefix("spotify:playlist:")
    elif value.startswith("https://"):
        parsed = urlsplit(value)
        parts = parsed.path.strip("/").split("/")
        if parsed.hostname != "open.spotify.com" or len(parts) != 2 or parts[0] != "playlist":
            raise typer.BadParameter(
                "Use a Spotify playlist ID, URI or open.spotify.com playlist URL."
            )
        value = parts[1]
    return validate_playlist_id(value)


def select_playlist(client: SpotifyClient) -> str:
    console.print("Fetching playlists...")
    playlists = [playlist_summary(raw) for raw in client.playlists()]
    if not playlists:
        raise MigratorError("No playlists were returned for this account.")
    show_playlists(console, playlists)
    console.print("Item access requires ownership or actual collaborator access.")
    while True:
        choice = IntPrompt.ask("Select playlist")
        if 1 <= choice <= len(playlists):
            return playlists[choice - 1].playlist_id
        console.print(f"Choose a number between 1 and {len(playlists)}.")


def perform_scan(
    settings: Settings, playlist: str | None, *, raw: bool = False, json_output: bool = False
) -> None:
    if json_output and playlist is None:
        raise typer.BadParameter("--json requires --playlist to keep stdout machine-readable.")
    if json_output and raw:
        raise typer.BadParameter("--raw and --json cannot be combined.")
    with services(settings) as (_, client):
        playlist_id = parse_playlist_argument(playlist) if playlist else select_playlist(client)
        output = error_console if json_output else console
        output.print("Scanning playlist (read-only)...")
        capture = PlaylistScanner(client, attempts=settings.scan_attempts).scan(playlist_id)
        saved = CaptureStore(settings.data_dir).save(capture)
        if json_output:
            typer.echo(capture.model_dump_json(indent=2))
        else:
            show_capture(console, capture, raw=raw)
        output.print(Text(f"Complete scan saved: {saved}"))


@app.callback(invoke_without_command=True)
@user_errors
def configure(
    ctx: typer.Context,
    env_file: Annotated[Path, typer.Option("--env-file", help="Configuration file.")] = Path(
        ".env"
    ),
    verbose: Annotated[int, typer.Option("-v", "--verbose", count=True)] = 0,
) -> None:
    """Without a subcommand, select a playlist and scan it."""
    logging.basicConfig(
        level=logging.WARNING,
        handlers=[RichHandler(console=error_console, show_path=False, markup=False)],
        format="%(message)s",
        force=True,
    )
    logging.getLogger("spotify_local_migrator").setLevel(
        logging.DEBUG if verbose >= 2 else logging.INFO if verbose else logging.WARNING
    )
    # Third-party HTTP debug logging can include request URLs and OAuth query strings.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    ctx.obj = load_settings(env_file)
    if ctx.invoked_subcommand is None:
        console.print("[bold]Spotify Local Track Migrator[/bold]")
        perform_scan(ctx.obj, None)


@app.command()
@user_errors
def login(
    ctx: typer.Context,
    write: Annotated[
        bool, typer.Option("--write", help="Also request playlist modification permissions.")
    ] = False,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Print the login URL without opening a browser.")
    ] = False,
    manual: Annotated[
        bool, typer.Option(help="Paste the final callback URL locally; useful on a remote machine.")
    ] = False,
    timeout: Annotated[
        int, typer.Option(min=1, max=1800, help="Callback timeout in seconds.")
    ] = 300,
) -> None:
    """Authenticate with PKCE; --write also authorizes migration."""
    with services(ctx.obj) as (auth, _):

        def show_url(url: str) -> None:
            console.print("Authorize Spotify using this URL:")
            console.print(Text(url))

        if manual:
            request = auth.begin_login(write=write)
            show_url(request.url)
            callback = Prompt.ask("Paste the complete redirected URL here", password=True)
            auth.finish_login(request, callback)
        else:
            login_with_callback(
                auth, show_url, open_browser=not no_browser, timeout=timeout, write=write
            )
    console.print(
        "Logged in with playlist write permissions."
        if write
        else "Logged in with read-only playlist permissions."
    )


@app.command()
@user_errors
def playlists(
    ctx: typer.Context,
    count_local: Annotated[
        bool, typer.Option(help="Read every accessible playlist to count local occurrences.")
    ] = False,
) -> None:
    """List all paginated playlists; optionally count local tracks."""
    with services(ctx.obj) as (_, client):
        console.print("Fetching playlists...")
        summaries = [playlist_summary(raw) for raw in client.playlists()]
        counts: dict[str, str] = {}
        if count_local:
            for summary in summaries:
                try:
                    capture = PlaylistScanner(client, attempts=ctx.obj.scan_attempts).scan(
                        summary.playlist_id
                    )
                    counts[summary.playlist_id] = str(len(capture.local_tracks))
                    summary.item_count = len(capture.entries)
                except SpotifyAPIError as exc:
                    if exc.status_code != 403:
                        raise
                    counts[summary.playlist_id] = "no access"
        show_playlists(console, summaries, counts)
        if not summaries:
            console.print("No playlists returned.")
        if not count_local:
            console.print(
                "Use playlists --count-local to read each playlist and count local entries."
            )


@app.command()
@user_errors
def scan(
    ctx: typer.Context,
    playlist: Annotated[
        str | None, typer.Option("--playlist", "-p", help="Playlist ID, Spotify URI or URL.")
    ] = None,
    raw: Annotated[
        bool, typer.Option(help="Also show each local item's original API wrapper.")
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the entire capture as JSON; requires --playlist.")
    ] = False,
) -> None:
    """Select a playlist, inspect local metadata and save a stable full capture."""
    perform_scan(ctx.obj, playlist, raw=raw, json_output=json_output)


@app.command()
@user_errors
def status(ctx: typer.Context) -> None:
    """Inspect cached login and saved scan baselines without contacting Spotify."""
    settings: Settings = ctx.obj
    if settings.token_path.exists():
        try:
            token = TokenStore(settings.token_path).load()
        except MigratorError:
            console.print("Login: token file cannot be read; run login again.")
        else:
            login_status = (
                "cached; access token refresh needed"
                if token.expires_at <= time.time() + 60
                else "cached"
            )
            if settings.client_id and token.client_id != settings.client_id:
                login_status = "cached for another Client ID; run login again"
            console.print(f"Login: {login_status}")
    else:
        console.print("Login: not connected; run login after configuring .env.")
    store = CaptureStore(settings.data_dir)
    originals = store.originals()
    table = Table(title="First complete scan baselines (local files)")
    for heading in ("Playlist", "Items", "Local", "Captured", "State"):
        table.add_column(heading)
    for original in originals:
        try:
            capture = store.load(original)
            table.add_row(
                Text(capture.playlist.name),
                str(len(capture.entries)),
                str(len(capture.local_tracks)),
                capture.captured_at.isoformat(),
                "SCAN only",
            )
        except MigratorError:
            table.add_row(Text(original.parent.name), "?", "?", "?", "unreadable")
    console.print(table)
    show_job_status(settings)
    from .spotify.pacing import RequestPacer

    pacer = RequestPacer(
        settings.data_dir,
        interval=settings.request_interval_seconds,
    )
    used = pacer.usage()
    console.print(
        f"Spotify pacing: {pacer.effective_interval():g}s between API attempts; "
        f"{used} attempts in the last 24 hours; no daily cap."
    )
    console.print(
        "Cooldowns: wait and continue automatically."
        if settings.wait_for_rate_limits
        else "Cooldowns: fail immediately; automatic waiting disabled."
    )
    if settings.client_id:
        from .spotify.rate_limits import CooldownStore

        try:
            CooldownStore(settings.data_dir, settings.client_id).check()
        except SpotifyAPIError as exc:
            console.print(Text(str(exc), style="yellow"))


def selected_job(settings: Settings, job: Path | None, playlist: str | None = None):
    from .migration.jobs import JobStore, latest_job

    return (
        JobStore(job)
        if job
        else latest_job(settings.data_dir, parse_playlist_argument(playlist) if playlist else None)
    )


@app.command()
@user_errors
def match(
    ctx: typer.Context,
    playlist: Annotated[str | None, typer.Option("--playlist", "-p")] = None,
    from_scan: Annotated[Path | None, typer.Option("--from-scan")] = None,
    job: Annotated[
        Path | None, typer.Option("--job", help="Resume an existing matching job.")
    ] = None,
    config: Annotated[Path, typer.Option("--config")] = Path("config.yaml"),
    expected_artist: Annotated[list[str] | None, typer.Option("--expected-artist")] = None,
    no_cache: Annotated[bool, typer.Option("--no-cache")] = False,
) -> None:
    """Search, rank and save candidates. Never modify playlists."""
    from .migration.jobs import JobStore
    from .workflow import match_job

    if sum(bool(value) for value in (playlist, from_scan, job)) > 1:
        raise typer.BadParameter("Choose just one of --playlist, --from-scan or --job.")
    with services(ctx.obj) as (_, client):
        playlist_id = (
            parse_playlist_argument(playlist)
            if playlist
            else select_playlist(client)
            if not from_scan and not job
            else None
        )
        store = match_job(
            ctx.obj,
            client,
            console,
            playlist_id=playlist_id,
            from_scan=from_scan,
            store=JobStore(job) if job else None,
            config_path=config,
            expected_artists=expected_artist,
            no_cache=no_cache,
        )
        console.print(Text(f"Review with: spotify-local-migrate review --job {store.directory}"))


def perform_review(settings, client, store, all_tracks=False, no_cache=False):
    from .matching.cache import SearchCache
    from .matching.config import MatchingConfig
    from .matching.search import CatalogueSearch
    from .ui.review import review_report, show_results
    from .workflow import account_id

    report = store.report()
    if not report.matching_complete:
        raise MigratorError("Matching is incomplete. Run match --job for this job.")
    if account_id(client) != report.account_id:
        raise MigratorError("Job belongs to a different Spotify account.")
    config = MatchingConfig.model_validate(report.matching_config)
    with (
        store.lock(),
        SearchCache(
            settings.data_dir / ".cache" / "search.sqlite3", ttl_days=config.cache_ttl_days
        ) as cache,
    ):
        search = CatalogueSearch(client, config, report.account_id, cache=cache, no_cache=no_cache)
        report = review_report(console, store, report, search, all_tracks=all_tracks)
    show_results(console, report)


@app.command()
@user_errors
def review(
    ctx: typer.Context,
    job: Annotated[Path | None, typer.Option("--job")] = None,
    offline: Annotated[
        bool,
        typer.Option(
            "--offline", help="Review saved candidates without API calls, including a partial job."
        ),
    ] = False,
    all_tracks: Annotated[
        bool, typer.Option("--all", help="Also review automatic/unmatched tracks.")
    ] = False,
    no_cache: Annotated[bool, typer.Option("--no-cache")] = False,
) -> None:
    """Approve, reject, inspect scores or search manually."""
    store = selected_job(ctx.obj, job)
    if offline:
        from .ui.review import review_report, show_results

        report = store.report()

        class OfflineSearch:
            offline = True

        with store.lock():
            report = review_report(console, store, report, OfflineSearch(), all_tracks=all_tracks)
        show_results(console, report)
        return
    with services(ctx.obj) as (_, client):
        perform_review(ctx.obj, client, store, all_tracks, no_cache)


@app.command()
@user_errors
def migrate(
    ctx: typer.Context,
    playlist: Annotated[str | None, typer.Option("--playlist", "-p")] = None,
    job: Annotated[Path | None, typer.Option("--job")] = None,
    latest: Annotated[
        bool, typer.Option("--latest", help="Use the most recent matching job.")
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    no_review: Annotated[
        bool, typer.Option("--no-review", help="Leave ambiguous tracks unchanged.")
    ] = False,
    config: Annotated[Path, typer.Option("--config")] = Path("config.yaml"),
    expected_artist: Annotated[list[str] | None, typer.Option("--expected-artist")] = None,
    no_cache: Annotated[bool, typer.Option("--no-cache")] = False,
) -> None:
    """Match, review, show the complete plan, then confirm before writing."""
    from rich.prompt import Confirm

    from .matching.scoring import refresh_automatic_choices
    from .migration.executor import MigrationExecutor
    from .migration.planner import build_plan
    from .ui.review import show_plan
    from .workflow import match_job

    if sum(bool(value) for value in (job, playlist, latest)) > 1:
        raise typer.BadParameter("Choose --job, --latest or --playlist.")
    store = selected_job(ctx.obj, job) if job or latest else None
    if store and (store.directory / "migration.json").exists():
        raise MigratorError("Apply already started for this job. Use resume.")
    if store is None or not store.report().matching_complete:
        with services(ctx.obj) as (_, client):
            playlist_id = (
                (parse_playlist_argument(playlist) if playlist else select_playlist(client))
                if store is None
                else None
            )
            store = match_job(
                ctx.obj,
                client,
                console,
                store=store,
                playlist_id=playlist_id,
                config_path=config,
                expected_artists=expected_artist,
                no_cache=no_cache,
            )
    with store.lock():
        if (store.directory / "migration.json").exists():
            raise MigratorError("Apply already started for this job. Use resume.")
        report = store.report()
        if refresh_automatic_choices(report):
            store.save("matches.json", report)
    if not no_review and any(decision.needs_review for decision in report.decisions):
        if Confirm.ask("Review ambiguous matches?", default=True):
            with services(ctx.obj) as (_, client):
                perform_review(ctx.obj, client, store, no_cache=no_cache)
    with store.lock():
        plan = build_plan(store.capture(), store.report())
        store.save("plan.json", plan)
    show_plan(console, plan)
    console.print(Text(f"Complete plan saved: {store.directory / 'plan.json'}"))
    if dry_run:
        console.print("Dry run complete. No Spotify mutations.")
        return
    if not plan.replacements:
        console.print("No replacements selected. Playlist unchanged.")
        return
    if not Confirm.ask("Apply this plan, including the temporary API check?", default=False):
        console.print("Plan saved. Playlist unchanged.")
        return
    with services(ctx.obj) as (_, client):
        result = MigrationExecutor(
            client, progress=lambda message: console.print(Text(message))
        ).apply(store)
    console.print(f"{result.phase}: {result.next_replacement} occurrences replaced and verified.")


@app.command()
@user_errors
def resume(
    ctx: typer.Context,
    job: Annotated[Path | None, typer.Option("--job")] = None,
    playlist: Annotated[str | None, typer.Option("--playlist", "-p")] = None,
    no_cache: Annotated[bool, typer.Option("--no-cache")] = False,
    retry_unconfirmed: Annotated[
        bool,
        typer.Option(
            "--retry-unconfirmed", help="Confirm retry of a possibly in-flight insertion."
        ),
    ] = False,
) -> None:
    """Reconcile interrupted apply, or continue interrupted read-only matching."""
    from rich.prompt import Confirm

    from .migration.executor import MigrationExecutor
    from .workflow import match_job

    store = selected_job(ctx.obj, job, playlist)
    if not (store.directory / "migration.json").exists():
        with services(ctx.obj) as (_, client):
            match_job(ctx.obj, client, console, store=store, no_cache=no_cache)
        console.print(Text(f"Matching complete. Next: migrate --dry-run --job {store.directory}"))
        return
    if retry_unconfirmed:
        console.print(
            "An unconfirmed insertion may still be in flight. Wait and inspect Spotify first. "
            "Retrying can create an extra occurrence if the earlier request commits later."
        )
        if not Confirm.ask("Retry an unconfirmed insertion if still absent?", default=False):
            retry_unconfirmed = False
    with services(ctx.obj) as (_, client):
        result = MigrationExecutor(
            client, progress=lambda message: console.print(Text(message))
        ).apply(store, retry_unconfirmed_add=retry_unconfirmed)
    console.print(f"{result.phase}: {result.next_replacement} replacements verified.")


def show_job_status(settings: Settings) -> None:
    import json

    from .migration.jobs import JobStore

    table = Table(title="Matching and migration jobs")
    for heading in ("Playlist", "Matched", "Reviewed", "State", "Job"):
        table.add_column(heading)
    for file in sorted(settings.data_dir.glob("*/jobs/*/matches.json")):
        store = JobStore(file.parent)
        try:
            report = store.report()
            state = (
                "MATCH complete"
                if report.matching_complete
                else (
                    f"MATCH partial ({len(report.decisions)}/{len(store.capture().local_tracks)})"
                )
            )
            journal_path = file.parent / "migration.json"
            if journal_path.exists():
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                state = f"{journal['phase']} ({journal['next_replacement']} verified)"
                if journal.get("pending"):
                    state += " pending " + journal["pending"]["kind"]
            table.add_row(
                Text(report.playlist_name),
                str(sum(decision.candidate is not None for decision in report.decisions)),
                str(sum(decision.review_completed for decision in report.decisions)),
                Text(state),
                Text(str(file.parent)),
            )
        except (MigratorError, ValueError, KeyError):
            table.add_row("?", "?", "?", "unreadable", Text(str(file.parent)))
    console.print(table)


def main() -> None:
    app()
