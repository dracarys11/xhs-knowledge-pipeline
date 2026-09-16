"""Tests for the minimal sync runner and collector session helpers (no network)."""

import argparse
import json
from pathlib import Path

import pytest

from xhs_ingest import sync
from xhs_ingest.collector import CollectorSession, build_favorite_refs, feed_payload_has_note
from xhs_ingest.errors import (
    AuthRequiredError,
    ContentUnavailableError,
    MediaDownloadError,
    NetworkAcquisitionError,
)
from xhs_ingest.models import FavoriteRef, PageResult
from xhs_ingest.state import NoteStatus, StateStore


class FakeSession:
    def __init__(self, raw: dict | None = None, error: Exception | None = None) -> None:
        self.raw = raw
        self.error = error
        self.fetched: list[str] = []

    def fetch_note(self, ref: FavoriteRef) -> dict:
        self.fetched.append(ref.note_id)
        if self.error is not None:
            raise self.error
        return self.raw


def raw_payload(note_id: str, images: int = 0) -> dict:
    image_list = [
        {"url_default": f"https://ci.xiaohongshu.com/{note_id}_{i}.jpg"} for i in range(images)
    ]
    return {
        "data": {
            "items": [
                {
                    "id": note_id,
                    "note_card": {
                        "note_id": note_id,
                        "title": "Test",
                        "desc": "Body",
                        "user": {"user_id": "u1", "nickname": "Author"},
                        "image_list": image_list,
                    },
                }
            ]
        }
    }


def make_pending_note(store: StateStore, note_id: str = "n1") -> None:
    store.upsert_note(
        FavoriteRef(
            note_id=note_id,
            source_url=f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=tok&xsec_source=pc_fav",
            xsec_token="tok",
        )
    )
    store.mark_pending([note_id])


# -- happy path ---------------------------------------------------------------


def test_ingest_note_complete_zero_media(store: StateStore, tmp_path: Path):
    make_pending_note(store, "n1")
    session = FakeSession(raw=raw_payload("n1"))

    outcome = sync._ingest_note(session, store, store.get_note("n1"), tmp_path)

    assert outcome == "COMPLETE"
    assert store.get_note("n1").status is NoteStatus.COMPLETE
    note_dir = tmp_path / "n1"
    for name in ("raw.json", "canonical.json", "post.md", ".complete"):
        assert (note_dir / name).exists(), name
    # raw.json preserved the server payload; canonical excluded it.
    assert "note_card" in json.loads((note_dir / "raw.json").read_text())["data"]["items"][0]
    assert "raw" not in json.loads((note_dir / "canonical.json").read_text())


def test_ingest_note_media_partial(store: StateStore, tmp_path: Path, monkeypatch):
    make_pending_note(store, "n1")
    session = FakeSession(raw=raw_payload("n1", images=2))

    def fake_download(item, assets_dir, **kwargs):
        if item.filename == "image_01.jpg":
            item.download_status = "COMPLETED"
            item.size_bytes = 3
            item.checksum_sha256 = "abc"
            item.local_path = str(assets_dir / item.filename)
        else:
            item.download_status = "FAILED"
            item.error = "HTTP 500"
            raise MediaDownloadError("HTTP 500")

    monkeypatch.setattr(sync, "download_media_item", fake_download)

    outcome = sync._ingest_note(session, store, store.get_note("n1"), tmp_path)

    assert outcome == "PARTIAL"
    state = store.get_note("n1")
    assert state.status is NoteStatus.MEDIA_PARTIAL
    assert state.last_error_status == "MEDIA_DOWNLOAD_FAILED"
    assert state.last_failure_stage == "MEDIA"
    entries = {e["filename"]: e["status"] for e in state.media_state}
    assert entries == {"image_01.jpg": "COMPLETED", "image_02.jpg": "FAILED"}
    note_dir = tmp_path / "n1"
    assert (note_dir / "canonical.json").exists()
    assert not (note_dir / ".complete").exists()  # partial is never sealed


