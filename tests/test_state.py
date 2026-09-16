"""Tests for the SQLite-backed state store: schema, transitions, idempotency,
media execution state, and crash recovery."""

import sqlite3
from pathlib import Path

import pytest

from xhs_ingest.models import FavoriteRef
from xhs_ingest.state import (
    DEFAULT_MAX_ATTEMPTS,
    SCHEMA_VERSION,
    NoteStatus,
    StateStore,
)


@pytest.fixture()
def store(tmp_path: Path):
    s = StateStore(tmp_path / "sync.db")
    yield s
    s.close()


def make_ref(note_id: str = "note_1", token: str = "tok_a") -> FavoriteRef:
    return FavoriteRef(
        note_id=note_id,
        source_url=f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={token}&xsec_source=pc_fav",
        title=f"Title of {note_id}",
        xsec_token=token,
    )


def drive(store: StateStore, note_id: str, steps: list) -> None:
    """Walks a note through transition steps; tuple items carry kwargs."""
    for step in steps:
        status, kwargs = step if isinstance(step, tuple) else (step, {})
        store.transition(note_id, status, **kwargs)


def row_count(store: StateStore, table: str) -> int:
    return store._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


# -- initialization ---------------------------------------------------------


def test_init_creates_schema_and_seeds_meta(store: StateStore):
    tables = {
        r["name"]
        for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"notes", "sync_meta"} <= tables
    # Adjustment 1: explicit NEVER_RUN defaults, no NULL branching downstream.
    assert store.get_meta("schema_version") == SCHEMA_VERSION
    assert store.get_meta("last_enumeration_status") == "NEVER_RUN"
    assert store.get_meta("last_run_exit_code") == "NEVER_RUN"


def test_init_is_idempotent_and_preserves_data(store: StateStore, tmp_path: Path):
    store.upsert_note(make_ref("note_1"))
    store.close()

    reopened = StateStore(tmp_path / "sync.db")
    try:
        assert row_count(reopened, "notes") == 1
        assert reopened.get_note("note_1") is not None
        assert reopened.get_meta("last_enumeration_status") == "NEVER_RUN"
    finally:
        reopened.close()


# -- insert / idempotency ------------------------------------------------------


def test_upsert_note_inserts_discovered(store: StateStore):
    result = store.upsert_note(make_ref("note_1", token="tok_a"))
    assert result == "inserted"

    state = store.get_note("note_1")
    assert state is not None
    assert state.status is NoteStatus.DISCOVERED
    assert state.attempt_count == 0
    assert state.xsec_token == "tok_a"
    assert state.media_state is None  # media phase never started
    assert state.created_at is not None
    assert state.updated_at is not None
    assert state.last_seen_in_listing_at is not None


def test_upsert_duplicate_never_creates_second_row(store: StateStore):
    store.upsert_note(make_ref("note_1", token="tok_old"))
    result = store.upsert_note(make_ref("note_1", token="tok_new"))

    assert result == "updated"
    assert row_count(store, "notes") == 1

    state = store.get_note("note_1")
    assert state.status is NoteStatus.DISCOVERED
    assert state.xsec_token == "tok_new"  # listing snapshot refreshed


def test_upsert_does_not_clobber_progress(store: StateStore):
    store.upsert_note(make_ref("note_1", token="tok_old"))
    store.transition("note_1", NoteStatus.PENDING)
    store.transition("note_1", NoteStatus.FETCHING)
    store.transition("note_1", NoteStatus.DETAIL_SUCCESS)
    store.transition("note_1", NoteStatus.MEDIA_SYNCING)
    store.record_media_result("note_1", "image_01.jpg", status="COMPLETED", size_bytes=10, checksum_sha256="abc")

    store.upsert_note(make_ref("note_1", token="tok_new"))

    state = store.get_note("note_1")
    assert state.status is NoteStatus.MEDIA_SYNCING  # progress untouched
    assert state.attempt_count == 1
    assert state.media_state is not None and len(state.media_state) == 1
    assert state.xsec_token == "tok_new"  # token still refreshed


