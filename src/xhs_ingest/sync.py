"""Minimal sync runner: favorites -> state upsert -> detail -> normalize -> media -> COMPLETE.

Scope notes: single pass over both queues (media resume first, then fetch);
retry budget decided by StateStore at write time; exit 0 requires a server
termination proof from enumeration AND zero non-COMPLETE tracked notes.
"""

import json
import logging
from pathlib import Path

from .collector import XHS_BASE_URL
from .errors import AcquisitionError, AcquisitionStatus, ParseFailedError
from .media import download_media_item
from .models import FavoriteRef, get_current_iso_time
from .normalizer import normalize_note
from .renderer import render_post_markdown
from .state import DEFAULT_MAX_ATTEMPTS, NoteState, NoteStatus, StateStore

logger = logging.getLogger(__name__)

# Session-level failures: continuing would only hammer a dead/angry session.
ABORT_STATUSES = {
    AcquisitionStatus.AUTH_REQUIRED,
    AcquisitionStatus.RISK_CONTROLLED,
    AcquisitionStatus.RATE_LIMITED,
}

_EMPTY_STATE_PROOF = "EXPLICIT_EMPTY_STATE_VERIFIED_ON_PAGE"


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _ref_from_state(note: NoteState) -> FavoriteRef:
    source_url = note.source_url or f"{XHS_BASE_URL}/explore/{note.note_id}"
    return FavoriteRef(
        note_id=note.note_id,
        source_url=source_url,
        xsec_token=note.xsec_token,
        title=note.title,
    )


def _record_failure(store: StateStore, note_id: str, exc: AcquisitionError, stage: str) -> NoteState:
    target = (
        NoteStatus.FINAL_FAILED
        if exc.status is AcquisitionStatus.CONTENT_UNAVAILABLE
        else NoteStatus.RETRYABLE_FAILED
    )
    result = store.transition(
        note_id,
        target,
        error_status=exc.status.value,
        error_message=exc.message[:500],
        failure_stage=stage,
    )
    if result.status is NoteStatus.FINAL_FAILED and result.attempt_count >= DEFAULT_MAX_ATTEMPTS:
        logger.warning("[sync] note %s exhausted its fetch budget (use --retry-failed to reset)", note_id)
    return result


# -- media phase (shared by fetch path and resume path) ----------------------


def _media_phase(store: StateStore, post, note_dir: Path, existing_state: list | None) -> bool:
    """Downloads manifest files one by one, committing each result to state.

    Skip-if-verified: a file whose recorded execution state says COMPLETED and
    whose bytes on disk match the recorded size is not re-downloaded. Returns
    True only if every manifest item ended COMPLETED.
    """
    assets_dir = note_dir / "assets"
    recorded = {e.get("filename"): e for e in (existing_state or []) if isinstance(e, dict)}
    all_ok = True
    for item in post.media:
        prior = recorded.get(item.filename)
        target = assets_dir / item.filename
        if (
            prior is not None
            and prior.get("status") == "COMPLETED"
            and prior.get("size_bytes")
            and target.exists()
            and target.stat().st_size == prior.get("size_bytes")
        ):
            item.download_status = "COMPLETED"
            item.size_bytes = prior["size_bytes"]
            item.checksum_sha256 = prior.get("checksum_sha256")
            item.local_path = str(target)
            continue  # verified skip; state already records it
        try:
            download_media_item(item, assets_dir)
            store.record_media_result(
                post.note_id,
                item.filename,
                status="COMPLETED",
                url=item.url,
                size_bytes=item.size_bytes,
                checksum_sha256=item.checksum_sha256,
            )
        except Exception as exc:
            all_ok = False
            store.record_media_result(
                post.note_id, item.filename, status="FAILED", url=item.url, error=str(exc)[:300]
            )
    return all_ok


def _verify_media_on_disk(post, assets_dir: Path) -> bool:
    """Commit verification: every COMPLETED manifest item exists with the
    recorded size. Catches files deleted/truncated between download and seal."""
    for item in post.media:
        if item.download_status != "COMPLETED":
            return False
        path = assets_dir / item.filename
        if not path.exists():
            return False
        if item.size_bytes is not None and path.stat().st_size != item.size_bytes:
            return False
    return True


