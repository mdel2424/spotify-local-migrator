# Spotify Local Track Migrator

## Description

A Python 3.12+ CLI that replaces local-file occurrences in Spotify playlists
with matching Spotify catalogue tracks. It supports automatic matching, manual
review, dry runs, and resumable migration while preserving track order,
duplicates, and unmatched local files.
It can also remove local copies when a web migration has already added their
Spotify counterparts to the playlist.

## Implementation details

The scanner captures every ordered playlist occurrence. Matching cleans title
annotations, extracts artist and producer credits, and searches Spotify using
several title/artist queries. An embedded `Artist - Title` credit takes priority
over uploader tags when the Spotify artist corroborates it. Default scoring
weights are title 50%, artist 30%, duration 15%, and album 5%; missing fields
redistribute the weights. Automatic
selection requires a score of at least 90%, sufficient title/artist evidence,
compatible versions, duration agreement, and a margin over other recordings.
Review normally requires at least 70% plus supporting title and artist evidence.
Strong title/artist agreement can reach review despite runtime or version
differences. A near-exact title and close runtime (within 10 seconds and 5%)
can reach review with conflicting artist tags; artist identity still needs
confirmation. Plausible matches appear before results with weak identity evidence.

Equivalent album releases are grouped using recording identifiers, title,
artists, and duration. Playable explicit versions take priority over clean
counterparts; otherwise, Spotify's first matching search result is preferred.
Manual choices remain saved.

Cleanup compares local tracks only against Spotify tracks in the captured
playlist, using the same artist hints and review rules. It pairs occurrences
one for one, removes selected locals from bottom to top, and preserves every
existing Spotify occurrence in order. Uncertain and unpaired locals remain.
Each deletion uses the same compatibility check, journal and verification as
migration. Missing availability information is checked only for plausible
counterparts; matching and manual lookup do not search the Spotify catalogue.

Migration works from the bottom of the playlist upward. It inserts a replacement
before its local occurrence, verifies the complete sequence, removes the shifted
local by position, and verifies again. A fresh post-write verification also serves
as the next operation's pre-write check, avoiding duplicate full scans. Reads are
repeated after a cooldown, resume, or a delay of at least 10 seconds.
A temporary private playlist checks
positional removal before the original is changed. Successful checks are reused
for 24 hours for the same account, app, and unchanged job. Delayed snapshot reads
are reconciled against recorded mutation versions and exact track order.

Jobs under `data/<playlist-id>/jobs/<job-id>/` store `original.json`,
`matches.json`, `plan.json`, compatibility-check records, and `migration.json`.
Mutation intent is saved before each write so resume can reconcile interrupted
operations without repeating completed replacements. Search responses are cached
in SQLite. Atomic state writes and process locks protect saved progress;
migration locking requires Linux or macOS.

API requests are paced at least 3 seconds apart by default, with no local daily
request cap. HTTP 429 cooldowns are persisted and waited out automatically,
including 24-hour waits. Short rate limits increase the request interval.
Unexpected playlist changes stop further writes; uncertain mutations are not
automatically replayed.

## Setup

Run from the project directory:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

Create a Spotify Web API app and register this exact redirect URI:

```text
http://127.0.0.1:8765/callback
```

Set `SPOTIFY_CLIENT_ID` in `.env` to the app's Client ID. Authentication uses
PKCE and does not require a Client Secret. Then log in:

```bash
spotify-local-migrate login
```

Use `login --no-browser` to open the printed authorization URL yourself, or
`login --manual` to paste the callback URL on a remote machine. `login --write`
adds playlist modification permissions when you are ready to apply a migration.

Request pacing is configured in `.env`:

```dotenv
SPOTIFY_REQUEST_INTERVAL_SECONDS=3
SPOTIFY_WAIT_FOR_RATE_LIMITS=true
```

Setting `SPOTIFY_WAIT_FOR_RATE_LIMITS=false` makes commands stop on cooldowns
instead of waiting. Process environment variables override `.env`.

