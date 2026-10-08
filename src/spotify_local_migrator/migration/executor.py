"""Durable insert -> verify -> positional delete -> verify execution.

The original playlist is never rewritten, nor are local URIs sent to DELETE.
Spotify snapshots merge edits; full sequence comparison is therefore mandatory.
"""

import json
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from ..errors import PlaylistChangedError, RateLimitError, SpotifyAPIError, StateError
from ..matching.search import candidate_from_api
from ..spotify.client import SpotifyClient
from ..workflow import account_id
from .jobs import JobStore, content_hash
from .planner import EntryIdentity, MigrationPlan, identities
from .scanner import PlaylistScanner


class PendingOperation(BaseModel):
    kind: Literal["add", "delete"]
    before: list[EntryIdentity]
    after: list[EntryIdentity]
    snapshot_before: str
    acknowledged_snapshot: str | None = None


class MigrationJournal(BaseModel):
    schema_version: int = 1
    phase: Literal["APPLY", "VERIFY", "COMPLETE"] = "APPLY"
    plan_hash: str
    next_replacement: int = 0
    stage: Literal["ready", "added"] = "ready"
    current: list[EntryIdentity]
    snapshot_id: str
    pending: PendingOperation | None = None
    operations: list[dict] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


@contextmanager
def playlist_lock(store: JobStore):
    # Lock across different jobs for the same playlist, not just one job.
    import fcntl
    import os

    descriptor = os.open(store.directory.parent / ".apply.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StateError("Another migration is applying to this playlist.") from exc
        yield
    finally:
        os.close(descriptor)


def load_plan(store: JobStore) -> MigrationPlan:
    try:
        plan = MigrationPlan.model_validate_json(
            (store.directory / "plan.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError, ValidationError) as exc:
        raise StateError("Cannot read the migration plan. Generate a dry run first.") from exc
    if plan.capture_hash != content_hash(store.capture()) or plan.matches_hash != content_hash(
        store.report()
    ):
        raise StateError("Plan inputs changed. Regenerate the plan before applying.")
    from .planner import build_plan

    rebuilt = build_plan(store.capture(), store.report())
    if (
        rebuilt.requests != plan.requests
        or rebuilt.original != plan.original
        or rebuilt.desired != plan.desired
        or rebuilt.replacements != plan.replacements
        or rebuilt.account_id != plan.account_id
        or rebuilt.snapshot_id != plan.snapshot_id
        or rebuilt.playlist_id != plan.playlist_id
    ):
        raise StateError("Saved plan was altered; do not apply it.")
    return plan


def save_journal(store: JobStore, journal: MigrationJournal) -> None:
    journal.updated_at = datetime.now(UTC)
    store.save("migration.json", journal)


def validate_journal(plan: MigrationPlan, journal: MigrationJournal) -> None:
    if journal.plan_hash != content_hash(plan):
        raise StateError("Migration plan changed after apply started.")
    if not 0 <= journal.next_replacement <= len(plan.replacements):
        raise StateError("Migration progress is invalid.")
    expected = [entry.model_copy() for entry in plan.original]
    for replacement in plan.replacements[: journal.next_replacement]:
        expected[replacement.position] = EntryIdentity(
            uri=replacement.replacement_uri, is_local=False, item_type="track"
        )
    if journal.stage == "added":
        if journal.next_replacement >= len(plan.replacements):
            raise StateError("Invalid final migration stage.")
        replacement = plan.replacements[journal.next_replacement]
        expected.insert(
            replacement.position,
            EntryIdentity(uri=replacement.replacement_uri, is_local=False, item_type="track"),
        )
    if expected != journal.current:
        raise StateError("Migration journal sequence does not match its progress.")
    if journal.pending:
        pending = journal.pending
        if journal.next_replacement >= len(plan.replacements):
            raise StateError("Invalid pending operation.")
        replacement = plan.replacements[journal.next_replacement]
        after = [entry.model_copy() for entry in expected]
        if pending.kind == "add" and journal.stage == "ready":
            after.insert(
                replacement.position,
                EntryIdentity(uri=replacement.replacement_uri, is_local=False, item_type="track"),
            )
        elif pending.kind == "delete" and journal.stage == "added":
            del after[replacement.position + 1]
        else:
            raise StateError("Pending operation does not match the migration stage.")
        if (
            pending.before != expected
            or pending.after != after
            or pending.snapshot_before != journal.snapshot_id
        ):
            raise StateError("Pending operation sequence was altered.")


class MigrationExecutor:
    def __init__(
        self,
        client: SpotifyClient,
        *,
        scanner: PlaylistScanner | None = None,
        progress: Callable[[str], None] | None = None,
    ):
        self.client = client
        self.scanner = scanner or PlaylistScanner(client)
        self.progress = progress or (lambda message: None)

    def _capture(self, playlist_id: str):
        return self.scanner.scan(playlist_id)

    def _probe_write(self, request: Callable[..., Any], *args: Any) -> Any:
        # Only the private compatibility-check playlist is handled here. A 429
        # explicitly rejected the request; uncertain mutations are never retried.
        while True:
            try:
                return request(*args)
            except RateLimitError:
                if not self.client.settings.wait_for_rate_limits:
                    raise
                self.client.wait_for_cooldown()

    def preflight(self, store: JobStore, plan: MigrationPlan) -> None:
        """Prove undocumented positional semantics on catalogue-only duplicates.

        Test [A,B,A] -> [B,A], then repeat the OLD-snapshot deletion. If the
        API ignores positions, deletes all A occurrences, or uses current
        positions for stale snapshots, original-playlist execution is blocked.
        This is a runtime compatibility check, NOT a claim that the incomplete
        2026 reference guarantees this request body.
        """
        uris = list(dict.fromkeys(item.replacement_uri for item in plan.replacements))
        if len(uris) < 2:
            uris.extend(
                entry.uri
                for entry in plan.original
                if not entry.is_local and entry.item_type == "track" and entry.uri not in uris
            )
        # A one-song migration still needs a distinct, playable sentinel.
        if len(uris) < 2:
            response = self.client.search_tracks("track:love")
            for raw in response.get("tracks", {}).get("items", []):
                candidate = candidate_from_api(raw)
                if candidate and candidate.is_playable is True and candidate.uri not in uris:
                    uris.append(candidate.uri)
                    break
        if len(uris) < 2:
            raise StateError(
                "Could not obtain two distinct catalogue tracks for the compatibility check."
            )
        probe = {"phase": "CREATING", "name": "Local migrator API check " + uuid4().hex[:8]}
        store.save("probe.json", probe)
        playlist_id = None
        failure = None
        try:
            self.progress("Checking positional removal on a temporary private playlist...")
            created = self._probe_write(self.client.create_probe_playlist, probe["name"])
            playlist_id = created.get("id")
            from .state import validate_playlist_id

            if not isinstance(playlist_id, str):
                raise StateError("Spotify returned no temporary playlist ID; inspect your library.")
            validate_playlist_id(playlist_id)
            if playlist_id == plan.playlist_id:
                raise StateError("Temporary playlist unexpectedly has the original playlist ID.")
            probe.update(playlist_id=playlist_id, phase="CREATED")
            store.save("probe.json", probe)
            a, b = uris[:2]
            before_snapshot = self._probe_write(self.client.add_tracks, playlist_id, [a, b, a], 0)
            before = self._capture(playlist_id)
            if [entry.uri for entry in before.entries] != [
                a,
                b,
                a,
            ] or before.snapshot_id != before_snapshot:
                raise StateError("Temporary playlist insertion could not be verified.")
            probe.update(phase="TESTING", snapshot_before=before_snapshot)
            store.save("probe.json", probe)
            self._probe_write(self.client.remove_positions, playlist_id, [0], before_snapshot)
            first = self._capture(playlist_id)
            if [entry.uri for entry in first.entries] != [b, a]:
                raise StateError("Spotify did not honor position-only removal.")
            try:
                self._probe_write(self.client.remove_positions, playlist_id, [0], before_snapshot)
            except SpotifyAPIError as exc:
                if (
                    exc.status_code is None
                    or exc.status_code >= 500
                    or exc.status_code in (401, 403, 429)
                ):
                    raise
                # A stale snapshot rejected without a mutation is also safe.
            second = self._capture(playlist_id)
            if [entry.uri for entry in second.entries] != [b, a]:
                raise StateError("Spotify did not safely handle a repeated old-snapshot removal.")

            # Also prove that an old snapshot refers to the old occurrence
            # after an insertion shifts its position. Mere retry deduplication
            # is insufficient to protect a local entry from concurrent edits.
            shift_snapshot = second.snapshot_id
            inserted_snapshot = self._probe_write(self.client.add_tracks, playlist_id, [a], 0)
            inserted = self._capture(playlist_id)
            if [entry.uri for entry in inserted.entries] != [
                a,
                b,
                a,
            ] or inserted.snapshot_id != inserted_snapshot:
                raise StateError("Temporary position-shift insertion could not be verified.")
            stale_rejected = False
            try:
                self._probe_write(self.client.remove_positions, playlist_id, [0], shift_snapshot)
            except SpotifyAPIError as exc:
                if exc.status_code not in (400, 409, 412):
                    raise
                stale_rejected = True
            shifted = self._capture(playlist_id)
            expected_shifted = [a, b, a] if stale_rejected else [a, a]
            if [entry.uri for entry in shifted.entries] != expected_shifted:
                raise StateError(
                    "Spotify applied an old snapshot position to the wrong occurrence."
                )
            probe["checks"] = {
                "single_occurrence": True,
                "stale_retry": True,
                "shifted_position": True,
                "stale_snapshots_rejected": stale_rejected,
            }

            probe.update(phase="PASSED", tested_at=datetime.now(UTC).isoformat())
            store.save("probe.json", probe)
        except (SpotifyAPIError, StateError, PlaylistChangedError) as exc:
            failure = exc
            probe.update(phase="FAILED", reason=str(exc))
            store.save("probe.json", probe)
        finally:
            if playlist_id and playlist_id != plan.playlist_id:
                try:
                    self._probe_write(self.client.unfollow_probe_playlist, playlist_id)
                    probe["removed_from_library"] = True
                except SpotifyAPIError:
                    probe["removed_from_library"] = False
                    self.progress(
                        "Temporary test playlist could not be removed from your library: "
                        f"{playlist_id}"
                    )
                store.save("probe.json", probe)
        if failure is not None:
            raise StateError(
                "Position-removal compatibility check failed. Original playlist untouched. "
                f"{failure} See probe.json; Spotify's current DELETE schema is incomplete."
            ) from failure

    def apply(self, store: JobStore, *, retry_unconfirmed_add: bool = False) -> MigrationJournal:
        while True:
            try:
                return self._apply_once(store, retry_unconfirmed_add=retry_unconfirmed_add)
            except RateLimitError:
                if not self.client.settings.wait_for_rate_limits:
                    raise
                self.client.wait_for_cooldown()
                # Reconcile the journal and read the current ordered playlist
                # again. Never replay an original-playlist write after a long
                # cooldown using a position validated before the wait.

    def _apply_once(self, store: JobStore, *, retry_unconfirmed_add: bool) -> MigrationJournal:
        with store.lock(), playlist_lock(store):
            plan = load_plan(store)
            if account_id(self.client) != plan.account_id:
                raise StateError("Migration belongs to another Spotify account.")
            journal_path = store.directory / "migration.json"
            if journal_path.exists():
                try:
                    journal = MigrationJournal.model_validate_json(
                        journal_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError, ValidationError) as exc:
                    raise StateError(
                        "Cannot read migration journal. Do not restart from a new job."
                    ) from exc
                validate_journal(plan, journal)
            else:
                journal = MigrationJournal(
                    plan_hash=content_hash(plan),
                    current=plan.original,
                    snapshot_id=plan.snapshot_id,
                )
            current = self._capture(plan.playlist_id)
            if not journal.pending and (
                identities(current) != journal.current or current.snapshot_id != journal.snapshot_id
            ):
                raise PlaylistChangedError(
                    "Playlist changed since the saved plan/progress. Stop; inspect the saved state."
                )
            if not plan.replacements:
                journal.phase = "COMPLETE"
                save_journal(store, journal)
                return journal
            self.client.auth.require_write_scopes()
            if not journal_path.exists():
                # No original-playlist mutations or journal exist until the
                # isolated positional compatibility check passes.
                self.preflight(store, plan)
                current = self._capture(plan.playlist_id)
                if identities(current) != plan.original or current.snapshot_id != plan.snapshot_id:
                    raise PlaylistChangedError(
                        "Playlist changed during compatibility check; start a new plan."
                    )
                save_journal(store, journal)
            else:
                try:
                    probe = json.loads((store.directory / "probe.json").read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise StateError("Missing compatibility-check record; stop migration.") from exc
                if probe.get("phase") != "PASSED" or not all(
                    probe.get("checks", {}).get(check)
                    for check in ("single_occurrence", "stale_retry", "shifted_position")
                ):
                    raise StateError("No successful positional compatibility check is recorded.")

            while True:
                validate_journal(plan, journal)
                current = self._capture(plan.playlist_id)
                cooldown_generation = self.client.cooldown_generation
                actual = identities(current)
                pending = journal.pending
                if pending:
                    if actual == pending.after:
                        if (
                            current.snapshot_id == pending.snapshot_before
                            or pending.acknowledged_snapshot is not None
                            and current.snapshot_id != pending.acknowledged_snapshot
                        ):
                            raise PlaylistChangedError(
                                "Unexpected snapshot after mutation; inspect the playlist."
                            )
                        journal.operations.append(
                            {
                                "kind": pending.kind,
                                "replacement_index": journal.next_replacement,
                                "position": plan.replacements[journal.next_replacement].position
                                + (1 if pending.kind == "delete" else 0),
                                "snapshot_before": pending.snapshot_before,
                                "snapshot_after": current.snapshot_id,
                                "verified_at": datetime.now(UTC).isoformat(),
                            }
                        )
                        journal.current = pending.after
                        journal.snapshot_id = current.snapshot_id
                        journal.pending = None
                        if pending.kind == "add":
                            journal.stage = "added"
                        else:
                            journal.stage = "ready"
                            journal.next_replacement += 1
                            self.progress(
                                f"Verified replacement {journal.next_replacement}"
                                f"/{len(plan.replacements)}"
                            )
                        save_journal(store, journal)
                        continue
                    if actual != pending.before or current.snapshot_id != pending.snapshot_before:
                        raise PlaylistChangedError(
                            "Playlist differs from both expected mutation outcomes. "
                            "No further writes."
                        )
                    if pending.acknowledged_snapshot is not None:
                        raise StateError(
                            "Spotify acknowledged the mutation but readback is unchanged. "
                            "Retry resume later."
                        )
                    if pending.kind == "add":
                        if not retry_unconfirmed_add:
                            raise StateError(
                                "Insertion outcome is unconfirmed and the playlist still "
                                "shows the pre-add state. "
                                "No duplicate was added. Wait, inspect Spotify, then resume; "
                                "--retry-unconfirmed requires explicit confirmation "
                                "to retry this insertion."
                            )
                        retry_unconfirmed_add = False
                    # Pending DELETE retry is safe only against the exact OLD
                    # snapshot; preflight verified stale retry semantics.
                    self._dispatch(store, plan, journal)
                    continue

                if actual != journal.current or current.snapshot_id != journal.snapshot_id:
                    raise PlaylistChangedError("Playlist changed externally; no further writes.")
                if journal.next_replacement == len(plan.replacements):
                    journal.phase = "VERIFY"
                    save_journal(store, journal)
                    if actual != plan.desired:
                        raise StateError("Final playlist does not equal the desired sequence.")
                    journal.phase = "COMPLETE"
                    save_journal(store, journal)
                    return journal
                replacement = plan.replacements[journal.next_replacement]
                after = [entry.model_copy() for entry in journal.current]
                if journal.stage == "ready":
                    candidate = candidate_from_api(self.client.track(replacement.spotify_id))
                    if self.client.cooldown_generation != cooldown_generation:
                        # A track lookup can also pause for hours. Discard this
                        # pre-wait playlist verification and scan again.
                        continue
                    if (
                        candidate is None
                        or candidate.is_playable is not True
                        or candidate.uri != replacement.replacement_uri
                    ):
                        raise StateError(
                            "Replacement is no longer available; local occurrence left unchanged."
                        )
                    after.insert(
                        replacement.position,
                        EntryIdentity(
                            uri=replacement.replacement_uri, is_local=False, item_type="track"
                        ),
                    )
                    kind = "add"
                else:
                    local_position = replacement.position + 1
                    if (
                        not after[local_position].is_local
                        or after[local_position].uri != replacement.local_uri
                        or after[replacement.position].uri != replacement.replacement_uri
                    ):
                        raise StateError(
                            "Local entry and verified replacement are not at expected positions."
                        )
                    del after[local_position]
                    kind = "delete"
                journal.pending = PendingOperation(
                    kind=kind,
                    before=journal.current,
                    after=after,
                    snapshot_before=journal.snapshot_id,
                )
                save_journal(store, journal)  # Durable intent BEFORE dispatch.
                self._dispatch(store, plan, journal)

    def _dispatch(self, store: JobStore, plan: MigrationPlan, journal: MigrationJournal) -> None:
        pending = journal.pending
        assert pending is not None
        replacement = plan.replacements[journal.next_replacement]
        try:
            if pending.kind == "add":
                snapshot = self.client.add_tracks(
                    plan.playlist_id, [replacement.replacement_uri], replacement.position
                )
            else:
                snapshot = self.client.remove_positions(
                    plan.playlist_id, [replacement.position + 1], pending.snapshot_before
                )
        except SpotifyAPIError as exc:
            if exc.status_code is not None and 400 <= exc.status_code < 500:
                # An explicit rejection has not committed this request. A
                # transport failure/5xx/malformed successful response remains
                # uncertain and keeps its durable intent for reconciliation.
                journal.pending = None
                save_journal(store, journal)
            raise
        pending.acknowledged_snapshot = snapshot
        save_journal(store, journal)  # Then read back before any further write.