def test_ingest_note_media_complete(store: StateStore, tmp_path: Path, monkeypatch):
    make_pending_note(store, "n1")
    session = FakeSession(raw=raw_payload("n1", images=2))

    def fake_download(item, assets_dir, **kwargs):
        target = assets_dir / item.filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"xyz")
        item.download_status = "COMPLETED"
        item.size_bytes = 3
        item.checksum_sha256 = "abc"
        item.local_path = str(target)

    monkeypatch.setattr(sync, "download_media_item", fake_download)

    outcome = sync._ingest_note(session, store, store.get_note("n1"), tmp_path)
    assert outcome == "COMPLETE"
    assert store.get_note("n1").status is NoteStatus.COMPLETE


# -- failure routing ------------------------------------------------------------


def test_ingest_note_network_failure_is_retryable(store: StateStore, tmp_path: Path):
    make_pending_note(store, "n1")
    session = FakeSession(error=NetworkAcquisitionError("timeout"))

    outcome = sync._ingest_note(session, store, store.get_note("n1"), tmp_path)

    assert outcome == "FAILED"
    state = store.get_note("n1")
    assert state.status is NoteStatus.RETRYABLE_FAILED
    assert state.last_error_status == "NETWORK_ERROR"
    assert state.last_failure_stage == "FETCH"
    assert state.attempt_count == 1
    assert not (tmp_path / "n1").exists()  # nothing written on fetch failure


def test_ingest_note_content_unavailable_is_final(store: StateStore, tmp_path: Path):
    make_pending_note(store, "n1")
    session = FakeSession(error=ContentUnavailableError("deleted"))

    outcome = sync._ingest_note(session, store, store.get_note("n1"), tmp_path)

    assert outcome == "FAILED"
    state = store.get_note("n1")
    assert state.status is NoteStatus.FINAL_FAILED
    assert state.last_error_status == "CONTENT_UNAVAILABLE"


def test_ingest_note_auth_error_aborts_run(store: StateStore, tmp_path: Path):
    make_pending_note(store, "n1")
    session = FakeSession(error=AuthRequiredError("guest session"))

    outcome = sync._ingest_note(session, store, store.get_note("n1"), tmp_path)

    assert outcome == "ABORT"
    state = store.get_note("n1")
    assert state.status is NoteStatus.RETRYABLE_FAILED
    assert state.last_error_status == "AUTH_REQUIRED"


def test_ingest_note_identity_mismatch_is_normalize_failure(store: StateStore, tmp_path: Path):
    make_pending_note(store, "n1")
    session = FakeSession(raw=raw_payload("other_note"))  # server returned someone else

    outcome = sync._ingest_note(session, store, store.get_note("n1"), tmp_path)

    assert outcome == "FAILED"
    state = store.get_note("n1")
    assert state.status is NoteStatus.RETRYABLE_FAILED
    assert state.last_error_status == "PARSE_FAILED"
    assert state.last_failure_stage == "NORMALIZE"


def test_ref_from_state_falls_back_to_bare_url(store: StateStore):
    store.upsert_note(FavoriteRef(note_id="n1", source_url="https://www.xiaohongshu.com/explore/n1"))
    note = store.get_note("n1")
    ref = sync._ref_from_state(note)
    assert ref.note_id == "n1"
    assert ref.source_url.endswith("/explore/n1")
    assert ref.xsec_token is None


# -- collector pure helpers -------------------------------------------------------


def test_build_favorite_refs_tokens_and_dedup():
    notes = [
        {"note_id": "a", "xsec_token": "tok_a", "display_title": "A", "user": {"nickname": "U"}},
        {"note_id": "a", "xsec_token": "tok_a"},  # duplicate within page
        {"note_id": "b"},  # no token -> bare URL
    ]
    refs = build_favorite_refs(notes)
    assert [r.note_id for r in refs] == ["a", "b"]
    assert "xsec_token=tok_a" in refs[0].source_url
    assert "xsec_token" not in refs[1].source_url
    assert refs[0].xsec_token == "tok_a"

    seen_again = {"a"}  # cross-page dedup via shared seen set
    refs2 = build_favorite_refs([{"note_id": "a"}, {"note_id": "c"}], seen=seen_again)
    assert [r.note_id for r in refs2] == ["c"]


def test_feed_payload_has_note_identity():
    payload = {"data": {"items": [{"id": "n1", "note_card": {"note_id": "n1"}}]}}
    assert feed_payload_has_note(payload, "n1") is True
    assert feed_payload_has_note(payload, "n2") is False
    assert feed_payload_has_note({"data": {"items": []}}, "n1") is False
    assert feed_payload_has_note({"data": None}, "n1") is False