def _commit_note(store: StateStore, post, note_dir: Path) -> str:
    """Renders artifacts and closes out the note; COMPLETE is the last write."""
    _atomic_write_text(
        note_dir / "canonical.json",
        json.dumps(post.to_dict(include_raw=False), ensure_ascii=False, indent=2),
    )
    _atomic_write_text(note_dir / "post.md", render_post_markdown(post))
    if _verify_media_on_disk(post, note_dir / "assets"):
        _atomic_write_text(
            note_dir / ".complete",
            json.dumps({"note_id": post.note_id, "finished_at": get_current_iso_time()}, ensure_ascii=False),
        )
        store.transition(post.note_id, NoteStatus.COMPLETE)
        return "COMPLETE"
    failed = sum(1 for m in post.media if m.download_status != "COMPLETED")
    store.transition(
        post.note_id,
        NoteStatus.MEDIA_PARTIAL,
        error_status="MEDIA_DOWNLOAD_FAILED",
        error_message=f"{failed}/{len(post.media)} media files failed or unverified",
        failure_stage="MEDIA",
    )
    return "PARTIAL"


# -- fetch path -----------------------------------------------------------------


def _ingest_note(session, store: StateStore, note: NoteState, output_dir: Path) -> str:
    """Runs one note through the full pipeline. Returns COMPLETE | PARTIAL | FAILED | ABORT."""
    note_id = note.note_id
    ref = _ref_from_state(note)
    store.transition(note_id, NoteStatus.FETCHING)

    try:
        raw = session.fetch_note(ref)
    except AcquisitionError as exc:
        _record_failure(store, note_id, exc, stage="FETCH")
        return "ABORT" if exc.status in ABORT_STATUSES else "FAILED"

    try:
        post = normalize_note(raw, source_url=ref.source_url)
    except ParseFailedError as exc:
        _record_failure(store, note_id, exc, stage="NORMALIZE")
        return "FAILED"
    if post.note_id != note_id:
        exc = ParseFailedError(
            f"normalized note_id {post.note_id!r} != requested {note_id!r}",
            details={"note_id": note_id},
        )
        _record_failure(store, note_id, exc, stage="NORMALIZE")
        return "FAILED"

    note_dir = output_dir / note_id
    _atomic_write_text(note_dir / "raw.json", json.dumps(post.raw, ensure_ascii=False, indent=2))
    store.transition(note_id, NoteStatus.DETAIL_SUCCESS)
    store.transition(note_id, NoteStatus.MEDIA_SYNCING)
    ok = _media_phase(store, post, note_dir, existing_state=None)
    if not ok:
        for m in post.media:
            if m.download_status != "COMPLETED" and not m.error:
                m.error = "download failed"
    return _commit_note(store, post, note_dir)


# -- media resume path -------------------------------------------------------------


def _resume_note_media(store: StateStore, note: NoteState, output_dir: Path) -> str:
    """Closes out DETAIL_SUCCESS / MEDIA_PARTIAL from raw.json on disk.

    raw.json is the evidence for these statuses: if it is missing or can no
    longer be normalized, the evidence is invalidated and the note demotes to
    PENDING for a fresh fetch (no attempt charged; existing legal edges).
    """
    note_id = note.note_id
    note_dir = output_dir / note_id
    raw_path = note_dir / "raw.json"
    if not raw_path.exists():
        store.transition(note_id, NoteStatus.PENDING)
        logger.warning("[sync] %s: raw.json missing; demoted to PENDING for refetch", note_id)
        return "DEMOTED"
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        post = normalize_note(raw, source_url=note.source_url)
    except (OSError, ValueError, ParseFailedError) as exc:
        store.transition(note_id, NoteStatus.PENDING)
        logger.warning("[sync] %s: raw.json unusable (%s); demoted to PENDING", note_id, exc)
        return "DEMOTED"
    if post.note_id != note_id:
        store.transition(note_id, NoteStatus.PENDING)
        logger.warning("[sync] %s: raw.json holds note %s; demoted to PENDING", note_id, post.note_id)
        return "DEMOTED"

    store.transition(note_id, NoteStatus.MEDIA_SYNCING)
    _media_phase(store, post, note_dir, existing_state=note.media_state)
    return _commit_note(store, post, note_dir)


def _process_media_resume_queue(store: StateStore, output_dir: Path) -> None:
    for note in store.get_media_resume_queue():
        outcome = _resume_note_media(store, note, output_dir)
        logger.info("[sync] media-resume %s -> %s", note.note_id, outcome)


# -- COMPLETE artifact guard ---------------------------------------------------------