For matching overrides, copy `config.example.yaml` to `config.yaml` and edit
artist hints, producer names, thresholds, or weights. `match` and `migrate`
accept `--config PATH` and repeatable `--artist NAME` (`--expected-artist` also
works). Playlist tags are alternatives: any tagged artist can appear as a main
or featured artist. Features explicitly named in a track remain required.

```yaml
playlists:
  spook:  # Playlist name or Spotify playlist ID
    expected_artists: [Corbin]
  "Corbin and Shlohmo":
    expected_artists: [Corbin, Shlohmo]
```

Existing jobs retain their saved configuration. To update artist tags while
keeping completed human reviews, use
`match --job PATH --artist Corbin` (repeat `--artist` for more names).
Review recalculates unattended saved candidates with the current scoring rules,
including previously unmatched tracks, without repeating completed human reviews.

## Usage

Select a playlist, match its local tracks, review the results, and inspect the
plan before applying:

```bash
spotify-local-migrate match
spotify-local-migrate review
spotify-local-migrate migrate --latest --dry-run
spotify-local-migrate login --write
spotify-local-migrate migrate --latest
```

Apply displays the plan and asks for confirmation before writing. Ambiguous
matches can be approved, searched manually, or left unchanged.
During review, press **Enter** to accept the top match or **Escape** to leave the
track unmatched. Local and Spotify metadata appear side by side, with differences
highlighted and a duration delta. Other matches are available on demand;
numbered choices remain available.

If another app has left both local and Spotify copies, preview cleanup first:

```bash
spotify-local-migrate cleanup --dry-run
spotify-local-migrate review --offline --job PATH_PRINTED_ABOVE
spotify-local-migrate cleanup --job PATH_PRINTED_ABOVE
```

`cleanup` accepts `-p PLAYLIST_ID`, `--artist NAME`, and `--config PATH`.
The preview writes no playlist changes and prints the exact job path to review
and apply. Apply asks for confirmation. `--no-review` keeps uncertain pairs;
`cleanup --latest` selects the most recent cleanup job. Interrupted cleanup
continues with `resume --job PATH`. Use a fresh scan after changes made by other
apps; existing migration jobs are not reused as cleanup jobs.

To select another playlist directly:

```bash
spotify-local-migrate playlists
spotify-local-migrate match -p PLAYLIST_ID
```

`-p` accepts a 22-character playlist ID, Spotify playlist URI, or playlist URL.
Playlist list numbers are only accepted at the interactive selection prompt.
The playlist table shows local counts from the latest saved scan, including
matching and cleanup jobs; playlists without a saved scan show `not scanned`.
Use `playlists --count-local` to refresh and save these counts.

`review`, `resume`, and `migrate --latest` use the most recently updated job.
Use `--job PATH` to select a specific saved job:

```bash
spotify-local-migrate status
spotify-local-migrate resume --job data/PLAYLIST_ID/jobs/JOB_ID
spotify-local-migrate review --offline --job data/PLAYLIST_ID/jobs/JOB_ID
```

Keep a running command open to continue automatically after a cooldown. If
interrupted, `resume` continues matching or reconciles migration progress.
For an uncertain write, inspect Spotify before using `--retry-unconfirmed`
(insertion) or `--retry-unconfirmed-delete` (deletion); both require confirmation.

| Command or option | Purpose |
| --- | --- |
| `scan [-p ID] [--raw]` | Capture a playlist and inspect local metadata |
| `scan -p ID --json` | Print the complete capture as JSON |
| `match --from-scan PATH` | Match an existing capture |
| `review --all` | Include automatic and unmatched decisions |
| `review --offline` | Review saved candidates without API calls |
| `migrate --no-review` | Leave ambiguous matches unchanged |
| `cleanup --dry-run` | Preview removal of local copies already represented on Spotify |
| `status` | Show saved jobs, login state, and request usage without API calls |
| `--help` | Show commands and options; also available on each command |

Use `--no-cache` on match/review/migrate/resume to bypass cached search results.
For verbose logs, place verbosity before the command:
`spotify-local-migrate -vv match -p PLAYLIST_ID`.