def test_session_fetch_note_requires_favorite_ref():
    session = CollectorSession.__new__(CollectorSession)  # no context needed for the type check
    with pytest.raises(TypeError, match="FavoriteRef"):
        session.fetch_note("6aa174a8000000002901b985")


# ======================================================================
# Media resume pipeline (gap #1)
# ======================================================================


def make_media_partial(store: StateStore, tmp_path: Path, note_id: str = "n1"):
    """A note resting in MEDIA_PARTIAL: raw.json on disk, image_01 verified,
    image_02 failed (the 1/2-timeout shape seen in the real run)."""
    ref = FavoriteRef(
        note_id=note_id,
        source_url=f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=tok&xsec_source=pc_fav",
        xsec_token="tok",
    )
    store.upsert_note(ref)
    for status in ("PENDING", "FETCHING", "DETAIL_SUCCESS", "MEDIA_SYNCING"):
        store.transition(note_id, NoteStatus(status))
    store.record_media_result(
        note_id, "image_01.jpg", status="COMPLETED",
        url="https://cdn/1.jpg", size_bytes=3, checksum_sha256="x",
    )
    store.record_media_result(note_id, "image_02.jpg", status="FAILED", url="https://cdn/2.jpg", error="timed out")
    note_dir = tmp_path / note_id
    (note_dir / "assets").mkdir(parents=True, exist_ok=True)
    (note_dir / "raw.json").write_text(json.dumps(raw_payload(note_id, images=2)))
    (note_dir / "assets" / "image_01.jpg").write_bytes(b"abc")
    store.transition(
        note_id, NoteStatus.MEDIA_PARTIAL,
        error_status="MEDIA_DOWNLOAD_FAILED", error_message="1/2 media files failed",
        failure_stage="MEDIA",
    )
    return store.get_note(note_id)


def test_media_resume_completes_partial(store: StateStore, tmp_path: Path, monkeypatch):
    note = make_media_partial(store, tmp_path)

    def fake_download(item, assets_dir, **kwargs):
        assert item.filename == "image_02.jpg", "must not re-download the verified file"
        target = assets_dir / item.filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"xy")
        item.download_status = "COMPLETED"
        item.size_bytes = 2
        item.checksum_sha256 = "h2"
        item.local_path = str(target)

    monkeypatch.setattr(sync, "download_media_item", fake_download)

    outcome = sync._resume_note_media(store, note, tmp_path)

    assert outcome == "COMPLETE"
    state = store.get_note("n1")
    assert state.status is NoteStatus.COMPLETE
    assert (tmp_path / "n1" / ".complete").exists()
    entries = {e["filename"]: e["status"] for e in state.media_state}
    assert entries == {"image_01.jpg": "COMPLETED", "image_02.jpg": "COMPLETED"}


def test_media_resume_missing_raw_demotes_to_pending(store: StateStore, tmp_path: Path):
    make_pending_note(store, "n1")
    for status in ("FETCHING", "DETAIL_SUCCESS"):
        store.transition("n1", NoteStatus(status))
    note = store.get_note("n1")

    outcome = sync._resume_note_media(store, note, tmp_path)

    assert outcome == "DEMOTED"
    state = store.get_note("n1")
    assert state.status is NoteStatus.PENDING  # evidence invalidation -> refetch path
    assert state.attempt_count == 1  # demotion itself costs no attempt


def test_media_resume_corrupt_raw_demotes_to_pending(store: StateStore, tmp_path: Path):
    note = make_media_partial(store, tmp_path)
    (tmp_path / "n1" / "raw.json").write_text("{not json")

    outcome = sync._resume_note_media(store, note, tmp_path)

    assert outcome == "DEMOTED"
    assert store.get_note("n1").status is NoteStatus.PENDING


def test_media_resume_queue_processed_by_run(store: StateStore, tmp_path: Path, monkeypatch):
    make_media_partial(store, tmp_path)

    def fake_download(item, assets_dir, **kwargs):
        target = assets_dir / item.filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"xy")
        item.download_status = "COMPLETED"
        item.size_bytes = 2
        item.checksum_sha256 = "h2"
        item.local_path = str(target)

    monkeypatch.setattr(sync, "download_media_item", fake_download)
    collector = FakeCollector([PageResult(items=[], has_more=False, completion_proof="EXPLICIT_EMPTY_STATE_VERIFIED_ON_PAGE")])
    # No fetch work; the partial note must be closed by the resume pass alone.
    exit_code = sync.run_sync(collector, store, tmp_path)
    assert exit_code == 0
    assert store.get_note("n1").status is NoteStatus.COMPLETE


