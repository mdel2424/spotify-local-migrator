import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from spotify_local_migrator.errors import PlaylistChangedError, SpotifyAPIError
from spotify_local_migrator.migration.scanner import (
    PlaylistScanner,
    parse_entry,
    parse_local_uri,
    playlist_summary,
)

PLAYLIST_ID = "A" * 22


def test_local_metadata_and_full_raw_wrapper_are_preserved(raw_local):
    entry = parse_entry(raw_local, 10)
    track = entry.local_track
    assert track.playlist_position == 10
    assert track.title == "Elizabeth"
    assert track.artists == ["Westside Gunn"]
    assert track.album == "Supreme Blientele"
    assert track.duration_ms == 241000
    assert all(source == "item" for source in track.metadata_sources.values())
    assert entry.raw == raw_local
    assert track.uri_metadata.title == "Elizabeth"


def test_legacy_response_still_parses(raw_local):
    legacy = deepcopy(raw_local)
    legacy["track"] = legacy.pop("item")
    assert parse_entry(legacy, 0).local_track.title == "Elizabeth"


def test_new_null_item_does_not_use_legacy_fallback(raw_local):
    assert parse_entry({"item": None, "track": raw_local["item"]}, 0).uri is None


@pytest.mark.parametrize("evidence", ["entry", "item", "uri"])
def test_local_detection_variants(evidence):
    raw = {"item": {"type": "track", "name": "Local"}}
    if evidence == "entry":
        raw["is_local"] = True
    elif evidence == "item":
        raw["item"]["is_local"] = True
    else:
        raw["item"]["uri"] = "spotify:local:A:B:C:1"
    assert parse_entry(raw, 0).is_local


def test_missing_metadata_uses_labeled_unicode_uri_fallbacks():
    uri = "spotify:local:Bj%C3%B6rk:Album%3A+One:Don%E2%80%99t+Go%2B:123"
    entry = parse_entry(
        {
            "is_local": True,
            "item": {
                "uri": uri,
                "name": "",
                "artists": [],
                "album": None,
                "duration_ms": 0,
            },
        },
        4,
    )
    track = entry.local_track
    assert track.title == "Don’t Go+"
    assert track.artists == ["Björk"]
    assert track.album == "Album: One"
    assert track.duration_ms == 123000
    assert set(track.metadata_sources.values()) == {"uri"}


def test_item_metadata_wins_when_uri_disagrees(raw_local):
    raw_local["item"]["name"] = "  Observed title (Live)  "
    raw_local["item"]["artists"] = [{"name": "Artist"}, {"name": "Feature"}]
    track = parse_entry(raw_local, 0).local_track
    assert track.title == "  Observed title (Live)  "
    assert track.artists == ["Artist", "Feature"]
    assert track.uri_metadata.title == "Elizabeth"
    assert track.metadata_sources["title"] == "item"


def test_local_null_object_keeps_position_and_unknown_fields():
    entry = parse_entry({"is_local": True, "item": None}, 7)
    assert entry.local_track.title is None
    assert entry.local_track.artists == []
    assert entry.local_track.album is None
    assert entry.local_track.duration_ms is None
    assert entry.local_track.playlist_position == 7
    assert entry.local_track.metadata_sources == {}


@pytest.mark.parametrize(
    "uri", [None, "spotify:track:abc", "spotify:local:x", "spotify:local:A:B:C:1:2"]
)
def test_unparseable_uri_does_not_invent_metadata(uri):
    assert parse_local_uri(uri) is None


@pytest.mark.parametrize("duration", ["", "unknown", "0", "-5"])
def test_bad_uri_duration_is_unknown(duration):
    fields = parse_local_uri("spotify:local:::04_track.mp3:" + duration)
    assert fields.duration_ms is None
    assert fields.title == "04_track.mp3"


def test_malformed_entry_rejected():
    with pytest.raises(SpotifyAPIError):
        parse_entry("bad", 0)
    with pytest.raises(SpotifyAPIError):
        parse_entry({"item": []}, 0)


def test_playlist_summary_current_and_legacy_count():
    for field in ("items", "tracks"):
        summary = playlist_summary(
            {
                "id": PLAYLIST_ID,
                "name": "Artist",
                field: {"total": 183},
                "owner": {"id": "owner"},
                "collaborative": True,
            }
        )
        assert summary.item_count == 183
        assert summary.owner_id == "owner"
    assert playlist_summary({"id": PLAYLIST_ID}).item_count is None


