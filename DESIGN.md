# Spotify Local Track Migrator: design

Official documentation checked on 2026-10-07. Real-account scanner validation
covered the 344-item dxrt playlist. Real read-only matching checkpointed 188
occurrences before a 429 cooldown. This fulfilled the scanner/matcher gate
before mutation code was implemented. No live playlist mutations have occurred
during development of the scanner/matcher. Subsequent live catalogue-only
compatibility tests verified single and nonzero current-position removal;
original-playlist local removal remains subject to runtime verification.

## 2026 API contracts

- Use `/playlists/{id}/items`, wrapped `item` and playlist `items`. Accept legacy
  response names only as parser fallbacks. Item access requires ownership or
  collaboration. Keep every paginated item type, null and duplicate.
  [Migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide),
  [items](https://developer.spotify.com/documentation/web-api/reference/get-playlists-items).
- Local fields can be missing/null; preserve raw metadata and labeled URI
  fallbacks. Local entries cannot be added through the API. Spotify's playlist
  guide calls for local removal by index and snapshot.
  [Playlist concepts](https://developer.spotify.com/documentation/web-api/concepts/playlists).
- **Unresolved removal contract:** DELETE `/playlists/{id}/items` documents
  URI-based `items` but omits the position-only local-removal schema. The adapter
  body `{items: [], positions: [p], snapshot_id: s}` is an inference, not a
  confirmed contract. Isolated runtime tests must pass before original writes.
  [DELETE reference](https://developer.spotify.com/documentation/web-api/reference/remove-items-playlist).
- Insert catalogue URIs with POST `/playlists/{id}/items`, zero-based position,
  and returned snapshot. Insertion has no conditional snapshot parameter.
  Snapshots version and merge edits rather than locking the whole playlist.
  [Insertion](https://developer.spotify.com/documentation/web-api/reference/add-items-to-playlist).
- Search development-mode pages contain at most 10 tracks; account country
  takes precedence over market.
  [Search](https://developer.spotify.com/documentation/web-api/reference/search).
- Create temporary private playlists with POST `/me/playlists`. Remove their
  library entry via DELETE `/me/library?uris=spotify:playlist:…`, accepting the
  documented empty successful response.
  [Create](https://developer.spotify.com/documentation/web-api/reference/create-playlist),
  [library removal](https://developer.spotify.com/documentation/web-api/reference/remove-library-items).
- PKCE uses S256, random validated state, exact callback validation and refresh;
  no Client Secret. Redirect: `http://127.0.0.1:8765/callback`. Read scopes:
  `playlist-read-private` and `playlist-read-collaborative`. `login --write`
  adds both playlist modification scopes. Verify scopes actually granted.
  [PKCE](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow),
  [redirects](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri),
  [scopes](https://developer.spotify.com/documentation/web-api/concepts/scopes).
- Prefer stable `account_id` over legacy `id` for job/cache ownership. Space all
  Web API attempts by 3 seconds, with persistent backoff for short burst limits
  and usage statistics without a local daily cap. Persist every Retry-After and
  automatically wait and continue, including day-long cooldowns. When no usable
  header is supplied, label the increasing retry backoff as estimated rather
  than presenting it as a Spotify reset time. Revalidate playlist state before
  retrying a rejected original-playlist write; never retry uncertain mutations.
  Development quotas may be shared across apps.
  [May](https://developer.spotify.com/documentation/web-api/references/changes/may-2026),
  [July](https://developer.spotify.com/documentation/web-api/references/changes/july-2026),
  [rate limits](https://developer.spotify.com/documentation/web-api/concepts/rate-limits).

## Capture, match, review, plan

SCAN → MATCH → REVIEW → PLAN → APPLY → VERIFY.

Scanning requires equal before/after snapshots and complete pagination totals.
Positions include nulls/episodes. Capture raw metadata/pages, parsed ordered
entries, URI fallback provenance and snapshot/time. Publish the first baseline
once and version later captures. Writes are atomic/fsynced, mode 0600.

Matching binds an immutable job capture to an account. Cache by account,
market, query and page; checkpoint each fully searched occurrence. Incomplete
queries never become completed decisions.

The real format embeds artists and uploader/producer credits in titles. Learn
the dominant repeated artist and explicitly labeled production names across
the playlist; explicit configuration overrides artist inference. Known artist
anchors split uploader/artist/title prefixes. Remove explicit production,
exclusive, uploader-handle, director and video annotations. Keep unknown title
annotations and version qualifiers. Report removed text, ignored tags and
warnings alongside original metadata.

Normalize NFKC/case/whitespace/apostrophes/punctuation/& and filename artifacts.
Compare title, every relevant artist, duration and available album with
configurable weights. Missing artist/duration evidence disables automatic
selection; wrong-artist evidence caps default confidence below review.
Qualifier conflicts have penalties and forbid auto-selection. Automatic
decisions require threshold, margin over different recordings, strong
title/all artist evidence, availability and duration agreement. Scores are
heuristics, not probabilities.

Group equivalent album releases by shared ISRC, normalized title/version, artists,
content rating and duration within 2 seconds. Known clean/explicit counterparts
may have separate ISRCs if the remaining evidence agrees. Require pairwise
agreement within a group so one counterpart cannot bridge distinct recordings.
Choose a playable explicit release first, then the earliest Spotify search result
among releases with the same rating. Persist first-seen search order; older jobs
fall back to their saved order. Keep every release as evidence, show one preferred
choice per group, and retain the original confidence gates. Refresh unattended
automatic choices offline; preserve human choices and lock decisions after apply.

Reviews persist each decision and manual-search provenance. Offline review can
work on partial jobs during a cooldown. Planning still requires decisions for
every local occurrence. Plans bind capture/report hashes and contain exact
original/desired sequences, descending replacements and duplicate warnings.
Unselected occurrences remain local.

## Isolated compatibility gate

Before the first original-playlist write, after explicit default-No confirmation:

1. Create a private playlist, add distinct playable catalogue tracks `[A,B,A]`,
   and verify exact order; record both response and readback snapshots.
2. Use the inferred position-only DELETE at 0 against its snapshot. Require
   `[B,A]`; detect no-ops and URI-wide duplicate deletion.
3. Insert A at 0, verify `[A,B,A]`, and delete current position 1 against the
   insertion response snapshot. Require `[A,A]`, proving nonzero-position removal.
4. Save the `verified_current_positions` strategy, capabilities and snapshot
   read reliability, then remove the temporary playlist from the user's library.

Do not require idempotent stale-snapshot removal: the live endpoint has been
observed applying a repeated DELETE to the current index and deleting the next
track. Migration therefore sends each delete once, compares complete sequences
before/after, and never automatically replays an uncertain deletion. A stale
read snapshot is not a version anchor: journal mutation response snapshots
separately, use the last one for deletion when available, and retain observed read
snapshots for external-change checks. If the probe reports unreliable reads,
accept snapshot catch-up or cache regression only to a version recorded in this
original playlist's journal, alongside an exact full sequence match. Load that
policy before validating progress on resume. Refresh the observed read version
without changing completed operations or the last mutation version. Apply the
same policy to an unchanged pending-before state, retaining the prohibition on
automatic replay. Unknown versions and changed sequences still stop execution.
If the probe reports reliable snapshots, require exact saved-version and mutation
acknowledgment/readback agreement.

Persist successful proof in a separate `compatibility.json`, binding account,
Client ID, capture/matches hashes, strategy version and UTC test time. Reuse
for at most 24 hours before a new apply; started jobs may resume using their
bound proof after expiry. Never overwrite successful proof on a failed attempt.
Preserve the latest attempt and separate cleanup errors in `probe.json`. Surface
only sanitized structured API error message/reason fields; discard other body
fields and redact credentials/URLs. A 403 is a denial, not a presumed cooldown.

Failure leaves the original untouched. This proves catalogue positional
semantics, not local-file support. A later rejected local removal leaves its
already verified replacement alongside the retained local and can be resumed.
No live local-removal guarantee is claimed until observed.

Creation is journaled by name before dispatch because a lost response can leave
a test playlist whose ID was not returned. Cleanup failure records the ID and
is displayed. Unsupported removal has no URI or rewrite fallback.

## Occurrence-preserving apply

Never replace whole contents: unmatched locals cannot be re-added. Never
remove by local URI or deduplicate an existing catalogue occurrence.

For original positions p, descending:

1. Read a stable current sequence and compare both sequence and snapshot with
   progress. Stop on unexplained changes. Revalidate candidate availability.
2. Save insertion intent with exact before/after ordered sequences and snapshot.
   Insert one catalogue occurrence at p.
3. Persist returned snapshot; verify the complete post-insert sequence.
   The local occurrence has shifted to p+1.
4. Check replacement at p and original local at p+1. Save removal intent, then
   delete position p+1 against the verified post-insert snapshot.
5. Persist response and verify the complete post-remove sequence. Only then
   mark the pair finished.

Each pair leaves length unchanged and lower original positions stable.
Duplicates remain distinct occurrences; unmatched locals/nulls/episodes keep
order. An existing catalogue URI elsewhere is not evidence of this replacement.

External editors can race between reads/writes: Spotify has no global lock or
conditional insertion. Snapshot merging/rejection plus full readback mitigate
this; unexpected edits stop execution. Avoid concurrent edits during apply.

## Durable resume

Save intent before dispatch, snapshot after acknowledged success, verification
after readback. Network/5xx/malformed successful responses retain uncertain
intent; do not blindly repeat additions. Explicit 4xx rejection clears that
request's intent for later retry after resolving access/rate limits.

Resume validates account, capture/report/plan hashes and journal projections,
then compares actual ordered state with exact pending before/after sequences.
A consistent after state reconciles a committed request even after a crash
before local acknowledgement. An uncertain operation still showing the before
state stops without replay. Explicit additional default-No confirmation is
required to retry an absent insertion (`--retry-unconfirmed`) or an unchanged
deletion (`--retry-unconfirmed-delete`), after waiting and inspecting that it did
not commit. A late request could otherwise add a duplicate or delete another
occurrence. Other changes halt further writes.

POSIX locks cover the job and all apply jobs for one playlist. Recompute
journal expectations from the plan to detect invalid progress/pending state.
Compare the complete desired order/count before marking COMPLETE. Completed
resume sends no further mutation requests.

Captures provide inspection and reconciliation evidence; the API cannot
restore removed local files, so this is not automatic rollback.

## Validation status

Mocked API and snapshot-emulator tests cover formats, scoring, review,
pagination, duplicates, confidence, missing evidence, default-No/dry-run,
OAuth refresh, 429 cooldowns, unsupported/shifted positional behavior,
lost responses, crashes, external edits and journal tampering.

Real read-only results: 344 locals scanned; 188 matching decisions saved,
103 automatic, 15 review, 70 unmatched. Retry-After was 85404 seconds; remaining
searches must resume after the saved cooldown. Write authorization, the live
compatibility check and actual local replacement have not yet been exercised
against the user's account.
