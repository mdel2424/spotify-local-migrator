"""Pure, read-only occurrence-preserving migration planning."""

from collections import Counter
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..errors import StateError
from ..matching.models import MatchReport
from ..models import PlaylistCapture
from .jobs import content_hash


class EntryIdentity(BaseModel):
    uri: str | None
    is_local: bool
    item_type: str | None


class Replacement(BaseModel):
    position: int = Field(ge=0)
    local_uri: str | None
    replacement_uri: str
    spotify_id: str
    title: str
    artists: list[str]
    selection: Literal["auto", "approved"]
    score: float


class PlannedRequest(BaseModel):
    method: Literal["POST", "DELETE"]
    endpoint: str
    body: dict


class MigrationPlan(BaseModel):
    schema_version: int = 1
    phase: Literal["PLAN"] = "PLAN"
    playlist_id: str
    playlist_name: str
    account_id: str
    snapshot_id: str
    capture_hash: str
    matches_hash: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    original: list[EntryIdentity]
    desired: list[EntryIdentity]
    replacements: list[Replacement]
    local_count: int
    duplicate_warnings: list[str]
    requests: list[PlannedRequest] = Field(default_factory=list)
    # Not an officially documented payload. Test it on an isolated playlist
    # before ANY insertion/removal in the user's original playlist.
    removal_payload: dict = Field(
        default_factory=lambda: {
            "items": [],
            "positions": ["<local position after insertion>"],
            "snapshot_id": "<verified snapshot>",
        }
    )


def identities(capture: PlaylistCapture) -> list[EntryIdentity]:
    return [
        EntryIdentity(uri=entry.uri, is_local=entry.is_local, item_type=entry.item_type)
        for entry in capture.entries
    ]


def build_plan(capture: PlaylistCapture, report: MatchReport) -> MigrationPlan:
    if not report.matching_complete:
        raise StateError("Matching is incomplete. Resume matching before planning.")
    if (
        report.capture_hash != content_hash(capture)
        or report.playlist_id != capture.playlist.playlist_id
        or report.snapshot_id != capture.snapshot_id
    ):
        raise StateError("Matches do not belong to this playlist capture.")
    local_by_position = {track.playlist_position: track for track in capture.local_tracks}
    if len(report.decisions) != len(local_by_position):
        raise StateError("Matching report does not cover every local occurrence.")
    seen = set()
    replacements = []
    original = identities(capture)
    desired = [entry.model_copy() for entry in original]
    for decision in report.decisions:
        position = decision.local_track.playlist_position
        if position in seen or decision.local_track != local_by_position.get(position):
            raise StateError("Local metadata/positions were changed in the matching report.")
        seen.add(position)
        if decision.status not in ("auto", "approved"):
            if decision.candidate is not None:
                raise StateError("Unselected decision contains a replacement.")
            continue
        candidate = decision.candidate
        if candidate is None or candidate.uri != "spotify:track:" + candidate.spotify_id:
            raise StateError("Selected candidate has no valid catalogue URI.")
        from .state import validate_playlist_id

        validate_playlist_id(candidate.spotify_id)
        if candidate.is_playable is not True:
            raise StateError("Replacement playability must be confirmed before planning.")
        desired[position] = EntryIdentity(uri=candidate.uri, is_local=False, item_type="track")
        replacements.append(
            Replacement(
                position=position,
                local_uri=decision.local_track.uri,
                replacement_uri=candidate.uri,
                spotify_id=candidate.spotify_id,
                title=candidate.title,
                artists=candidate.artists,
                selection=decision.status,
                score=candidate.score,
            )
        )
    existing = Counter(entry.uri for entry in original if not entry.is_local and entry.uri)
    additions = Counter(item.replacement_uri for item in replacements)
    warnings = []
    for uri, count in additions.items():
        if existing[uri] or count > 1:
            warnings.append(
                f"{uri}: {existing[uri]} existing catalogue occurrence(s), "
                f"{count} local occurrence(s) to replace; final count {existing[uri] + count}. "
                "Each original occurrence is preserved."
            )
    # Each pair leaves the playlist length unchanged. Starting at the end
    # keeps lower original positions stable. Never DELETE by URI: duplicates
    # and other local occurrences must retain their positions/counts.
    replacements.sort(key=lambda item: item.position, reverse=True)
    endpoint = f"/playlists/{report.playlist_id}/items"
    requests = []
    for replacement in replacements:
        requests.extend(
            [
                PlannedRequest(
                    method="POST",
                    endpoint=endpoint,
                    body={
                        "uris": [replacement.replacement_uri],
                        "position": replacement.position,
                    },
                ),
                PlannedRequest(
                    method="DELETE",
                    endpoint=endpoint,
                    body={
                        "items": [],
                        "positions": [replacement.position + 1],
                        "snapshot_id": "<verified snapshot returned by preceding insertion>",
                    },
                ),
            ]
        )
    return MigrationPlan(
        playlist_id=report.playlist_id,
        playlist_name=report.playlist_name,
        account_id=report.account_id,
        snapshot_id=report.snapshot_id,
        capture_hash=report.capture_hash,
        matches_hash=content_hash(report),
        original=original,
        desired=desired,
        replacements=sorted(replacements, key=lambda item: item.position, reverse=True),
        local_count=len(capture.local_tracks),
        duplicate_warnings=warnings,
        requests=requests,
    )
