# Spotify Local Track Migrator

Python 3.12+ CLI for replacing local-file occurrences with Spotify catalogue
tracks. Search/review/dry-run are read-only. Apply requires write authorization,
a displayed plan with default-No confirmation, and an isolated compatibility
check before it touches your original playlist.

## Install and connect

Run from this project directory:

~~~bash
cd ~/dev/spotify-convert
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
~~~

If already installed, activate your existing environment; keep the existing
.env. Put the Spotify **Client ID** in SPOTIFY_CLIENT_ID. PKCE needs no Client
Secret. Git ignores .env, tokens, captures, cache and migration state.

In the developer dashboard choose **Web API**, a name such as **Local Track
Migrator** (the name cannot start with “Spot”), and a description such as
“Match local playlist files to Spotify catalogue tracks.” Website is optional.
Register this exact redirect:

~~~text
http://127.0.0.1:8765/callback
~~~

Development apps require the applicable Premium/allowlisted-user conditions.
Authorize on your own machine:

~~~bash
spotify-local-migrate login
spotify-local-migrate playlists
spotify-local-migrate scan --raw
~~~

Use `login --no-browser` to open the printed URL yourself, or `login --manual`
to paste the callback URL into the terminal's hidden prompt on a remote machine.
Keep callback codes, tokens and secrets out of chat.

Read scopes: `playlist-read-private`, `playlist-read-collaborative`.
`login --write` additionally requests `playlist-modify-private` and
`playlist-modify-public`. It uses the same redirect.

## Your dxrt scan

There are 344 local occurrences, with uploader/artist prefixes, production
brackets, exclusives, video credits and features embedded in titles. Album
metadata is absent; 24 durations are missing. Artist tags often identify
uploaders/producers. The cleaner learns the dominant artist and explicitly
labeled producers, extracts collaborations/features from titles, and retains
all original metadata and removed annotations in the report.

| Local metadata | Prepared title / artists |
| --- | --- |
| `2sdxrt3all - oh (prod. whyceg) [djslimebxll exclusive]` | `oh` / `2sdxrt3all` |
| `v8gve - 2sdxrt3all - cut off my hand [whyceg] ...exclusive...` | `cut off my hand` / `2sdxrt3all` |
| `2sdxrt3all feat. 2sroccet x 3hard - steppin (Man of da year)` | `steppin (Man of da year)` / all three artists |
| `2sdxrt3all - push (@wizardpem @whyceg)` | `push` / `2sdxrt3all` |

Unknown annotations are retained. Conflicting versions prevent automatic
selection. “Long Live Twxn” remains a title. Missing artist/duration evidence
also disables automatic selection, and unrelated artists are left unmatched.

Real read-only matching on 2026-10-07 saved **188/344** decisions:
**103 automatic, 15 needing review, 70 unmatched**. Spotify then returned
Retry-After **85404 seconds**. The remaining matching is unfinished. No playlist
was modified. The cooldown is persisted; rerunning commands will not resend
API requests until it expires.

Saved job:

~~~text
data/6ZIuyhjbuRSbldrD0kJm7m/jobs/20261007T190836Z-4d06c66b
~~~

During the cooldown, inspect/review saved candidates without API calls:

~~~bash
spotify-local-migrate status
spotify-local-migrate review --offline
~~~

After the cooldown, complete matching, review, and inspect the full plan:

~~~bash
spotify-local-migrate resume
spotify-local-migrate review
spotify-local-migrate migrate --latest --dry-run
~~~

When you approve that plan:

~~~bash
spotify-local-migrate login --write
spotify-local-migrate migrate --latest
~~~

Unqualified review/resume and `--latest` select the most recently updated job.
For several playlists, use `--job data/<id>/jobs/<job>`. Apply requires complete
matching. `review --all` also includes automatic/unmatched decisions.

## Commands

| Command | Behavior |
| --- | --- |
| no subcommand | Select and scan a playlist |
| `login [--write]` | PKCE login; write scopes are opt-in |
| `playlists [--count-local]` | List playlists and optionally count local tracks |
| `scan [-p ID] [--raw / --json]` | Save stable metadata and every ordered occurrence |
| `match [-p ID]` | Fresh scan, searches, ranked candidates and checkpoints |
| `match --from-scan PATH` | Match a saved scan |
| `match --job PATH` | Resume matching |
| `review [--job PATH] [--all] [--offline]` | Approve, reject, inspect scores, search manually |
| `migrate [--latest / --job PATH / -p ID] --dry-run` | Save a complete operation plan; no mutations |
| `migrate [--latest / --job PATH / -p ID]` | Match/review/plan, confirm, check compatibility, apply |
| `resume [--job PATH / -p ID]` | Resume matching or reconcile interrupted apply |
| `status` | Inspect cached login, scans and jobs locally |

