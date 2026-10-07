from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..errors import ConfigurationError


class ScoreWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: float = Field(default=0.50, ge=0)
    artist: float = Field(default=0.30, ge=0)
    duration: float = Field(default=0.15, ge=0)
    album: float = Field(default=0.05, ge=0)

    @model_validator(mode="after")
    def nonzero(self):
        if self.title <= 0 or self.artist <= 0:
            raise ValueError("Title and artist must have positive weights.")
        return self


class PlaylistHints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_artists: list[str] = Field(default_factory=list)


class MatchingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weights: ScoreWeights = Field(default_factory=ScoreWeights)
    auto_threshold: float = Field(default=0.90, ge=0, le=1)
    review_threshold: float = Field(default=0.70, ge=0, le=1)
    minimum_margin: float = Field(default=0.05, ge=0, le=1)
    auto_title_minimum: float = Field(default=0.94, ge=0, le=1)
    auto_artist_minimum: float = Field(default=0.95, ge=0, le=1)
    version_penalty: float = Field(default=0.22, ge=0, le=1)
    remaster_penalty: float = Field(default=0.08, ge=0, le=1)
    cache_ttl_days: float = Field(default=7, gt=0)
    search_pages: int = Field(default=2, ge=1, le=5)
    market: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    producer_names: list[str] = Field(default_factory=list)
    uploader_names: list[str] = Field(default_factory=list)
    playlists: dict[str, PlaylistHints] = Field(default_factory=dict)

    @model_validator(mode="after")
    def thresholds(self):
        if self.review_threshold > self.auto_threshold:
            raise ValueError("Review threshold cannot exceed automatic threshold.")
        return self


def load_matching_config(path: Path = Path("config.yaml")) -> MatchingConfig:
    if not path.exists():
        return MatchingConfig()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return MatchingConfig.model_validate(data)
    except (OSError, ValueError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigurationError("Invalid matching configuration; check config.yaml.") from exc
