from rich.console import Console
from rich.prompt import IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from ..errors import StateError
from ..matching.config import MatchingConfig
from ..matching.models import MatchDecision, MatchReport
from ..matching.scoring import group_candidates, rank_candidates, refresh_automatic_choices
from ..matching.search import CatalogueSearch
from ..migration.jobs import JobStore
from ..migration.planner import MigrationPlan


def duration(milliseconds: int | None) -> str:
    if not milliseconds:
        return "unknown"
    seconds = milliseconds // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def content_rating(explicit: bool | None) -> str:
    return (
        "Explicit"
        if explicit is True
        else "Non-explicit"
        if explicit is False
        else "Rating unknown"
    )


def show_results(console: Console, report: MatchReport) -> None:
    table = Table(title="Results")
    table.add_column("Decision")
    table.add_column("Occurrences", justify="right")
    for label, count in [
        ("Automatic", sum(d.status == "auto" for d in report.decisions)),
        ("Approved", sum(d.status == "approved" for d in report.decisions)),
        ("Needs review", sum(d.needs_review for d in report.decisions)),
        (
            "Unmatched / left unchanged",
            sum(d.candidate is None and not d.needs_review for d in report.decisions),
        ),
    ]:
        table.add_row(label, str(count))
    console.print(table)
    console.print(
        Text(
            "Expected artists: "
            + ", ".join(report.expected_artists)
            + f" ({report.expected_artist_source})"
        )
    )


def review_decision(
    console: Console,
    decision: MatchDecision,
    search: CatalogueSearch,
    config: MatchingConfig,
) -> None:
    local = decision.local_track
    console.print(Text(f"\nLocal occurrence #{local.playlist_position + 1}: {local.title or '?'}"))
    console.print(Text(f"Tags: {', '.join(local.artists) or 'missing'}"))
    console.print(
        Text(
            f"Cleaned: {decision.prepared.title} | "
            f"Artists: {', '.join(decision.prepared.artists)} | "
            f"Album: {local.album or 'missing'} | Duration: {duration(local.duration_ms)}"
        )
    )
    for warning in decision.prepared.warnings + decision.reasons:
        console.print(Text(warning, style="yellow"))
    decision.candidates = rank_candidates(local, decision.prepared, decision.candidates, config)
    groups = group_candidates(decision.candidates)[:10]
    candidates = [group[0] for group in groups]
    while True:
        for index, candidate in enumerate(candidates, 1):
            alternatives = len(groups[index - 1]) - 1
            releases = f" | {alternatives} alternate release(s)" if alternatives else ""
            console.print(
                Text(
                    f"{index}. {', '.join(candidate.artists)} - {candidate.title}\n"
                    f"   {candidate.album or '?'} | {duration(candidate.duration_ms)} | "
                    f"{content_rating(candidate.explicit)}{releases} | "
                    f"{candidate.score:.0%} | "
                    f"https://open.spotify.com/track/{candidate.spotify_id}"
                )
            )
        manual, leave, debug = len(candidates) + 1, len(candidates) + 2, len(candidates) + 3
        console.print(f"{manual}. Search manually\n{leave}. Leave unchanged\n{debug}. Show scores")
        choice = IntPrompt.ask("Choice", default=leave)
        if choice == manual:
            if getattr(search, "offline", False):
                console.print(
                    "Manual search requires a live connection. "
                    "Select a saved result or leave unchanged."
                )
                continue
            query = Prompt.ask("Spotify search query").strip()
            if not query:
                continue
            ranked = rank_candidates(local, decision.prepared, search.query(query), config)
            groups = group_candidates(ranked)[:10]
            candidates = [group[0] for group in groups]
            decision.searched_queries.append(query)
            known = {candidate.spotify_id for candidate in decision.candidates}
            next_order = (
                max(
                    (
                        item.search_order
                        for item in decision.candidates
                        if item.search_order is not None
                    ),
                    default=-1,
                )
                + 1
            )
            for candidate in sorted(ranked, key=lambda item: item.search_order):
                if candidate.spotify_id not in known:
                    candidate.search_order = next_order
                    next_order += 1
                    decision.candidates.append(candidate)
        elif choice == leave:
            decision.status = "rejected"
            decision.candidate = None
            break
        elif choice == debug:
            for group in groups:
                for candidate in group:
                    console.print(
                        Text(
                            f"{candidate.title} | {candidate.album or '?'} | "
                            f"{content_rating(candidate.explicit)} | "
                            f"https://open.spotify.com/track/{candidate.spotify_id}"
                        )
                    )
                    for key, value in candidate.score_breakdown.items():
                        console.print(f"  {key}: {value:.4f}")
                    for reason in candidate.reasons:
                        console.print(Text(reason))
        elif 1 <= choice <= len(candidates):
            selected = candidates[choice - 1]
            if selected.is_playable is not True:
                console.print("This candidate's availability is unconfirmed; choose another.")
                continue
            decision.candidate = selected
            decision.status = "approved"
            break
        else:
            console.print("Choose one of the displayed options.")
    decision.needs_review = False
    decision.review_completed = True


def review_report(
    console: Console,
    store: JobStore,
    report: MatchReport,
    search: CatalogueSearch,
    *,
    all_tracks: bool = False,
) -> MatchReport:
    if (store.directory / "migration.json").exists():
        raise StateError("Apply already started; decisions are locked. Use resume.")
    if refresh_automatic_choices(report):
        store.save("matches.json", report)
    config = MatchingConfig.model_validate(report.matching_config)
    for decision in report.decisions:
        if all_tracks or decision.needs_review:
            review_decision(console, decision, search, config)
            report.phase = "REVIEW"
            store.save("matches.json", report)
    return report


def show_plan(console: Console, plan: MigrationPlan) -> None:
    console.print(Text(f"\nMigration plan: {plan.playlist_name}"))
    count = len(plan.replacements)
    console.print(
        f"Local occurrences: {plan.local_count}\n"
        f"Replace: {count}\nLeave: {plan.local_count - count}\n"
        f"Add Spotify: {count}\nDelete local: {count}"
    )
    table = Table(title="Planned replacements (executed from bottom to top)")
    for column in ("Position (1-based)", "Replacement", "Selection", "Score"):
        table.add_column(column)
    for replacement in plan.replacements:
        table.add_row(
            str(replacement.position + 1),
            Text(", ".join(replacement.artists) + " - " + replacement.title),
            replacement.selection,
            f"{replacement.score:.0%}",
        )
    console.print(table)
    for warning in plan.duplicate_warnings:
        console.print(Text(warning, style="yellow"))
    if count:
        console.print(
            "Apply first creates a private catalogue-only test playlist to verify "
            "position removal and snapshot behavior, then removes it from your library. "
            "If verification fails, your original playlist stays untouched."
        )
        console.print(
            "Keep playlist editing paused until migration finishes. "
            "Each positional deletion is verified and sent once; simultaneous edits "
            "can move its target before Spotify processes it."
        )