def _verify_complete_artifacts(note: NoteState, note_dir: Path) -> bool:
    if not (note_dir / "raw.json").exists():
        return False
    if not (note_dir / "canonical.json").exists():
        return False
    if not (note_dir / "post.md").exists():
        return False
    for entry in note.media_state or []:
        if not isinstance(entry, dict) or entry.get("status") != "COMPLETED":
            return False
        path = note_dir / "assets" / entry.get("filename", "")
        if not path.exists() or path.stat().st_size != entry.get("size_bytes"):
            return False
    return True


def _guard_complete(store: StateStore, output_dir: Path) -> tuple[int, int]:
    """Two-tier guard over COMPLETE notes (state must not lie about disk).

    `.complete` missing alone is the crash window before the seal: if all
    artifacts verify, rewrite the seal (repair). Real evidence loss (raw/
    canonical/media missing or size-mismatched) demotes to PENDING.
    """
    repaired = demoted = 0
    for note in store.get_notes_by_status(NoteStatus.COMPLETE):
        note_dir = output_dir / note.note_id
        artifacts_ok = _verify_complete_artifacts(note, note_dir)
        has_seal = (note_dir / ".complete").exists()
        if not artifacts_ok:
            if has_seal:
                (note_dir / ".complete").unlink(missing_ok=True)
            store.transition(note.note_id, NoteStatus.PENDING)
            demoted += 1
            logger.warning("[sync] %s: COMPLETE artifacts missing; demoted to PENDING", note.note_id)
        elif not has_seal:
            _atomic_write_text(
                note_dir / ".complete",
                json.dumps({"note_id": note.note_id, "finished_at": get_current_iso_time()}, ensure_ascii=False),
            )
            repaired += 1
            logger.info("[sync] %s: repaired missing .complete seal", note.note_id)
    return repaired, demoted


# -- run loop ---------------------------------------------------------------------------


def _process_fetch_queue(session, store: StateStore, output_dir: Path, max_notes: int | None) -> bool:
    """Returns True if the run was aborted (auth/risk/rate)."""
    for note in store.get_fetch_queue(limit=max_notes):
        outcome = _ingest_note(session, store, note, output_dir)
        logger.info("[sync] note %s -> %s", note.note_id, outcome)
        if outcome == "ABORT":
            logger.error("[sync] run-level abort (auth/risk/rate); state preserved, rerun to continue")
            return True
    return False


def run_sync(collector, store: StateStore, output_dir: Path, max_notes: int | None = None) -> int:
    """One sync run: recover, guard, enumerate, media-resume, fetch, finalize."""
    output_dir = Path(output_dir)
    recovery = store.recover_interrupted()
    if any(recovery.values()):
        logger.info("[sync] startup recovery: %s", recovery)
    repaired, demoted = _guard_complete(store, output_dir)
    if repaired or demoted:
        logger.info("[sync] COMPLETE guard: %d repaired, %d demoted", repaired, demoted)

    enum_error: AcquisitionError | None = None
    enum_proven = False
    aborted = False
    with collector.session() as session:
        try:
            pages = 0
            for page_result in session.iter_favorites(limit=max_notes):
                inserted, _ = store.upsert_notes(page_result.items)
                store.mark_pending([r.note_id for r in page_result.items])
                pages += 1
                if page_result.has_more is False or page_result.completion_proof == _EMPTY_STATE_PROOF:
                    enum_proven = True
                logger.info(
                    "[sync] enumeration page %d: %d item(s), %d new",
                    pages, len(page_result.items), inserted,
                )
        except AcquisitionError as exc:
            logger.warning("[sync] enumeration ended with %s: %s", exc.status.value, exc.message)
            enum_error = exc

        if enum_error is None or enum_error.status not in ABORT_STATUSES:
            _process_media_resume_queue(store, output_dir)
            aborted = _process_fetch_queue(session, store, output_dir, max_notes)

    counts = store.status_counts()
    remaining = sum(n for s, n in counts.items() if s != "COMPLETE")
    print("\n=== Sync summary ===")
    for status, n in sorted(counts.items()):
        if n:
            print(f"  {status}: {n}")

    if enum_error is not None or aborted:
        exit_code = 2
    elif remaining:
        exit_code = 3
    elif not enum_proven:
        logger.warning(
            "[sync] enumeration completion unproven (slice or stall); not claiming full sync"
        )
        exit_code = 3
    else:
        exit_code = 0
    store.set_meta("last_enumeration_status", "COMPLETE" if enum_proven else "UNPROVEN")
    store.set_meta("last_run_exit_code", str(exit_code))
    store.set_meta("last_run_at", get_current_iso_time())
    return exit_code