def test_full_scan_preserves_positions_episodes_nulls_and_duplicates(make_api):
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "playlist_items_2026.json").read_text()
    )
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/items"):
            offset = int(request.url.params["offset"])
            rows = fixture["items"][offset : offset + 3]
            return httpx.Response(
                200,
                json={
                    "items": rows,
                    "offset": offset,
                    "total": fixture["total"],
                    "next": "next" if offset + len(rows) < fixture["total"] else None,
                },
            )
        return httpx.Response(
            200,
            json={
                "id": PLAYLIST_ID,
                "name": "Mixed playlist",
                "snapshot_id": "stable",
                "items": {"total": fixture["total"]},
                "owner": {"id": "owner"},
            },
        )

    api, _, _ = make_api(handler)
    capture = PlaylistScanner(api).scan(PLAYLIST_ID)
    assert len(capture.entries) == 7
    assert [entry.playlist_position for entry in capture.entries] == list(range(7))
    assert [track.playlist_position for track in capture.local_tracks] == [0, 4, 6]
    assert capture.local_tracks[0].uri == capture.local_tracks[1].uri
    assert capture.entries[2].raw == {"is_local": False, "item": None}
    assert capture.entries[3].item_type == "episode"
    assert capture.entries[5].raw is None
    assert [page["offset"] for page in capture.raw_pages] == [0, 3, 6]
    assert all(request.method == "GET" for request in requests)
    assert len(requests) == 5


def test_snapshot_change_retries_entire_scan(make_api, raw_local):
    metadata_calls = []
    item_calls = []

    def handler(request):
        if request.url.path.endswith("/items"):
            item_calls.append(request)
            return httpx.Response(
                200,
                json={
                    "items": [raw_local],
                    "offset": 0,
                    "total": 1,
                    "next": None,
                },
            )
        metadata_calls.append(request)
        return httpx.Response(
            200,
            json={
                "id": PLAYLIST_ID,
                "name": "Artist",
                "snapshot_id": "old" if len(metadata_calls) == 1 else "new",
                "items": {"total": 1},
            },
        )

    api, _, _ = make_api(handler)
    capture = PlaylistScanner(api).scan(PLAYLIST_ID)
    assert capture.snapshot_id == "new"
    assert len(metadata_calls) == 4
    assert len(item_calls) == 2


def test_continuously_changing_playlist_fails_after_bounded_attempts(make_api):
    metadata_calls = []

    def handler(request):
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json={"items": [], "offset": 0, "total": 0, "next": None})
        metadata_calls.append(request)
        return httpx.Response(
            200,
            json={
                "id": PLAYLIST_ID,
                "snapshot_id": str(len(metadata_calls)),
                "items": {"total": 0},
            },
        )

    api, _, _ = make_api(handler)
    with pytest.raises(PlaylistChangedError, match="No scan saved"):
        PlaylistScanner(api).scan(PLAYLIST_ID)
    assert len(metadata_calls) == 6


@pytest.mark.parametrize(
    "metadata",
    [
        {"id": PLAYLIST_ID, "snapshot_id": None, "items": {"total": 0}},
        {"id": "Z" * 22, "snapshot_id": "stable", "items": {"total": 0}},
    ],
)
def test_missing_or_mismatched_identity_rejected(make_api, metadata):
    api, _, _ = make_api(lambda _: httpx.Response(200, json=metadata))
    with pytest.raises(SpotifyAPIError):
        PlaylistScanner(api).scan(PLAYLIST_ID)


def test_metadata_total_mismatch_cannot_publish_scan(make_api):
    def handler(request):
        return httpx.Response(
            200,
            json=(
                {"items": [], "offset": 0, "total": 0, "next": None}
                if request.url.path.endswith("/items")
                else {
                    "id": PLAYLIST_ID,
                    "snapshot_id": "stable",
                    "items": {"total": 1},
                }
            ),
        )

    api, _, _ = make_api(handler)
    with pytest.raises(PlaylistChangedError):
        PlaylistScanner(api).scan(PLAYLIST_ID)
