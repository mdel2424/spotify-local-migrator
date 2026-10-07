import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote_plus

from ..errors import PlaylistChangedError, SpotifyAPIError
from ..models import (
    LocalTrack,
    LocalURIFields,
    PlaylistCapture,
    PlaylistEntry,
    PlaylistSummary,
)
from ..spotify.client import SpotifyClient

logger = logging.getLogger(__name__)


def text_value(value: Any) -> str | None:
    # Retain punctuation/case/spacing in observed metadata; normalize in MATCH later.
    return value if isinstance(value, str) and value.strip() else None


def positive_duration(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def parse_local_uri(uri: str | None) -> LocalURIFields | None:
    if uri is None or not uri.startswith("spotify:local:"):
        return None
    parts = uri.split(":")
    if len(parts) != 6:
        return None
    artist, album, title = [text_value(unquote_plus(value)) for value in parts[2:5]]
    try:
        duration = positive_duration(int(parts[5]) * 1000)
    except ValueError:
        duration = None
    return LocalURIFields(artist=artist, album=album, title=title, duration_ms=duration)


def parse_entry(raw: dict[str, Any] | None, position: int) -> PlaylistEntry:
    if raw is None:
        return PlaylistEntry(playlist_position=position, raw=None)
    if not isinstance(raw, dict):
        raise SpotifyAPIError("Spotify returned an invalid playlist item; scan not saved.")
    # If the new field exists and is null, do not substitute a stale legacy track.
    item = raw["item"] if "item" in raw else raw.get("track")
    if item is not None and not isinstance(item, dict):
        raise SpotifyAPIError("Spotify returned an invalid track object; scan not saved.")
    item = item or {}
    uri = text_value(item.get("uri"))
    local = (
        raw.get("is_local") is True
        or item.get("is_local") is True
        or bool(uri and uri.startswith("spotify:local:"))
    )
    entry = PlaylistEntry(
        playlist_position=position,
        uri=uri,
        item_type=text_value(item.get("type")),
        is_local=local,
        raw=raw,
    )
    if not local:
        return entry

    fallback = parse_local_uri(uri)
    title = text_value(item.get("name"))
    album_data = item.get("album")
    album = text_value(album_data.get("name")) if isinstance(album_data, dict) else None
    artist_data = item.get("artists")
    artists = [
        name
        for artist in (artist_data if isinstance(artist_data, list) else [])
        if isinstance(artist, dict) and (name := text_value(artist.get("name")))
    ]
    duration = positive_duration(item.get("duration_ms"))
    sources = {}
    values = {"title": title, "artists": artists, "album": album, "duration_ms": duration}
    uri_values = {
        "title": fallback.title if fallback else None,
        "artists": [fallback.artist] if fallback and fallback.artist else [],
        "album": fallback.album if fallback else None,
        "duration_ms": fallback.duration_ms if fallback else None,
    }
    for key in values:
        if values[key]:
            sources[key] = "item"
        elif uri_values[key]:
            values[key] = uri_values[key]
            sources[key] = "uri"
    entry.local_track = LocalTrack(
        playlist_position=position,
        uri=uri,
        **values,
        metadata_sources=sources,
        uri_metadata=fallback,
    )
    return entry


def playlist_summary(raw: dict[str, Any]) -> PlaylistSummary:
    playlist_id = text_value(raw.get("id"))
    if not playlist_id:
        raise SpotifyAPIError("Spotify playlist metadata is missing its ID.")
    collection = raw["items"] if "items" in raw else raw.get("tracks")
    total = collection.get("total") if isinstance(collection, dict) else None
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        total = None
    owner = raw.get("owner")
    return PlaylistSummary(
        playlist_id=playlist_id,
        name=text_value(raw.get("name")) or "(unnamed playlist)",
        owner_id=text_value(owner.get("id")) if isinstance(owner, dict) else None,
        collaborative=raw.get("collaborative") is True,
        item_count=total,
        snapshot_id=text_value(raw.get("snapshot_id")),
    )


class PlaylistScanner:
    def __init__(self, client: SpotifyClient, *, attempts: int = 3):
        self.client = client
        self.attempts = attempts

    def scan(self, playlist_id: str) -> PlaylistCapture:
        """Read every occurrence and publish only a complete, stable-snapshot capture."""
        for attempt in range(self.attempts):
            try:
                before = self.client.playlist(playlist_id)
                summary = playlist_summary(before)
                if summary.playlist_id != playlist_id or not summary.snapshot_id:
                    raise SpotifyAPIError("Spotify returned missing/mismatched playlist identity.")
                pages = list(self.client.playlist_item_pages(playlist_id))
                entries = [
                    parse_entry(raw, page["offset"] + index)
                    for page in pages
                    for index, raw in enumerate(page["items"])
                ]
                after = self.client.playlist(playlist_id)
                after_summary = playlist_summary(after)
                if (
                    summary.snapshot_id != after_summary.snapshot_id
                    or after_summary.playlist_id != playlist_id
                    or any(
                        count is not None and count != len(entries)
                        for count in (summary.item_count, after_summary.item_count)
                    )
                ):
                    raise PlaylistChangedError("Playlist changed during scanning.")
                summary.item_count = len(entries)
                logger.info(
                    "Stable capture complete: %s entries, %s local occurrences.",
                    len(entries),
                    sum(entry.is_local for entry in entries),
                )
                return PlaylistCapture(
                    captured_at=datetime.now(UTC),
                    playlist=summary,
                    snapshot_id=summary.snapshot_id,
                    entries=entries,
                    raw_playlist_before=before,
                    raw_playlist_after=after,
                    raw_pages=pages,
                )
            except PlaylistChangedError:
                if attempt + 1 == self.attempts:
                    raise PlaylistChangedError(
                        "Playlist kept changing or pagination was incomplete. No scan saved. "
                        "Pause other playlist editing and scan again."
                    ) from None
                logger.warning(
                    "Playlist changed during scan; restarting capture (%s).", attempt + 2
                )
        raise ValueError("Scanner attempts must be at least one.")