`--no-review` leaves ambiguous decisions unchanged. A completed saved job with
`migrate --latest --dry-run --no-review` needs no network. `--no-cache` disables
search response reuse but never bypasses a cooldown. Verbosity precedes the
command: `spotify-local-migrate -vv match -p ID`.

## Matching settings

Copy `config.example.yaml` to `config.yaml` for explicit playlist artist hints,
additional producer names, thresholds or weights. Use `--config PATH` or repeat
`--expected-artist NAME` with match/migrate. Existing jobs retain their original
configuration.

Default weights: title 50%, artist 30%, duration 15%, album 5%. Missing fields
redistribute weight, with conservative automatic-selection gates. Duration is
essentially identical within 2 seconds, strong within 5, progressively worse
at 5–30, and heavily penalized beyond 30. Scores are heuristics, not probabilities.

Defaults: automatic >=0.90, review >=0.70, with a >=0.05 margin over other
recordings. Automatic selection also needs strong title/all artist evidence,
account playability, duration within 10 seconds, and consistent versions.
Album variants count as the same recording only with matching ISRC,
title/versions, artists, explicit flag and near-identical duration.

Queries broaden from title+artist filters to free text and title-only searches.
All query variants are gathered before ranking ambiguity. SQLite cache keys
include account, market, query and page.

## Apply and recovery

Replacements preserve each original occurrence, including intentional duplicates.
The plan explains existing catalogue occurrences and repeated replacements.
Nothing is silently deduplicated.

Work from bottom to top. Insert a catalogue track directly before the local
occurrence, persist its returned snapshot, verify the whole playlist, then remove
that exact shifted local occurrence using position and verified snapshot. Every
mutation has a saved intent before dispatch and verification afterwards.
Unmatched locals, episodes and null/unavailable entries retain order.

**Spotify's positional removal schema is incomplete.** The playlist guide
requires index + snapshot for locals, while the current DELETE reference
documents URI-based items. The inferred position-only payload is tested on a
new private catalogue-only playlist first: single-occurrence removal, duplicate
preservation, stale retries, and positions shifted after a snapshot. Failure
stops before any original-playlist mutation. There is no local-URI deletion or
whole-playlist rewrite fallback. Live local removal has not yet been validated
with your write-authorized account.

The test playlist is removed from your library after the check. Cleanup failure
is reported and saved in `probe.json`. A lost creation response can leave a
temporary playlist without a returned ID; locate it by its saved “Local migrator
API check …” name.

After an interrupted apply:

~~~bash
spotify-local-migrate resume
~~~

Resume compares actual ordered state with saved before/after states and
reconciles committed requests. It never blindly repeats an unconfirmed insertion.
If that insertion still appears absent, stop, wait and inspect Spotify.
`resume --retry-unconfirmed` then offers another default-No confirmation. An
earlier request might still commit later, creating an extra occurrence.

Unexpected external edits stop further writes. Avoid concurrent editing while
applying: snapshots are not a global transaction lock. JSON captures support
inspection/reconciliation, but cannot restore deleted locals through the API.
Keep the original audio and job files until satisfied with verification.

## Project and validation

~~~text
spotify-convert/
├── README.md, DESIGN.md, pyproject.toml, config.example.yaml
├── src/spotify_local_migrator/
│   ├── cli.py, config.py, models.py, workflow.py
│   ├── spotify/       # PKCE, 2026 API, retries, durable cooldown
│   ├── matching/      # cleanup, search cache, scoring, checkpoints
│   ├── migration/     # scanner, jobs, planner, executor, atomic state
│   └── ui/            # inspection and review
└── tests/
~~~

Each job has `original.json`, `matches.json`, `plan.json`, then `probe.json` and
`migration.json` when applying. Captures also remain under `data/<id>/scans/`;
the first `data/<id>/original.json` is never overwritten. Files are atomically
written/fsynced with mode 0600. Back up whole job directories. Apply locking
currently requires POSIX (Linux/macOS).

~~~bash
python -m pytest -q
ruff check .
ruff format --check .
python -m pip check
~~~

Tests mock Spotify, including real metadata formats, version conflicts,
confidence, missing evidence, pagination, duplicates, positional snapshots,
OAuth refresh, 429 cooldowns, crashes, unknown outcomes, external edits, process
locks and default-No confirmation.

See [DESIGN.md](DESIGN.md) for API sources, algorithm rationale and live
validation limits.
