from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models import LocalTrack


class PreparedTrack(BaseModel):
    title: str
    core_title: str
    artists: list[str]
    primary_artists: list[str]
    featured_artists: list[str] = Field(default_factory=list)
    artist_source: Literal["title", "metadata", "configured", "inferred", "missing"]
    qualifiers: list[str] = Field(default_factory=list)
    removed_annotations: list[str] = Field(default_factory=list)
    ignored_artist_tags: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    filename_like: bool = False


class SpotifyCandidate(BaseModel):
    spotify_id: str
    uri: str
    title: str
    artists: list[str]
    artist_ids: list[str] = Field(default_factory=list)
    album: str | None = None
    duration_ms: int = Field(gt=0)
    is_playable: bool | None = None
    isrc: str | None = None
    score: float = Field(default=0, ge=0, le=1)
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class MatchDecision(BaseModel):
    local_track: LocalTrack
    prepared: PreparedTrack
    candidates: list[SpotifyCandidate] = Field(default_factory=list)
    candidate: SpotifyCandidate | None = None
    status: Literal["auto", "approved", "rejected", "unmatched"] = "unmatched"
    needs_review: bool = False
    review_completed: bool = False
    reasons: list[str] = Field(default_factory=list)
    searched_queries: list[str] = Field(default_factory=list)


class MatchReport(BaseModel):
    schema_version: int = 1
    phase: Literal["MATCH", "REVIEW", "PLAN"] = "MATCH"
    playlist_id: str
    playlist_name: str
    snapshot_id: str
    capture_hash: str
    account_id: str
    created_at: datetime
    expected_artists: list[str]
    expected_artist_source: Literal["configured", "inferred", "missing"]
    producer_names: list[str]
    matching_config: dict[str, Any]
    decisions: list[MatchDecision] = Field(default_factory=list)
    matching_complete: bool = False
    real_api_validation: bool = False
    validation_summary: dict[str, Any] = Field(default_factory=dict)
