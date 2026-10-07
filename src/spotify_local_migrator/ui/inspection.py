from rich.console import Console
from rich.json import JSON
from rich.table import Table
from rich.text import Text

from ..models import PlaylistCapture, PlaylistSummary


def show_playlists(
    console: Console,
    playlists: list[PlaylistSummary],
    counts: dict[str, str] | None = None,
) -> None:
    table = Table(title="Your Spotify playlists")
    for heading in ("#", "Playlist", "Items", "Local", "Owner"):
        table.add_column(heading, justify="right" if heading in ("#", "Items", "Local") else "left")
    for index, playlist in enumerate(playlists, start=1):
        table.add_row(
            str(index),
            Text(playlist.name),
            str(playlist.item_count) if playlist.item_count is not None else "unknown",
            (counts or {}).get(playlist.playlist_id, "not scanned"),
            Text(playlist.owner_id or "unknown"),
        )
    console.print(table)


def duration_text(duration_ms: int | None) -> str:
    if duration_ms is None:
        return "(missing)"
    seconds = duration_ms // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def show_capture(console: Console, capture: PlaylistCapture, *, raw: bool = False) -> None:
    console.print(Text(f'{len(capture.local_tracks)} local tracks in "{capture.playlist.name}".'))
    console.print(f"All playlist entries read: {len(capture.entries)}")
    console.print(Text(f"Snapshot: {capture.snapshot_id}"))
    console.print(f"https://open.spotify.com/playlist/{capture.playlist.playlist_id}")
    for track in capture.local_tracks:
        console.print()
        console.print(
            Text(f"Playlist position {track.playlist_position} (zero-based)", style="bold")
        )
        details = Table(show_header=False, box=None, padding=(0, 1))
        values = [
            ("Title", track.title or "(missing)", "title"),
            ("Artists", ", ".join(track.artists) or "(missing)", "artists"),
            ("Album", track.album or "(missing)", "album"),
            ("Duration", duration_text(track.duration_ms), "duration_ms"),
            ("URI", track.uri or "(missing)", None),
        ]
        for label, value, key in values:
            source = " [URI fallback]" if key and track.metadata_sources.get(key) == "uri" else ""
            details.add_row(label + ":", Text(value + source))
        console.print(details)
        if raw:
            console.print("Original Spotify item wrapper:")
            console.print(JSON.from_data(capture.entries[track.playlist_position].raw))
