"""SQLite-backed persistent state store for incremental sync (P1).

Scope: workflow state only. This module knows nothing about collectors,
browser lifecycle, or download orchestration, and it never touches the
filesystem under data/ — verifying filesystem artifacts is sync.py's job.

Responsibility split (docs/P1_STATE_CONTRACT_V2.md §1):
- state.py owns workflow state: discovery, lifecycle, budgets, errors.
- sync.py owns filesystem integrity verification (raw.json existence,
  media bytes, the .complete seal) and every state<->disk guard.

Authority rules (docs/P1_STATE_CONTRACT_V2.md §1, P1_REVIEW_SONNET.md
Risk 1/2):

- ``data/<note_id>/raw.json`` on disk is the source of truth for the note
  detail payload and media URLs. ``media_state`` stores *execution* state
  only: its ``url`` field is provenance captured from normalizer output at
  build time and MUST NOT be treated as authoritative or assumed unexpired.
- The skip-if-verified lookup key for media entries is ``filename``, which
  must exactly equal the basename of the target path
  (``assets_dir / item.filename``). No fuzzy or index-based matching.
- ``FETCHING`` and ``MEDIA_SYNCING`` are crash traces, never legal resting
  states; ``recover_interrupted()`` resets them at startup.

Queue contract (docs/P1_STATE_CONTRACT_V2.md §3): work is derived from
status via two disjoint SQL projections — get_fetch_queue() and
get_media_resume_queue() — which together cover all non-terminal work.
Neither queue alone is "all pending work".

Invariants enforced by this module (P1_STATE_CODE_REVIEW_CODEX.md B1–B5):

- Startup fail-closed: schema version must match exactly (no migration by
  design), expected columns must exist, and every stored status must be a
  known NoteStatus. Old, newer, or tampered databases raise at open time.
- Budget invariant: a failure commit atomically decides RETRYABLE_FAILED vs
  FINAL_FAILED; a RETRYABLE_FAILED row with attempt_count >=
  DEFAULT_MAX_ATTEMPTS can never exist, and crash recovery never restores
  an exhausted FETCHING to a runnable state.
- FINAL_FAILED exits only through retry_failed(), never transition().
- Media results are recorded only while MEDIA_SYNCING.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from .errors import AcquisitionStatus
from .models import FavoriteRef, get_current_iso_time

DEFAULT_STATE_DB = Path(".xhs-state/sync.db")
# v2: added notes.last_failure_stage (docs/P1_STATE_CONTRACT_V2.md §4).
SCHEMA_VERSION = "2"

# P1_REVIEW_SONNET.md Risk 3: 5 absorbs 1-2 environment-crash attempts on top
# of genuine failures before a note falls to FINAL_FAILED.
DEFAULT_MAX_ATTEMPTS = 5

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notes (
    note_id                 TEXT PRIMARY KEY,
    status                  TEXT NOT NULL,
    xsec_token              TEXT,
    source_url              TEXT,
    title                   TEXT,
    attempt_count           INTEGER NOT NULL DEFAULT 0,
    last_error_status       TEXT,
    last_error_message      TEXT,
    last_failure_stage      TEXT,
    media_state             TEXT,
    created_at              TEXT NOT NULL,
    last_seen_in_listing_at TEXT,
    updated_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_EXPECTED_NOTE_COLUMNS = frozenset({
    "note_id", "status", "xsec_token", "source_url", "title", "attempt_count",
    "last_error_status", "last_error_message", "last_failure_stage",
    "media_state", "created_at", "last_seen_in_listing_at", "updated_at",
})


class NoteStatus(str, Enum):
    """Note-level lifecycle statuses.

    DISCOVERED  seen in favorites enumeration, not yet admitted to work queue
    PENDING     queued, waiting for a fetch slot
    FETCHING    detail fetch in flight (crash trace only)
    DETAIL_SUCCESS  raw.json durably written; media phase not finished
    MEDIA_SYNCING   media phase in flight (crash trace only)
    MEDIA_PARTIAL   detail intact, some media failed or missing
    COMPLETE    all artifacts written and verified
    RETRYABLE_FAILED  failed with a retryable error class
    FINAL_FAILED terminal: attempts exhausted or non-retryable error class
    """

    DISCOVERED = "DISCOVERED"
    PENDING = "PENDING"
    FETCHING = "FETCHING"
    DETAIL_SUCCESS = "DETAIL_SUCCESS"
    MEDIA_SYNCING = "MEDIA_SYNCING"
    MEDIA_PARTIAL = "MEDIA_PARTIAL"
    COMPLETE = "COMPLETE"
    RETRYABLE_FAILED = "RETRYABLE_FAILED"
    FINAL_FAILED = "FINAL_FAILED"


# Legal state transitions. Anything else is a sync-runner bug and fails closed.
TRANSITIONS: dict[NoteStatus, frozenset[NoteStatus]] = {
    NoteStatus.DISCOVERED: frozenset({NoteStatus.PENDING}),
    NoteStatus.PENDING: frozenset({NoteStatus.FETCHING}),
    # PENDING here = startup recovery from crash mid-fetch (attempt already counted).
    NoteStatus.FETCHING: frozenset(
        {NoteStatus.DETAIL_SUCCESS, NoteStatus.RETRYABLE_FAILED, NoteStatus.FINAL_FAILED, NoteStatus.PENDING}
    ),
    # PENDING here = demotion guard when raw.json is missing on disk (review Risk 2).
    # COMPLETE covers zero-media notes that skip the media phase.
    NoteStatus.DETAIL_SUCCESS: frozenset({NoteStatus.MEDIA_SYNCING, NoteStatus.COMPLETE, NoteStatus.PENDING}),
    # DETAIL_SUCCESS here = startup recovery from crash mid-media-phase.
    # RETRYABLE_FAILED covers URL_EXPIRED / no-progress escalation; when the
    # fetch budget is exhausted, transition() coerces the failure commit to
    # FINAL_FAILED even though this per-edge listing does not show it — the
    # budget is a global invariant that supersedes edge enumeration.
    NoteStatus.MEDIA_SYNCING: frozenset(
        {NoteStatus.COMPLETE, NoteStatus.MEDIA_PARTIAL, NoteStatus.RETRYABLE_FAILED, NoteStatus.DETAIL_SUCCESS}
    ),
    NoteStatus.MEDIA_PARTIAL: frozenset({NoteStatus.MEDIA_SYNCING, NoteStatus.RETRYABLE_FAILED, NoteStatus.PENDING}),
    # PENDING here = demotion guard when committed artifacts are missing on disk.
    NoteStatus.COMPLETE: frozenset({NoteStatus.PENDING}),
    NoteStatus.RETRYABLE_FAILED: frozenset({NoteStatus.FETCHING, NoteStatus.PENDING}),
    # FINAL_FAILED is closed: the only exit is retry_failed()'s dedicated
    # atomic reset (Codex review B3). Budget coercion may still *enter* it
    # from any failure-legal source (see transition()).
    NoteStatus.FINAL_FAILED: frozenset(),
}

# Per-file media execution statuses (aligned with MediaItem.download_status).
_MEDIA_FILE_STATUSES = frozenset({"PENDING", "COMPLETED", "FAILED"})

# Targets that may carry a note-level error record.
_ERROR_TARGETS = frozenset({NoteStatus.RETRYABLE_FAILED, NoteStatus.FINAL_FAILED, NoteStatus.MEDIA_PARTIAL})

# Pipeline stages a failure can be attributed to. attempt_count is a single
# fetch budget (never split into detail/media counters); the stage makes the
# budget explainable — e.g. MEDIA no-progress escalations consume fetch
# attempts too, and without the stage that is invisible (contract v2 §4).
_FAILURE_STAGES = frozenset({"FETCH", "NORMALIZE", "MEDIA"})

# Failure targets that must always carry a normalized error_status.
_RETRY_TERMINAL_TARGETS = frozenset({NoteStatus.RETRYABLE_FAILED, NoteStatus.FINAL_FAILED})

# Source statuses from which a MEDIA-stage failure can legitimately originate
# (media work only happens while MEDIA_SYNCING / resting in MEDIA_PARTIAL).
_MEDIA_STAGE_SOURCES = frozenset({NoteStatus.MEDIA_SYNCING, NoteStatus.MEDIA_PARTIAL})


@dataclass
class NoteState:
    """Row mapping for one note. media_state is the parsed entry list."""

    note_id: str
    status: NoteStatus
    attempt_count: int
    created_at: str
    updated_at: str
    xsec_token: str | None = None
    source_url: str | None = None
    title: str | None = None
    last_error_status: str | None = None
    last_error_message: str | None = None
    last_failure_stage: str | None = None
    media_state: list[dict[str, Any]] | None = None
    last_seen_in_listing_at: str | None = None


class StateStore:
    """Durable note/sync state. Single-connection, single-writer, NOT thread-safe.

    Callers must ensure all access from a single thread/process: this class
    holds one connection and uses no thread locks (docs/P1_STATE_CONTRACT_V2.md
    §6). A future concurrent-download optimization must pass results back to
    the main thread for recording — worker threads never mutate state.

    Every public mutation is one committed transaction, so a crash at any
    moment leaves the database consistent with work actually done.

    This store manages workflow state only. It does not verify filesystem
    artifacts (raw.json, media bytes, .complete); those guards live in
    sync.py per the responsibility split in the module docstring.
    """

    def __init__(self, db_path: str | Path = DEFAULT_STATE_DB) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        # FULL + WAL: a committed transition survives power loss, not just
        # process death. Write rate here is one tx per note/file, so the cost
        # is negligible.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._init_schema()

    # -- lifecycle ---------------------------------------------------------

    def _init_schema(self) -> None:
        """Creates the schema on a fresh database; validates strictly otherwise.

        Fail-closed startup checks (Codex review B1/B5): the stored schema
        version must match SCHEMA_VERSION exactly (migration is intentionally
        unsupported), the notes table must have the expected columns, and
        every stored status must be a known NoteStatus. Old, newer, partially
        initialized, or tampered databases raise ValueError at open time —
        never mid-run with a confusing OperationalError.
        """
        tables = {
            row["name"]
            for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        known = tables & {"notes", "sync_meta"}
        if not known:
            with self._conn:
                self._conn.executescript(_SCHEMA_SQL)
                # P1_REVIEW_SONNET.md Adjustment 1: seed explicit defaults so
                # callers never branch on NULL vs missing vs failed.
                self._conn.executemany(
                    "INSERT OR IGNORE INTO sync_meta (key, value) VALUES (?, ?)",
                    [
                        ("schema_version", SCHEMA_VERSION),
                        ("last_enumeration_status", "NEVER_RUN"),
                        ("last_run_exit_code", "NEVER_RUN"),
                    ],
                )
            return
        if known != {"notes", "sync_meta"}:
            raise ValueError(
                f"Corrupt state database {self.db_path}: expected tables 'notes' and "
                f"'sync_meta', found only {sorted(known)} (partially initialized or "
                f"tampered). Fail closed; recreate the database."
            )
        row = self._conn.execute(
            "SELECT value FROM sync_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            raise ValueError(
                f"Corrupt state database {self.db_path}: sync_meta.schema_version "
                f"is missing. Fail closed; recreate the database."
            )
        if row["value"] != SCHEMA_VERSION:
            raise ValueError(
                f"State database schema version {row['value']!r} != expected "
                f"{SCHEMA_VERSION!r} at {self.db_path}. Migration is intentionally "
                f"unsupported: fail closed, recreate the database or use a matching build."
            )
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(notes)")}
        missing = _EXPECTED_NOTE_COLUMNS - columns
        if missing:
            raise ValueError(
                f"Corrupt state database {self.db_path}: notes table is missing "
                f"columns {sorted(missing)}. Fail closed; recreate the database."
            )
        for r in self._conn.execute("SELECT note_id, status FROM notes"):
            try:
                NoteStatus(r["status"])
            except ValueError:
                raise ValueError(
                    f"Corrupt state database {self.db_path}: unknown status "
                    f"{r['status']!r} for note {r['note_id']!r}. Fail closed."
                ) from None

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- discovery (favorites enumeration) ----------------------------------

    def upsert_note(self, ref: FavoriteRef) -> str:
        """Inserts a discovered note or refreshes its listing snapshot.

        Idempotent on note_id: a repeat insert never creates a second row and
        never touches status, attempt_count, or media_state. Snapshot fields
        are refreshed with COALESCE so a listing entry missing its token does
        not erase a previously known good one.
        """
        inserted, updated = self.upsert_notes([ref])
        return "inserted" if inserted else "updated"

    def upsert_notes(self, refs: Iterable[FavoriteRef]) -> tuple[int, int]:
        """Batch upsert of one intercepted listing page, in one transaction."""
        ref_list = list(refs)
        if not ref_list:
            return (0, 0)
        now = get_current_iso_time()
        with self._conn:
            placeholders = ",".join("?" for _ in ref_list)
            existing = {
                row["note_id"]
                for row in self._conn.execute(
                    f"SELECT note_id FROM notes WHERE note_id IN ({placeholders})",
                    [r.note_id for r in ref_list],
                )
            }
            self._conn.executemany(
                """
                INSERT INTO notes
                    (note_id, status, xsec_token, source_url, title,
                     attempt_count, created_at, last_seen_in_listing_at, updated_at)
                VALUES (?, 'DISCOVERED', ?, ?, ?, 0, ?, ?, ?)
                ON CONFLICT(note_id) DO UPDATE SET
                    xsec_token = COALESCE(excluded.xsec_token, notes.xsec_token),
                    source_url = COALESCE(excluded.source_url, notes.source_url),
                    title = COALESCE(excluded.title, notes.title),
                    last_seen_in_listing_at = excluded.last_seen_in_listing_at,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        r.note_id,
                        r.xsec_token,
                        r.source_url,
                        r.title,
                        now,
                        now,
                        now,
                    )
                    for r in ref_list
                ],
            )
        inserted = sum(1 for r in ref_list if r.note_id not in existing)
        return (inserted, len(ref_list) - inserted)

    def mark_pending(self, note_ids: Iterable[str]) -> int:
        """Enumeration admission: batch DISCOVERED -> PENDING.

        Dedicated to post-enumeration admission. Idempotent: non-DISCOVERED
        notes are silently skipped (already-queued notes are a no-op). For
        strict single-note lifecycle moves with transition validation, use
        transition() instead (docs/P1_STATE_CONTRACT_V2.md §3.3).
        """
        ids = [(nid,) for nid in note_ids]
        if not ids:
            return 0
        with self._conn:
            cur = self._conn.executemany(
                "UPDATE notes SET status = 'PENDING', updated_at = ? "
                "WHERE note_id = ? AND status = 'DISCOVERED'",
                [(get_current_iso_time(), nid) for (nid,) in ids],
            )
        return cur.rowcount

    # -- work queues ----------------------------------------------------------

    def get_fetch_queue(self, limit: int | None = None) -> list[NoteState]:
        """Notes needing a (re-)fetch, in discovery order.

        Eligible: DISCOVERED, PENDING, and RETRYABLE_FAILED. The budget is
        enforced at write time — transition() atomically finalizes a
        retryable failure whose budget is exhausted — so every RETRYABLE_FAILED
        row returned here has attempts remaining by construction. There are
        no stranded at-cap rows, and no query parameter can create one.

        Intentionally EXCLUDES DETAIL_SUCCESS and MEDIA_PARTIAL: those are
        media resume work and must be obtained via get_media_resume_queue().
        The two queues are disjoint and together cover all non-terminal work;
        never assume this queue alone contains everything pending
        (docs/P1_STATE_CONTRACT_V2.md §3).
        """
        sql = (
            "SELECT * FROM notes "
            "WHERE status IN ('DISCOVERED', 'PENDING', 'RETRYABLE_FAILED') "
            "ORDER BY rowid"
        )
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_state(row) for row in rows]

    def get_media_resume_queue(self, limit: int | None = None) -> list[NoteState]:
        """Notes whose detail is durably captured but media is unfinished.

        Eligible: DETAIL_SUCCESS and MEDIA_PARTIAL, in discovery order. These
        do NOT go through fetch_note — the caller resumes from raw.json on
        disk (docs/P1_STATE_CONTRACT_V2.md §3.1).

        Call recover_interrupted() before queueing: a MEDIA_SYNCING row is a
        crash trace and is absent from both queues until recovered to
        DETAIL_SUCCESS (docs/P1_STATE_CONTRACT_V2.md §6.4).
        """
        sql = (
            "SELECT * FROM notes "
            "WHERE status IN ('DETAIL_SUCCESS', 'MEDIA_PARTIAL') "
            "ORDER BY rowid"
        )
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_state(row) for row in rows]

    def retry_failed(self) -> int:
        """Manual reset: FINAL_FAILED -> PENDING with attempt_count cleared.

        Error fields are cleared too, matching transition() semantics for
        non-failure targets.
        """
        with self._conn:
            cur = self._conn.execute(
                "UPDATE notes SET status = 'PENDING', attempt_count = 0, "
                "last_error_status = NULL, last_error_message = NULL, last_failure_stage = NULL, updated_at = ? "
                "WHERE status = 'FINAL_FAILED'",
                (get_current_iso_time(),),
            )
        return cur.rowcount

    # -- transitions ---------------------------------------------------------

    def transition(
        self,
        note_id: str,
        to_status: NoteStatus | str,
        *,
        error_status: str | None = None,
        error_message: str | None = None,
        failure_stage: str | None = None,
    ) -> NoteState:
        """Atomically moves a note to to_status.

        Entering FETCHING increments attempt_count in the same transaction:
        every fetch start is an attempt, including ones interrupted by a
        crash (docs/P1_STATE_CONTRACT_V2.md §4.1).

        Budget invariant (Codex review B2): requesting RETRYABLE_FAILED when
        the budget is exhausted atomically lands FINAL_FAILED instead — the
        failure commit itself decides, so a RETRYABLE_FAILED row with
        attempt_count >= DEFAULT_MAX_ATTEMPTS can never exist. The coerced
        FINAL_FAILED is reachable from any source that may legally record a
        retryable failure, superseding the per-edge TRANSITIONS listing.

        FINAL_FAILED has no exit through this method (Codex review B3);
        only retry_failed() may reset it. RETRYABLE_FAILED and FINAL_FAILED
        require a taxonomy error_status (not SUCCESS) so the budget stays
        explainable; failure_stage is bound to its source phase (MEDIA only
        from media-phase statuses, FETCH/NORMALIZE otherwise). Illegal calls
        raise ValueError and change nothing.
        """
        requested = NoteStatus(to_status)
        with self._conn:
            row = self._conn.execute(
                "SELECT status, attempt_count FROM notes WHERE note_id = ?",
                (note_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown note_id: {note_id!r}")
            current = NoteStatus(row["status"])
            if requested not in TRANSITIONS[current]:
                raise ValueError(
                    f"Illegal transition {current.value} -> {requested.value} for note {note_id!r}"
                )
            if failure_stage is not None:
                if error_status is None:
                    raise ValueError("failure_stage requires error_status")
                if failure_stage not in _FAILURE_STAGES:
                    raise ValueError(
                        f"Invalid failure stage: {failure_stage!r} (expected one of {sorted(_FAILURE_STAGES)})"
                    )
                allowed_stages = (
                    ("MEDIA",) if current in _MEDIA_STAGE_SOURCES else ("FETCH", "NORMALIZE")
                )
                if failure_stage not in allowed_stages:
                    raise ValueError(
                        f"failure_stage {failure_stage!r} is impossible from {current.value}"
                    )
            if error_status is not None:
                try:
                    taxonomy = AcquisitionStatus(error_status)
                except ValueError:
                    raise ValueError(
                        f"error_status {error_status!r} is not an acquisition status"
                    ) from None
                if taxonomy is AcquisitionStatus.SUCCESS:
                    raise ValueError("error_status must describe a failure, not SUCCESS")
            has_error = any(v is not None for v in (error_status, error_message, failure_stage))
            if has_error and requested not in _ERROR_TARGETS:
                raise ValueError(
                    f"Error details only allowed on failure targets, not {requested.value}"
                )
            if requested in _RETRY_TERMINAL_TARGETS and error_status is None:
                raise ValueError(
                    f"{requested.value} requires error_status (the budget must stay explainable)"
                )
            new_attempt = (
                row["attempt_count"] + 1 if requested is NoteStatus.FETCHING else row["attempt_count"]
            )
            target = requested
            if target is NoteStatus.RETRYABLE_FAILED and new_attempt >= DEFAULT_MAX_ATTEMPTS:
                target = NoteStatus.FINAL_FAILED
            now = get_current_iso_time()
            # A transition without error details clears the stale last error.
            err_status = error_status if has_error else None
            err_message = error_message if has_error else None
            stage = failure_stage if has_error else None
            self._conn.execute(
                "UPDATE notes SET status = ?, attempt_count = ?, "
                "last_error_status = ?, last_error_message = ?, last_failure_stage = ?, updated_at = ? "
                "WHERE note_id = ?",
                (target.value, new_attempt, err_status, err_message, stage, now, note_id),
            )
        result = self.get_note(note_id)
        assert result is not None  # row proven to exist inside the transaction
        return result

    # -- media execution state -------------------------------------------------

    def record_media_result(
        self,
        note_id: str,
        filename: str,
        *,
        status: str,
        url: str | None = None,
        size_bytes: int | None = None,
        checksum_sha256: str | None = None,
        error: str | None = None,
    ) -> None:
        """Commits one media file's execution outcome (one transaction).

        Entries are keyed strictly by filename; re-recording a filename
        replaces its entry. The stored url is provenance only — recovery must
        re-derive URLs from raw.json, never from media_state.
        """
        if status not in _MEDIA_FILE_STATUSES:
            raise ValueError(f"Invalid media status: {status!r}")
        if not filename or "/" in filename or "\\" in filename or filename in {".", ".."}:
            raise ValueError(
                f"filename must be a bare basename matching assets_dir/<filename>, got {filename!r}"
            )
        with self._conn:
            row = self._conn.execute(
                "SELECT status, media_state FROM notes WHERE note_id = ?", (note_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown note_id: {note_id!r}")
            current = NoteStatus(row["status"])
            # Contract D3 (Codex review B4): media work happens only in
            # MEDIA_SYNCING, so the crash trace is always observable.
            if current is not NoteStatus.MEDIA_SYNCING:
                raise ValueError(
                    f"Cannot record media for note {note_id!r} in status {current.value}: "
                    f"transition(MEDIA_SYNCING) is mandatory before any media work"
                )
            entries: list[dict[str, Any]] = json.loads(row["media_state"]) if row["media_state"] else []
            previous = next((e for e in entries if e.get("filename") == filename), None)
            if url is None and previous is not None:
                url = previous.get("url")
            entries = [e for e in entries if e.get("filename") != filename]
            entries.append(
                {
                    "filename": filename,
                    "url": url,
                    "status": status,
                    "size_bytes": size_bytes,
                    "checksum_sha256": checksum_sha256,
                    "error": error,
                }
            )
            self._conn.execute(
                "UPDATE notes SET media_state = ?, updated_at = ? WHERE note_id = ?",
                (json.dumps(entries, ensure_ascii=False), get_current_iso_time(), note_id),
            )

    # -- crash recovery ---------------------------------------------------------

    def recover_interrupted(self) -> dict[str, int]:
        """Startup recovery for crash-trace statuses.

        FETCHING -> PENDING (attempt stays counted), unless the budget is
        already exhausted: then -> FINAL_FAILED, never a runnable state —
        crash loops must not bypass the budget (Codex review B2).
        MEDIA_SYNCING -> DETAIL_SUCCESS (media resume point; raw.json on disk
        is the durable payload). Never fabricates progress: each reset only
        returns the note to a state whose on-disk evidence already exists.
        """
        now = get_current_iso_time()
        with self._conn:
            reset = self._conn.execute(
                "UPDATE notes SET status = 'PENDING', updated_at = ? "
                "WHERE status = 'FETCHING' AND attempt_count < ?",
                (now, DEFAULT_MAX_ATTEMPTS),
            )
            finalized = self._conn.execute(
                "UPDATE notes SET status = 'FINAL_FAILED', "
                "last_error_status = 'UNKNOWN_ACQUISITION_FAILURE', "
                "last_error_message = 'fetch budget exhausted while interrupted', "
                "last_failure_stage = 'FETCH', updated_at = ? "
                "WHERE status = 'FETCHING' AND attempt_count >= ?",
                (now, DEFAULT_MAX_ATTEMPTS),
            )
            media = self._conn.execute(
                "UPDATE notes SET status = 'DETAIL_SUCCESS', updated_at = ? "
                "WHERE status = 'MEDIA_SYNCING'",
                (now,),
            )
        return {
            "fetching_reset": reset.rowcount,
            "fetching_finalized": finalized.rowcount,
            "media_syncing_reset": media.rowcount,
        }

    # -- sync metadata ---------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM sync_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else default

    def set_meta(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO sync_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # -- introspection ---------------------------------------------------------

    def get_note(self, note_id: str) -> NoteState | None:
        row = self._conn.execute("SELECT * FROM notes WHERE note_id = ?", (note_id,)).fetchone()
        return self._row_to_state(row) if row is not None else None

    def get_notes_by_status(self, status: NoteStatus | str, limit: int | None = None) -> list[NoteState]:
        """Narrow read-only query by exact status, in discovery order.

        Exists for the runner's COMPLETE artifact-guard scan: COMPLETE rows
        are intentionally absent from both work queues, so this is the only
        public way to enumerate them. Not a general repository API.
        """
        target = NoteStatus(status)
        sql = "SELECT * FROM notes WHERE status = ? ORDER BY rowid"
        params: list[Any] = [target.value]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_state(row) for row in rows]

    def get_media_state(self, note_id: str) -> list[dict[str, Any]]:
        """Parsed media entries; empty list when the media phase never started."""
        state = self.get_note(note_id)
        if state is None:
            raise ValueError(f"Unknown note_id: {note_id!r}")
        return state.media_state or []

    def status_counts(self) -> dict[str, int]:
        """Counts per status, zero-filled across the full taxonomy.

        Unknown status values raise instead of being silently counted
        (Codex review B5): the startup check should have caught them, so
        reaching one here means mid-run corruption — fail closed.
        """
        counts = {status.value: 0 for status in NoteStatus}
        for row in self._conn.execute("SELECT status, COUNT(*) AS n FROM notes GROUP BY status"):
            if row["status"] not in counts:
                raise ValueError(
                    f"Unknown status {row['status']!r} in state database. Fail closed."
                )
            counts[row["status"]] = row["n"]
        return counts

    @staticmethod
    def _row_to_state(row: sqlite3.Row) -> NoteState:
        return NoteState(
            note_id=row["note_id"],
            status=NoteStatus(row["status"]),
            attempt_count=row["attempt_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            xsec_token=row["xsec_token"],
            source_url=row["source_url"],
            title=row["title"],
            last_error_status=row["last_error_status"],
            last_error_message=row["last_error_message"],
            last_failure_stage=row["last_failure_stage"],
            media_state=json.loads(row["media_state"]) if row["media_state"] else None,
            last_seen_in_listing_at=row["last_seen_in_listing_at"],
        )
