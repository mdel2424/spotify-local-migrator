from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from ..errors import StateError
from ..matching.config import MatchingConfig
from ..matching.models import MatchDecision, MatchReport
from ..matching.normalize import corroborate_title_artist
from ..matching.scoring import rank_candidate_groups, refresh_unreviewed_choices
from ..matching.search import CatalogueSearch
from ..migration.jobs import JobStore
from ..migration.planner import MigrationPlan
from .comparison import show_comparison, show_other_matches
from .prompts import ReviewChoicePrompt


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
            "Playlist artist tags: "
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
    ranked_groups = rank_candidate_groups(local, decision.prepared, decision.candidates, config)
    decision.candidates = [candidate for group in ranked_groups for candidate in group]
    groups = ranked_groups[:10]
    candidates = [group[0] for group in groups]
    if candidates:
        decision.prepared = corroborate_title_artist(decision.prepared, candidates[0].artists)
    warnings = decision.prepared.warnings + decision.reasons
    show_others = False
    while True:
        show_comparison(console, decision, candidates[0] if candidates else None)
        if candidates:
            candidate = candidates[0]
            alternatives = len(groups[0]) - 1
            releases = f" | {alternatives} alternate release(s)" if alternatives else ""
            console.print(
                Text(
                    f"{content_rating(candidate.explicit)}{releases} | "
                    f"https://open.spotify.com/track/{candidate.spotify_id}"
                )
            )
            if candidate.is_playable is not True:
                console.print(
                    "Top match's availability is unconfirmed; choose another.", style="yellow"
                )
        for warning in dict.fromkeys(warnings):
            console.print(Text(warning, style="yellow"))
        if show_others:
            show_other_matches(console, candidates)
        manual, leave, debug = len(candidates) + 1, len(candidates) + 2, len(candidates) + 3
        more = len(candidates) + 4
        if len(candidates) > 1:
            console.print(
                f"{more}. Show {len(candidates) - 1} other matches (choices 2–{len(candidates)})"
            )
        console.print(f"{manual}. Search manually\n{leave}. Leave unchanged\n{debug}. Show scores")
        default = 1 if candidates else leave
        shortcut = (
            "Enter: accept top match; Esc: leave unchanged"
            if candidates
            else "Enter/Esc: leave unchanged"
        )
        choice = ReviewChoicePrompt.ask(f"Choice ({shortcut})", default=default, console=console)
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
            ranked_groups = rank_candidate_groups(
                local, decision.prepared, search.query(query), config
            )
            ranked = [candidate for group in ranked_groups for candidate in group]
            groups = ranked_groups[:10]
            candidates = [group[0] for group in groups]
            warnings = decision.prepared.warnings + (candidates[0].reasons if candidates else [])
            show_others = False
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
        elif choice in (leave, ReviewChoicePrompt.ESCAPE):
            decision.status = "rejected"
            decision.candidate = None
            break
        elif choice == more and len(candidates) > 1:
            show_others = True
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
    if refresh_unreviewed_choices(report):
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