# ======================================================================
# Media commit verification (gap #4)
# ======================================================================


def test_complete_blocked_when_file_missing_on_disk(store: StateStore, tmp_path: Path, monkeypatch):
    make_pending_note(store, "n1")
    session = FakeSession(raw=raw_payload("n1", images=1))

    def lying_download(item, assets_dir, **kwargs):
        # Claims success but never writes the file: commit verification must catch it.
        item.download_status = "COMPLETED"
        item.size_bytes = 3
        item.checksum_sha256 = "abc"

    monkeypatch.setattr(sync, "download_media_item", lying_download)

    outcome = sync._ingest_note(session, store, store.get_note("n1"), tmp_path)

    assert outcome == "PARTIAL"
    state = store.get_note("n1")
    assert state.status is NoteStatus.MEDIA_PARTIAL
    assert not (tmp_path / "n1" / ".complete").exists()


def test_complete_blocked_when_file_truncated_on_disk(store: StateStore, tmp_path: Path, monkeypatch):
    make_pending_note(store, "n1")
    session = FakeSession(raw=raw_payload("n1", images=1))

    def truncated_download(item, assets_dir, **kwargs):
        item.download_status = "COMPLETED"
        item.size_bytes = 100
        item.checksum_sha256 = "abc"
        assets_dir.mkdir(parents=True, exist_ok=True)
        (assets_dir / item.filename).write_bytes(b"truncated")

    monkeypatch.setattr(sync, "download_media_item", truncated_download)

    outcome = sync._ingest_note(session, store, store.get_note("n1"), tmp_path)

    assert outcome == "PARTIAL"
    state = store.get_note("n1")
    assert state.status is NoteStatus.MEDIA_PARTIAL
    assert not (tmp_path / "n1" / ".complete").exists()


# ======================================================================
# COMPLETE artifact guard (gap #3)
# ======================================================================


def _ingest_zero_media_complete(store: StateStore, tmp_path: Path, note_id: str = "n1") -> Path:
    make_pending_note(store, note_id)
    session = FakeSession(raw=raw_payload(note_id))
    assert sync._ingest_note(session, store, store.get_note(note_id), tmp_path) == "COMPLETE"
    return tmp_path / note_id


def test_complete_guard_repairs_missing_marker(store: StateStore, tmp_path: Path):
    note_dir = _ingest_zero_media_complete(store, tmp_path)
    (note_dir / ".complete").unlink()  # crash window: DB says COMPLETE, seal missing

    repaired, demoted = sync._guard_complete(store, tmp_path)

    assert (repaired, demoted) == (1, 0)
    assert (note_dir / ".complete").exists()  # repaired, not re-ingested
    assert store.get_note("n1").status is NoteStatus.COMPLETE


def test_complete_guard_demotes_missing_artifacts(store: StateStore, tmp_path: Path):
    note_dir = _ingest_zero_media_complete(store, tmp_path)
    (note_dir / "raw.json").unlink()  # real evidence loss

    repaired, demoted = sync._guard_complete(store, tmp_path)

    assert (repaired, demoted) == (0, 1)
    assert store.get_note("n1").status is NoteStatus.PENDING
    assert not (note_dir / ".complete").exists()


def test_complete_guard_demotes_missing_canonical(store: StateStore, tmp_path: Path):
    note_dir = _ingest_zero_media_complete(store, tmp_path)
    (note_dir / "canonical.json").unlink()

    repaired, demoted = sync._guard_complete(store, tmp_path)

    assert (repaired, demoted) == (0, 1)
    assert store.get_note("n1").status is NoteStatus.PENDING
    assert not (note_dir / ".complete").exists()


def test_complete_guard_demotes_missing_post_markdown(store: StateStore, tmp_path: Path):
    note_dir = _ingest_zero_media_complete(store, tmp_path)
    (note_dir / "post.md").unlink()

    repaired, demoted = sync._guard_complete(store, tmp_path)

    assert (repaired, demoted) == (0, 1)
    assert store.get_note("n1").status is NoteStatus.PENDING
    assert not (note_dir / ".complete").exists()