def test_upsert_keeps_known_token_when_new_listing_lacks_it(store: StateStore):
    store.upsert_note(make_ref("note_1", token="tok_known"))
    # Next enumeration saw the note but its listing entry had no token.
    store.upsert_note(FavoriteRef(note_id="note_1", source_url="https://www.xiaohongshu.com/explore/note_1"))
    assert store.get_note("note_1").xsec_token == "tok_known"


def test_upsert_notes_batch_is_one_page_commit(store: StateStore):
    store.upsert_note(make_ref("note_1", token="tok_old"))
    inserted, updated = store.upsert_notes(
        [make_ref("note_1", token="tok_new"), make_ref("note_2"), make_ref("note_3")]
    )
    assert (inserted, updated) == (2, 1)
    assert row_count(store, "notes") == 3


# -- transitions ---------------------------------------------------------------


def test_legal_lifecycle_transitions(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    store.transition("note_1", NoteStatus.PENDING)
    store.transition("note_1", NoteStatus.FETCHING)
    store.transition("note_1", NoteStatus.DETAIL_SUCCESS)
    store.transition("note_1", NoteStatus.MEDIA_SYNCING)
    store.transition("note_1", NoteStatus.COMPLETE)
    assert store.get_note("note_1").status is NoteStatus.COMPLETE


def test_illegal_transition_rejected_and_unchanged(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    with pytest.raises(ValueError, match="Illegal transition"):
        store.transition("note_1", NoteStatus.COMPLETE)
    assert store.get_note("note_1").status is NoteStatus.DISCOVERED


def test_transition_unknown_note_rejected(store: StateStore):
    with pytest.raises(ValueError, match="Unknown note_id"):
        store.transition("ghost", NoteStatus.PENDING)


def test_error_details_only_on_failure_targets(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    store.transition("note_1", NoteStatus.PENDING)
    store.transition("note_1", NoteStatus.FETCHING)
    with pytest.raises(ValueError, match="failure targets"):
        store.transition("note_1", NoteStatus.DETAIL_SUCCESS, error_status="PARSE_FAILED")


def test_error_recorded_then_cleared_on_recovery(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    store.transition("note_1", NoteStatus.PENDING)
    store.transition("note_1", NoteStatus.FETCHING)
    failed = store.transition(
        "note_1",
        NoteStatus.RETRYABLE_FAILED,
        error_status="NETWORK_ERROR",
        error_message="connection reset",
    )
    assert failed.last_error_status == "NETWORK_ERROR"
    assert failed.last_error_message == "connection reset"

    # Next run: requeue, refetch, succeed -> stale error cleared.
    store.transition("note_1", NoteStatus.FETCHING)
    recovered = store.transition("note_1", NoteStatus.DETAIL_SUCCESS)
    assert recovered.last_error_status is None
    assert recovered.last_error_message is None


# -- retry counting -------------------------------------------------------------


def test_attempt_count_increments_on_each_fetch_start(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    store.transition("note_1", NoteStatus.PENDING)

    for expected_attempt in (1, 2, 3):
        store.transition("note_1", NoteStatus.FETCHING)
        assert store.get_note("note_1").attempt_count == expected_attempt
        store.transition("note_1", NoteStatus.RETRYABLE_FAILED, error_status="NETWORK_ERROR")


def test_retry_budget_exhaustion_finalizes_atomically(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    store.transition("note_1", NoteStatus.PENDING)

    for attempt in range(1, DEFAULT_MAX_ATTEMPTS + 1):
        started = store.transition("note_1", NoteStatus.FETCHING)
        assert started.attempt_count == attempt
        failed = store.transition(
            "note_1",
            NoteStatus.RETRYABLE_FAILED,
            error_status="NETWORK_ERROR",
            failure_stage="FETCH",
        )
        if attempt < DEFAULT_MAX_ATTEMPTS:
            assert failed.status is NoteStatus.RETRYABLE_FAILED
        else:
            # Budget exhausted: the failure commit itself atomically finalizes.
            assert failed.status is NoteStatus.FINAL_FAILED
            assert failed.last_error_status == "NETWORK_ERROR"
            assert failed.attempt_count == DEFAULT_MAX_ATTEMPTS

    # Invariant: no stranded at-cap RETRYABLE_FAILED in either queue.
    assert store.get_fetch_queue() == []
    assert store.get_media_resume_queue() == []
    # FINAL_FAILED leaves only through the manual reset.
    assert store.retry_failed() == 1
    assert store.get_note("note_1").status is NoteStatus.PENDING


def test_crash_at_cap_fetching_is_finalized_not_resumable(tmp_path: Path):
    db = tmp_path / "sync.db"
    store = StateStore(db)
    store.upsert_note(make_ref("note_1"))
    store.transition("note_1", NoteStatus.PENDING)
    for _ in range(DEFAULT_MAX_ATTEMPTS - 1):
        store.transition("note_1", NoteStatus.FETCHING)
        store.transition("note_1", NoteStatus.RETRYABLE_FAILED, error_status="NETWORK_ERROR")
    store.transition("note_1", NoteStatus.FETCHING)  # 5th start; crash mid-fetch
    store.close()

    reopened = StateStore(db)  # fresh process on the same database
    try:
        counts = reopened.recover_interrupted()
        assert counts == {"fetching_reset": 0, "fetching_finalized": 1, "media_syncing_reset": 0}
        state = reopened.get_note("note_1")
        assert state.status is NoteStatus.FINAL_FAILED  # never a runnable state
        assert state.attempt_count == DEFAULT_MAX_ATTEMPTS
        assert state.last_error_status == "UNKNOWN_ACQUISITION_FAILURE"
        assert reopened.get_fetch_queue() == []  # no 6th automatic fetch
    finally:
        reopened.close()


def test_final_failed_exit_only_via_retry_failed(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    drive(
        store,
        "note_1",
        FETCH_PATH + [
            (NoteStatus.FINAL_FAILED, dict(error_status="CONTENT_UNAVAILABLE", failure_stage="FETCH")),
        ],
    )
    with pytest.raises(ValueError, match="Illegal transition"):
        store.transition("note_1", NoteStatus.PENDING)  # generic exit is closed
    state = store.get_note("note_1")
    assert state.status is NoteStatus.FINAL_FAILED
    assert state.attempt_count == 1  # zero side effects

    assert store.retry_failed() == 1  # the dedicated path still works
    assert store.get_note("note_1").status is NoteStatus.PENDING


def test_fetch_queue_orders_by_discovery_order(store: StateStore):
    for nid in ("note_b", "note_a", "note_c"):
        store.upsert_note(make_ref(nid))
    store.transition("note_b", NoteStatus.PENDING)
    assert [n.note_id for n in store.get_fetch_queue()] == ["note_b", "note_a", "note_c"]


# -- work queues (contract v2: two disjoint queues) --------------------------


FETCH_PATH = [NoteStatus.PENDING, NoteStatus.FETCHING]


def test_fetch_queue_excludes_media_resume_notes(store: StateStore):
    store.upsert_note(make_ref("fetch_work"))
    store.upsert_note(make_ref("detail_ok"))
    store.upsert_note(make_ref("partial"))
    store.transition("fetch_work", NoteStatus.PENDING)
    for nid in ("detail_ok", "partial"):
        drive(store, nid, FETCH_PATH + [NoteStatus.DETAIL_SUCCESS])
    drive(
        store,
        "partial",
        [NoteStatus.MEDIA_SYNCING,
         (NoteStatus.MEDIA_PARTIAL, dict(error_status="MEDIA_DOWNLOAD_FAILED", failure_stage="MEDIA"))],
    )

    assert [n.note_id for n in store.get_fetch_queue()] == ["fetch_work"]


def test_media_resume_queue_returns_detail_success_and_partial(store: StateStore):
    store.upsert_note(make_ref("fetch_work"))
    store.upsert_note(make_ref("partial"))
    store.upsert_note(make_ref("detail_ok"))
    store.upsert_note(make_ref("complete"))
    store.upsert_note(make_ref("final"))
    store.transition("fetch_work", NoteStatus.PENDING)
    for nid in ("partial", "detail_ok", "complete"):
        drive(store, nid, FETCH_PATH + [NoteStatus.DETAIL_SUCCESS])
    drive(
        store,
        "partial",
        [NoteStatus.MEDIA_SYNCING,
         (NoteStatus.MEDIA_PARTIAL, dict(error_status="MEDIA_DOWNLOAD_FAILED", failure_stage="MEDIA"))],
    )
    drive(store, "complete", [NoteStatus.MEDIA_SYNCING, NoteStatus.COMPLETE])
    drive(
        store,
        "final",
        FETCH_PATH + [
            (NoteStatus.RETRYABLE_FAILED, dict(error_status="NETWORK_ERROR", failure_stage="FETCH")),
            NoteStatus.FETCHING,
            (NoteStatus.FINAL_FAILED, dict(error_status="NETWORK_ERROR", failure_stage="FETCH")),
        ],
    )

    # Discovery order; fetch work, COMPLETE, and FINAL_FAILED stay out.
    assert [n.note_id for n in store.get_media_resume_queue()] == ["partial", "detail_ok"]


def test_queues_are_disjoint_and_cover_non_terminal_work(store: StateStore):
    paths = {
        "discovered": [],
        "pending": [NoteStatus.PENDING],
        "retryable": FETCH_PATH + [
            (NoteStatus.RETRYABLE_FAILED, dict(error_status="NETWORK_ERROR", failure_stage="FETCH")),
        ],
        "detail_ok": FETCH_PATH + [NoteStatus.DETAIL_SUCCESS],
        "partial": FETCH_PATH + [NoteStatus.DETAIL_SUCCESS, NoteStatus.MEDIA_SYNCING,
                                 (NoteStatus.MEDIA_PARTIAL, dict(error_status="MEDIA_DOWNLOAD_FAILED"))],
        "complete": FETCH_PATH + [NoteStatus.DETAIL_SUCCESS, NoteStatus.MEDIA_SYNCING, NoteStatus.COMPLETE],
        "final": FETCH_PATH + [
            (NoteStatus.FINAL_FAILED, dict(error_status="CONTENT_UNAVAILABLE", failure_stage="FETCH")),
        ],
    }
    for nid, steps in paths.items():
        store.upsert_note(make_ref(nid))
        drive(store, nid, steps)

    fetch_ids = {n.note_id for n in store.get_fetch_queue()}
    resume_ids = {n.note_id for n in store.get_media_resume_queue()}

    assert fetch_ids == {"discovered", "pending", "retryable"}
    assert resume_ids == {"detail_ok", "partial"}
    assert not (fetch_ids & resume_ids)
    # Union covers every non-terminal note; terminal statuses are in neither.
    assert fetch_ids | resume_ids == set(paths) - {"complete", "final"}


def test_recovered_fetching_lands_in_fetch_queue(store: StateStore):
    store.upsert_note(make_ref("crashed_fetch"))
    drive(store, "crashed_fetch", FETCH_PATH)  # crash mid-fetch leaves FETCHING

    store.recover_interrupted()

    assert [n.note_id for n in store.get_fetch_queue()] == ["crashed_fetch"]
    assert store.get_media_resume_queue() == []
    assert store.get_note("crashed_fetch").status is NoteStatus.PENDING


def test_recovered_media_syncing_lands_in_media_resume_queue(store: StateStore):
    store.upsert_note(make_ref("crashed_media"))
    drive(store, "crashed_media", FETCH_PATH + [NoteStatus.DETAIL_SUCCESS, NoteStatus.MEDIA_SYNCING])

    store.recover_interrupted()

    assert [n.note_id for n in store.get_media_resume_queue()] == ["crashed_media"]
    assert store.get_fetch_queue() == []
    assert store.get_note("crashed_media").status is NoteStatus.DETAIL_SUCCESS


def test_media_syncing_absent_from_both_queues_until_recovered(store: StateStore):
    # Contract v2 §6.4: recover_interrupted() must run before queueing —
    # a MEDIA_SYNCING crash trace is invisible to both queues otherwise.
    store.upsert_note(make_ref("crashed"))
    drive(store, "crashed", FETCH_PATH + [NoteStatus.DETAIL_SUCCESS, NoteStatus.MEDIA_SYNCING])

    assert store.get_fetch_queue() == []
    assert store.get_media_resume_queue() == []


def test_mark_pending_admits_discovered_only(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    store.upsert_note(make_ref("note_2"))
    store.transition("note_2", NoteStatus.PENDING)

    assert store.mark_pending(["note_1", "note_2"]) == 1
    assert store.get_note("note_1").status is NoteStatus.PENDING
    assert store.get_note("note_2").status is NoteStatus.PENDING


def test_retry_failed_resets_attempts(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    store.transition("note_1", NoteStatus.PENDING)
    store.transition("note_1", NoteStatus.FETCHING)
    store.transition("note_1", NoteStatus.FINAL_FAILED, error_status="CONTENT_UNAVAILABLE")

    assert store.retry_failed() == 1
    state = store.get_note("note_1")
    assert state.status is NoteStatus.PENDING
    assert state.attempt_count == 0
    assert state.last_error_status is None


# -- failure stage (makes the single fetch budget explainable) ----------------


def test_failure_stage_recorded_then_cleared(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    drive(store, "note_1", FETCH_PATH)
    failed = store.transition(
        "note_1",
        NoteStatus.RETRYABLE_FAILED,
        error_status="NETWORK_ERROR",
        error_message="connection reset",
        failure_stage="FETCH",
    )
    assert failed.last_failure_stage == "FETCH"
    # Single fetch budget, not split counters; the stage explains the cycle.
    assert failed.attempt_count == 1

    # Next cycle succeeds -> stale stage cleared together with the error.
    store.transition("note_1", NoteStatus.FETCHING)
    recovered = store.transition("note_1", NoteStatus.DETAIL_SUCCESS)
    assert recovered.last_failure_stage is None
    assert recovered.last_error_status is None


def test_failure_stage_bound_to_source_phase(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    drive(store, "note_1", FETCH_PATH)
    # MEDIA stage is impossible from the detail path.
    with pytest.raises(ValueError, match="impossible"):
        store.transition(
            "note_1",
            NoteStatus.RETRYABLE_FAILED,
            error_status="MEDIA_DOWNLOAD_FAILED",
            failure_stage="MEDIA",
        )
    # Real path: the no-progress escalation fires from MEDIA_SYNCING.
    store.transition("note_1", NoteStatus.DETAIL_SUCCESS)
    store.transition("note_1", NoteStatus.MEDIA_SYNCING)
    escalated = store.transition(
        "note_1",
        NoteStatus.RETRYABLE_FAILED,
        error_status="MEDIA_DOWNLOAD_FAILED",
        error_message="no progress in media resume pass",
        failure_stage="MEDIA",
    )
    assert escalated.last_failure_stage == "MEDIA"


def test_failure_stage_requires_error_status(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    drive(store, "note_1", FETCH_PATH)
    with pytest.raises(ValueError, match="failure_stage requires error_status"):
        store.transition("note_1", NoteStatus.RETRYABLE_FAILED, failure_stage="FETCH")


def test_failure_stage_rejects_unknown_value(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    drive(store, "note_1", FETCH_PATH)
    with pytest.raises(ValueError, match="Invalid failure stage"):
        store.transition(
            "note_1",
            NoteStatus.RETRYABLE_FAILED,
            error_status="NETWORK_ERROR",
            failure_stage="DOWNLOAD",
        )


def test_retry_failed_clears_failure_stage(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    drive(
        store,
        "note_1",
        FETCH_PATH + [
            (NoteStatus.FINAL_FAILED, dict(error_status="NETWORK_ERROR", failure_stage="FETCH")),
        ],
    )
    assert store.get_note("note_1").last_failure_stage == "FETCH"

    assert store.retry_failed() == 1
    state = store.get_note("note_1")
    assert state.status is NoteStatus.PENDING
    assert state.attempt_count == 0
    assert state.last_failure_stage is None


# -- media_state ----------------------------------------------------------------


def test_record_and_get_media_state(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    drive(store, "note_1", FETCH_PATH + [NoteStatus.DETAIL_SUCCESS, NoteStatus.MEDIA_SYNCING])

    assert store.get_media_state("note_1") == []  # phase started, no files yet
    store.record_media_result("note_1", "image_01.jpg", status="COMPLETED", url="https://cdn/1.jpg", size_bytes=10, checksum_sha256="abc")
    store.record_media_result("note_1", "video_01.mp4", status="FAILED", url="https://cdn/1.mp4", error="HTTP 403")

    entries = store.get_media_state("note_1")
    assert [e["filename"] for e in entries] == ["image_01.jpg", "video_01.mp4"]
    assert entries[0]["checksum_sha256"] == "abc"
    assert entries[1]["status"] == "FAILED"


def test_media_state_replaces_entry_by_strict_filename(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    drive(store, "note_1", FETCH_PATH + [NoteStatus.DETAIL_SUCCESS, NoteStatus.MEDIA_SYNCING])

    store.record_media_result("note_1", "image_01.jpg", status="FAILED", url="https://cdn/1.jpg", error="timeout")
    store.record_media_result("note_1", "image_01.jpg", status="COMPLETED", size_bytes=5, checksum_sha256="xyz")

    entries = store.get_media_state("note_1")
    assert len(entries) == 1  # keyed by filename, no duplicates
    assert entries[0]["status"] == "COMPLETED"
    assert entries[0]["url"] == "https://cdn/1.jpg"  # provenance preserved when re-record omits it


def test_media_state_rejects_pathlike_filenames(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    drive(store, "note_1", FETCH_PATH + [NoteStatus.DETAIL_SUCCESS, NoteStatus.MEDIA_SYNCING])

    for bad in ("assets/image_01.jpg", "a/b", "..", ""):
        with pytest.raises(ValueError, match="bare basename"):
            store.record_media_result("note_1", bad, status="COMPLETED")


def test_media_state_rejects_non_media_phase_note(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    with pytest.raises(ValueError, match="Cannot record media"):
        store.record_media_result("note_1", "image_01.jpg", status="COMPLETED")


def test_media_state_rejects_invalid_file_status(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    drive(store, "note_1", FETCH_PATH + [NoteStatus.DETAIL_SUCCESS, NoteStatus.MEDIA_SYNCING])
    with pytest.raises(ValueError, match="Invalid media status"):
        store.record_media_result("note_1", "image_01.jpg", status="DOWNLOADING")


# -- crash recovery ---------------------------------------------------------------


def test_recover_resets_fetching_to_pending(store: StateStore):
    store.upsert_note(make_ref("fetching_note"))
    store.upsert_note(make_ref("pending_note"))
    store.upsert_note(make_ref("complete_note"))
    for nid in ("fetching_note", "pending_note", "complete_note"):
        store.transition(nid, NoteStatus.PENDING)
    store.transition("fetching_note", NoteStatus.FETCHING)
    store.transition("complete_note", NoteStatus.FETCHING)
    store.transition("complete_note", NoteStatus.DETAIL_SUCCESS)
    store.transition("complete_note", NoteStatus.COMPLETE)

    counts = store.recover_interrupted()

    assert counts == {"fetching_reset": 1, "fetching_finalized": 0, "media_syncing_reset": 0}
    fetching = store.get_note("fetching_note")
    assert fetching.status is NoteStatus.PENDING
    assert fetching.attempt_count == 1  # the interrupted attempt stays counted
    assert store.get_note("pending_note").status is NoteStatus.PENDING
    assert store.get_note("complete_note").status is NoteStatus.COMPLETE  # untouched


def test_recover_resets_media_syncing_to_detail_success(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    store.transition("note_1", NoteStatus.PENDING)
    store.transition("note_1", NoteStatus.FETCHING)
    store.transition("note_1", NoteStatus.DETAIL_SUCCESS)
    store.transition("note_1", NoteStatus.MEDIA_SYNCING)
    store.record_media_result("note_1", "image_01.jpg", status="COMPLETED", size_bytes=1, checksum_sha256="abc")

    counts = store.recover_interrupted()

    assert counts == {"fetching_reset": 0, "fetching_finalized": 0, "media_syncing_reset": 1}
    state = store.get_note("note_1")
    assert state.status is NoteStatus.DETAIL_SUCCESS  # resume point: raw.json on disk
    assert state.media_state is not None and len(state.media_state) == 1  # media progress kept
    assert state.attempt_count == 1  # media crash does not burn a fetch attempt


def test_recover_is_noop_when_clean(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    assert store.recover_interrupted() == {
        "fetching_reset": 0,
        "fetching_finalized": 0,
        "media_syncing_reset": 0,
    }


# -- sync metadata & introspection --------------------------------------------------


def test_meta_roundtrip_and_default(store: StateStore):
    assert store.get_meta("nonexistent") is None
    assert store.get_meta("nonexistent", default="fallback") == "fallback"

    store.set_meta("last_enumeration_status", "COMPLETE")
    assert store.get_meta("last_enumeration_status") == "COMPLETE"

    store.set_meta("last_enumeration_status", "ABORTED_RATE_LIMITED")  # overwrite
    assert store.get_meta("last_enumeration_status") == "ABORTED_RATE_LIMITED"


def test_status_counts_zero_filled(store: StateStore):
    assert store.status_counts() == {s.value: 0 for s in NoteStatus}
    store.upsert_note(make_ref("note_1"))
    store.upsert_note(make_ref("note_2"))
    store.transition("note_1", NoteStatus.PENDING)
    counts = store.status_counts()
    assert counts["PENDING"] == 1
    assert counts["DISCOVERED"] == 1
    assert counts["COMPLETE"] == 0


def test_get_media_state_unknown_note_rejected(store: StateStore):
    with pytest.raises(ValueError, match="Unknown note_id"):
        store.get_media_state("ghost")


def test_get_notes_by_status_narrow_query(store: StateStore):
    store.upsert_note(make_ref("complete_note"))
    store.upsert_note(make_ref("pending_note"))
    store.upsert_note(make_ref("discovered_note"))
    drive(store, "complete_note", FETCH_PATH + [NoteStatus.DETAIL_SUCCESS, NoteStatus.MEDIA_SYNCING, NoteStatus.COMPLETE])
    store.transition("pending_note", NoteStatus.PENDING)

    results = store.get_notes_by_status(NoteStatus.COMPLETE)
    assert [n.note_id for n in results] == ["complete_note"]
    assert store.get_notes_by_status(NoteStatus.PENDING)[0].note_id == "pending_note"
    assert store.get_notes_by_status(NoteStatus.DISCOVERED, limit=1)[0].note_id == "discovered_note"
    assert store.get_notes_by_status(NoteStatus.FINAL_FAILED) == []


# -- media phase gating (Codex review B4) --------------------------------------


def test_record_media_requires_media_syncing(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    drive(store, "note_1", FETCH_PATH + [NoteStatus.DETAIL_SUCCESS])
    with pytest.raises(ValueError, match="MEDIA_SYNCING"):
        store.record_media_result("note_1", "image_01.jpg", status="COMPLETED")
    drive(
        store,
        "note_1",
        [NoteStatus.MEDIA_SYNCING,
         (NoteStatus.MEDIA_PARTIAL, dict(error_status="MEDIA_DOWNLOAD_FAILED", failure_stage="MEDIA"))],
    )
    # MEDIA_PARTIAL is a resting state, not an execution state: re-enter
    # MEDIA_SYNCING before recording more media results.
    with pytest.raises(ValueError, match="MEDIA_SYNCING"):
        store.record_media_result("note_1", "image_01.jpg", status="COMPLETED")
    store.transition("note_1", NoteStatus.MEDIA_SYNCING)
    store.record_media_result("note_1", "image_01.jpg", status="COMPLETED", size_bytes=1)


# -- error record validation (Codex review B6) -----------------------------------


def test_failure_targets_require_error_status(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    drive(store, "note_1", FETCH_PATH)
    with pytest.raises(ValueError, match="requires error_status"):
        store.transition("note_1", NoteStatus.RETRYABLE_FAILED)


def test_error_status_must_be_failure_taxonomy(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    drive(store, "note_1", FETCH_PATH)
    with pytest.raises(ValueError, match="not an acquisition status"):
        store.transition("note_1", NoteStatus.RETRYABLE_FAILED, error_status="BOGUS")
    with pytest.raises(ValueError, match="not SUCCESS"):
        store.transition("note_1", NoteStatus.RETRYABLE_FAILED, error_status="SUCCESS")


def test_media_escalation_at_budget_cap_finalizes(store: StateStore):
    store.upsert_note(make_ref("note_1"))
    store.transition("note_1", NoteStatus.PENDING)
    # URL-expired escalations burn budget through the fetch cycles they force.
    for _ in range(DEFAULT_MAX_ATTEMPTS - 1):
        drive(
            store,
            "note_1",
            [NoteStatus.FETCHING, NoteStatus.DETAIL_SUCCESS, NoteStatus.MEDIA_SYNCING,
             (NoteStatus.RETRYABLE_FAILED,
              dict(error_status="MEDIA_DOWNLOAD_FAILED", failure_stage="MEDIA",
                   error_message="URL_EXPIRED"))],
        )
    store.transition("note_1", NoteStatus.FETCHING)  # attempt = cap
    store.transition("note_1", NoteStatus.DETAIL_SUCCESS)
    store.transition("note_1", NoteStatus.MEDIA_SYNCING)
    final = store.transition(
        "note_1",
        NoteStatus.RETRYABLE_FAILED,
        error_status="MEDIA_DOWNLOAD_FAILED",
        error_message="no progress in media resume pass",
        failure_stage="MEDIA",
    )
    # Coerced across the per-edge listing: exhausted budget is terminal.
    assert final.status is NoteStatus.FINAL_FAILED
    assert final.attempt_count == DEFAULT_MAX_ATTEMPTS
    assert store.get_fetch_queue() == []


# -- startup integrity (Codex review B1/B5) --------------------------------------


def _raw_exec(path: Path, sql: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(sql)
    conn.commit()
    conn.close()


_SCHEMA_V2_NOTES_ONLY = """
CREATE TABLE notes (
    note_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    xsec_token TEXT,
    source_url TEXT,
    title TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error_status TEXT,
    last_error_message TEXT,
    last_failure_stage TEXT,
    media_state TEXT,
    created_at TEXT NOT NULL,
    last_seen_in_listing_at TEXT,
    updated_at TEXT NOT NULL
);
"""


def test_v1_schema_db_rejected_at_startup(tmp_path: Path):
    db = tmp_path / "sync.db"
    _raw_exec(
        db,
        """
        CREATE TABLE notes (
            note_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error_status TEXT, last_error_message TEXT, media_state TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE sync_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO sync_meta (key, value) VALUES ('schema_version', '1');
        INSERT INTO notes (note_id, status, attempt_count, created_at, updated_at)
            VALUES ('legacy', 'PENDING', 1, '2026-01-01', '2026-01-01');
        """,
    )
    with pytest.raises(ValueError, match="schema version"):
        StateStore(db)


def test_future_schema_version_rejected_at_startup(tmp_path: Path):
    db = tmp_path / "sync.db"
    store = StateStore(db)
    store.close()
    _raw_exec(db, "UPDATE sync_meta SET value = '99' WHERE key = 'schema_version'")
    with pytest.raises(ValueError, match="schema version"):
        StateStore(db)


def test_missing_schema_version_rejected_at_startup(tmp_path: Path):
    db = tmp_path / "sync.db"
    store = StateStore(db)
    store.close()
    _raw_exec(db, "DELETE FROM sync_meta WHERE key = 'schema_version'")
    with pytest.raises(ValueError, match="schema_version"):
        StateStore(db)


def test_corrupt_status_rejected_at_startup(tmp_path: Path):
    db = tmp_path / "sync.db"
    store = StateStore(db)
    store.upsert_note(make_ref("note_1"))
    store.close()
    _raw_exec(db, "UPDATE notes SET status = 'WEIRD' WHERE note_id = 'note_1'")
    with pytest.raises(ValueError, match="unknown status"):
        StateStore(db)


def test_partial_schema_rejected_at_startup(tmp_path: Path):
    db = tmp_path / "sync.db"
    _raw_exec(db, _SCHEMA_V2_NOTES_ONLY)
    with pytest.raises(ValueError, match="partially initialized"):
        StateStore(db)
