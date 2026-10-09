"""Aligned local/catalogue metadata for quick review."""

import re
from difflib import SequenceMatcher

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ..matching.models import MatchDecision, SpotifyCandidate


def duration(milliseconds: int | None) -> str:
    if not milliseconds:
        return "unknown"
    seconds = milliseconds // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def _same(left: str, right: str) -> bool:
    return " ".join(left.casefold().split()) == " ".join(right.casefold().split())


def _text_pair(left: str | None, right: str | None) -> tuple[Text, Text, str]:
    if not left or not right:
        return (
            Text(left or "Missing", style="" if left else "dim"),
            Text(right or "Missing", style="" if right else "dim"),
            " ?",
        )
    if _same(left, right):
        return Text(left), Text(right), ""
    # Compare whole words and punctuation, preserving the original strings.
    # Only changed portions are emphasized; shared text stays easy to recognize.
    left_parts = re.findall(r"\w+|\W", left)
    right_parts = re.findall(r"\w+|\W", right)
    matcher = SequenceMatcher(
        None,
        [part.casefold() for part in left_parts],
        [part.casefold() for part in right_parts],
        autojunk=False,
    )
    source, target = Text(), Text()
    for operation, start, end, other_start, other_end in matcher.get_opcodes():
        style = "bold yellow" if operation != "equal" else ""
        source.append("".join(left_parts[start:end]), style=style)
        target.append("".join(right_parts[other_start:other_end]), style=style)
    return source, target, " ≠"


def show_comparison(
    console: Console, decision: MatchDecision, candidate: SpotifyCandidate | None
) -> None:
    local, prepared = decision.local_track, decision.prepared
    console.print(Text(f"\nLocal occurrence #{local.playlist_position + 1}", style="bold"))
    table = Table(box=box.ROUNDED, expand=True, padding=(0, 1), highlight=False)
    table.add_column("Field", width=16, no_wrap=True)
    table.add_column("Local track", ratio=1, overflow="fold")
    table.add_column(
        f"1. Top match ({candidate.score:.0%})" if candidate else "No Spotify match",
        ratio=1,
        overflow="fold",
    )

    def row(label: str, left: str | None, right: str | None):
        source, target, marker = _text_pair(left, right)
        table.add_row(Text(label + marker), source, target)

    title = candidate.title if candidate else None
    artists = ", ".join(candidate.artists) if candidate else None
    row("Title", local.title, title)
    if prepared.title and not _same(local.title or "", prepared.title):
        row("Search title", prepared.title, title)
    local_artists, search_artists = ", ".join(local.artists), ", ".join(prepared.artists)
    row("Artists", local_artists, artists)
    if search_artists and not _same(local_artists, search_artists):
        row("Search artists", search_artists, artists)
    source_duration = Text(duration(local.duration_ms), style="" if local.duration_ms else "dim")
    target_duration = Text(duration(candidate.duration_ms) if candidate else "Missing")
    marker = " ?" if not local.duration_ms or not candidate else ""
    if local.duration_ms and candidate:
        difference = candidate.duration_ms - local.duration_ms
        if difference:
            delta = (
                f"{difference / 1000:+.1f}"
                if abs(difference) >= 100
                else f"{difference / 1000:+.3f}"
            )
            style = "green" if abs(difference) <= 2000 else "bold yellow"
            target_duration.append(f" ({delta}s)", style=style)
            if abs(difference) > 2000:
                marker = " ≠"
    table.add_row(Text("Duration" + marker), source_duration, target_duration)
    row("Album", local.album, candidate.album if candidate else None)
    console.print(table)


def show_other_matches(console: Console, candidates: list[SpotifyCandidate]) -> None:
    table = Table(title="Other matches", box=box.SIMPLE, expand=True)
    table.add_column("#", no_wrap=True)
    table.add_column("Score", no_wrap=True)
    table.add_column("Title", ratio=2, overflow="fold")
    table.add_column("Artists", ratio=1, overflow="fold")
    table.add_column("Album", ratio=1, overflow="fold")
    table.add_column("Duration", no_wrap=True)
    for index, candidate in enumerate(candidates[1:], 2):
        table.add_row(
            str(index),
            f"{candidate.score:.0%}",
            Text(candidate.title),
            Text(", ".join(candidate.artists)),
            Text(candidate.album or "Missing"),
            duration(candidate.duration_ms),
        )
    console.print(table)
