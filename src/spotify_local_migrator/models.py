from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr


class TokenData(BaseModel):
    client_id: str = Field(repr=False, min_length=1)
    access_token: SecretStr = Field(min_length=1)
    refresh_token: SecretStr = Field(min_length=1)
    expires_at: float = Field(gt=0, allow_inf_nan=False)
    scopes: list[str]


class LocalURIFields(BaseModel):
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    duration_ms: int | None = None


class LocalTrack(BaseModel):
    playlist_position: int = Field(ge=0)
    uri: str | None = None
    title: str | None = None
    artists: list[str] = Field(default_factory=list)
    album: str | None = None
    duration_ms: int | None = None
    metadata_sources: dict[str, Literal["item", "uri"]] = Field(default_factory=dict)
    uri_metadata: LocalURIFields | None = None


class PlaylistEntry(BaseModel):
    playlist_position: int = Field(ge=0)
    uri: str | None = None
    item_type: str | None = None
    is_local: bool = False
    local_track: LocalTrack | None = None
    raw: dict[str, Any] | None = None


class PlaylistSummary(BaseModel):
    playlist_id: str
    name: str
    owner_id: str | None = None
    collaborative: bool = False
    item_count: int | None = None
    snapshot_id: str | None = None


class PlaylistCapture(BaseModel):
    schema_version: int = 1
    phase: Literal["SCAN"] = "SCAN"
    captured_at: datetime
    playlist: PlaylistSummary
    snapshot_id: str
    entries: list[PlaylistEntry]
    raw_playlist_before: dict[str, Any]
    raw_playlist_after: dict[str, Any]
    raw_pages: list[dict[str, Any]]

    @property
    def local_tracks(self) -> list[LocalTrack]:
        return [entry.local_track for entry in self.entries if entry.local_track is not None]