def test_complete_guard_demotes_media_size_mismatch(store: StateStore, tmp_path: Path, monkeypatch):
    make_pending_note(store, "n1")
    session = FakeSession(raw=raw_payload("n1", images=1))

    def fake_download(item, assets_dir, **kwargs):
        item.download_status = "COMPLETED"
        item.size_bytes = 10
        item.checksum_sha256 = "abc"
        item.local_path = str(assets_dir / item.filename)
        assets_dir.mkdir(parents=True, exist_ok=True)
        (assets_dir / item.filename).write_bytes(b"x" * 10)
        return item

    monkeypatch.setattr(sync, "download_media_item", fake_download)
    assert sync._ingest_note(session, store, store.get_note("n1"), tmp_path) == "COMPLETE"
    assert store.get_note("n1").status is NoteStatus.COMPLETE

    note_dir = tmp_path / "n1"
    (note_dir / "assets" / "image_01.jpg").write_bytes(b"short")

    repaired, demoted = sync._guard_complete(store, tmp_path)
    assert (repaired, demoted) == (0, 1)
    assert store.get_note("n1").status is NoteStatus.PENDING
    assert not (note_dir / ".complete").exists()


def test_complete_guard_skips_intact_notes(store: StateStore, tmp_path: Path):
    _ingest_zero_media_complete(store, tmp_path)
    assert sync._guard_complete(store, tmp_path) == (0, 0)


# ======================================================================
# Enumeration completion proof & exit codes (gap #2)
# ======================================================================


class FakeRunSession:
    def __init__(self, pages: list[PageResult]) -> None:
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def iter_favorites(self, limit=None):
        yield from self.pages

    def fetch_note(self, ref: FavoriteRef) -> dict:
        return raw_payload(ref.note_id)


class FakeCollector:
    def __init__(self, pages: list[PageResult]) -> None:
        self.pages = pages
        self._session = FakeRunSession(pages)

    def session(self):
        return self._session


def _ref(note_id: str) -> FavoriteRef:
    return FavoriteRef(
        note_id=note_id,
        source_url=f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=tok&xsec_source=pc_fav",
        xsec_token="tok",
    )


def test_run_sync_exit_0_with_server_termination_proof(store: StateStore, tmp_path: Path):
    collector = FakeCollector([PageResult(items=[_ref("n1")], has_more=False)])
    assert sync.run_sync(collector, store, tmp_path) == 0
    assert store.get_note("n1").status is NoteStatus.COMPLETE


def test_run_sync_exit_3_without_proof(store: StateStore, tmp_path: Path):
    # Enumeration ended without server termination evidence (stall/limit):
    # everything is COMPLETE, but full coverage is unproven -> never exit 0.
    collector = FakeCollector([PageResult(items=[_ref("n1")], has_more=None)])
    assert sync.run_sync(collector, store, tmp_path) == 3
    assert store.get_note("n1").status is NoteStatus.COMPLETE


def test_run_sync_slice_leaves_pending_exit_3(store: StateStore, tmp_path: Path):
    collector = FakeCollector([PageResult(items=[_ref("n1"), _ref("n2")], has_more=True)])
    assert sync.run_sync(collector, store, tmp_path, max_notes=1) == 3
    counts = store.status_counts()
    assert counts["COMPLETE"] == 1
    assert counts["PENDING"] + counts["DISCOVERED"] == 1


def test_run_sync_enum_error_exit_2(store: StateStore, tmp_path: Path):
    class ErroringSession(FakeRunSession):
        def iter_favorites(self, limit=None):
            yield PageResult(items=[_ref("n1")], has_more=False)
            raise AuthRequiredError("session expired")

    class ErroringCollector(FakeCollector):
        def session(self):
            return ErroringSession(self.pages)

    collector = ErroringCollector([])
    assert sync.run_sync(collector, store, tmp_path) == 2


# ======================================================================
# CLI --max-notes safety (gap #5)
# ======================================================================


def test_max_notes_argument_parsing():
    from xhs_ingest.cli import _parse_max_notes

    assert _parse_max_notes("all") is None  # full sync is an explicit opt-in
    assert _parse_max_notes("10") == 10
    for bad in ("abc", "0", "-3"):
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_max_notes(bad)
