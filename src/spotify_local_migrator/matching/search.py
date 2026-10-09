import hashlib
import json
import re
from typing import Any

from ..errors import SpotifyAPIError
from ..spotify.client import SpotifyClient
from .cache import SearchCache
from .config import MatchingConfig
from .models import PreparedTrack, SpotifyCandidate
from .normalize import artist_title_prefixes, extract_versions, normalize

ID = re.compile(r"[A-Za-z0-9]{22}")


def candidate_from_api(raw: dict[str, Any]) -> SpotifyCandidate | None:
    if not isinstance(raw, dict) or raw.get("type") != "track" or raw.get("is_local"):
        return None
    track_id = raw.get("id")
    if not isinstance(track_id, str) or not ID.fullmatch(track_id):
        return None
    if raw.get("uri") != "spotify:track:" + track_id:
        return None
    if not isinstance(raw.get("artists"), list):
        return None
    title = raw.get("name")
    artists = [
        artist["name"]
        for artist in raw.get("artists", [])
        if isinstance(artist, dict) and isinstance(artist.get("name"), str) and artist["name"]
    ]
    duration = raw.get("duration_ms")
    if not isinstance(title, str) or not title or not artists or not isinstance(duration, int):
        return None
    if duration <= 0:
        return None
    if raw.get("album") is not None and not isinstance(raw.get("album"), dict):
        return None
    album = raw.get("album") or {}
    external = raw.get("external_ids") or {}
    if not isinstance(album, dict) or not isinstance(external, dict) or isinstance(duration, bool):
        return None
    return SpotifyCandidate(
        spotify_id=track_id,
        uri=raw["uri"],
        title=title,
        artists=artists,
        artist_ids=[
            artist["id"]
            for artist in raw.get("artists", [])
            if isinstance(artist, dict) and isinstance(artist.get("id"), str)
        ],
        album=album.get("name"),
        duration_ms=duration,
        is_playable=raw.get("is_playable"),
        isrc=external.get("isrc"),
        raw=raw,
    )


def search_queries(prepared: PreparedTrack) -> list[str]:
    title = prepared.core_title
    if not title:
        return []
    queries = []
    if prepared.artist_source != "title":
        # Add precise searches for unconfirmed title prefixes. Preserve the
        # original queries so ordinary hyphenated song titles still resolve.
        for artists, song, _ in artist_title_prefixes(prepared.title):
            core, _ = extract_versions(song)
            if core:
                for artist in artists[:3]:
                    queries.append(f'track:"{core}" artist:"{normalize(artist)}"')
    for artist in prepared.primary_artists[:3]:
        queries.append(f'track:"{title}" artist:"{normalize(artist)}"')
    if prepared.primary_artists:
        queries.append(title + " " + normalize(prepared.primary_artists[0]))
    queries.append(f'track:"{title}"')
    return list(dict.fromkeys(queries))


class CatalogueSearch:
    def __init__(
        self,
        client: SpotifyClient,
        config: MatchingConfig,
        account_id: str,
        *,
        cache: SearchCache | None = None,
        no_cache: bool = False,
    ):
        self.client = client
        self.config = config
        self.account_id = account_id
        self.cache = cache
        self.no_cache = no_cache
        self.requests = 0

    def query(self, query: str) -> list[SpotifyCandidate]:
        candidates: dict[str, SpotifyCandidate] = {}
        for page in range(self.config.search_pages):
            offset = page * 10
            key = hashlib.sha256(
                json.dumps(
                    [self.account_id, self.config.market, query, offset, 10],
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            response = self.cache.get(key) if self.cache and not self.no_cache else None
            if response is None:
                response = self.client.search_tracks(
                    query, market=self.config.market, offset=offset
                )
                self.requests += 1
                if self.cache and not self.no_cache:
                    self.cache.put(key, response)
            tracks = response.get("tracks")
            if not isinstance(tracks, dict) or not isinstance(tracks.get("items"), list):
                raise SpotifyAPIError("Spotify search returned a malformed track page.")
            for raw in tracks["items"]:
                candidate = candidate_from_api(raw)
                if candidate and candidate.spotify_id not in candidates:
                    candidate.queries = [query]
                    candidate.search_order = len(candidates)
                    candidates[candidate.spotify_id] = candidate
            if not tracks.get("next") or not tracks["items"]:
                break
        return list(candidates.values())

    def for_track(self, prepared: PreparedTrack) -> tuple[list[SpotifyCandidate], list[str]]:
        candidates: dict[str, SpotifyCandidate] = {}
        queries = search_queries(prepared)
        # Gather all query variants before comparing margins: the first result
        # crossing a threshold is not proof that broader queries have no rival.
        for query in queries:
            for candidate in self.query(query):
                previous = candidates.get(candidate.spotify_id)
                if previous:
                    previous.queries = list(dict.fromkeys(previous.queries + candidate.queries))
                else:
                    candidate.search_order = len(candidates)
                    candidates[candidate.spotify_id] = candidate
        return list(candidates.values()), queries
