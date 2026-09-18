"""Unit tests for Phase C.3 DigestWriter (Module 3) — crash-safe publication model.

Publication model under test: immutable generation directories + ONE atomic
current.json pointer replacement as the sole publication commit point.

NOTE ON CRASH TESTS: simulated-failure tests verify the designed commit boundary
(where the single atomic pointer replacement happens), NOT literal SIGKILL or
power-loss atomicity. The guarantees are those of POSIX rename + fsync.

Covers (per remediation spec):
A.  ValidationResult consistency: PASS+errors, count mismatches, forged excerpts.
B.  Generation publication: digest.md + manifest.json, exact pointer target,
    verbatim_quote JSON round-trip fidelity.
C.  Crash/failure behavior: failure before pointer commit leaves previous
    generation current and intact; successful switch references complete generation.
D.  Zero-valid rerun: INCOMPLETE manifest-only generation becomes current,
    no digest.md, previous generation intact but no longer current.
E.  Integrity: returned/referenced hashes equal bytes reread from disk;
    boundary/symlink/unknown-file protections.
F.  Reproducibility: deterministic generation identity and byte-identical artifacts
    for fixed generated_at + identical inputs.
"""

import hashlib
import json
import os
from pathlib import Path
import pytest

from xhs_knowledge.contracts import (
    BoundaryViolationError,
    CollectionMembership,
    DigestRequest,
    EvidenceBundle,
    EvidenceExcerpt,
    SelectedNote,
    SelectionConfig,
    SourceConfig,
)
from xhs_knowledge.validator import (
    ValidationError,
    ValidationResult,
)
from xhs_knowledge.writer import (
    CURRENT_POINTER_FILENAME,
    GENERATION_MANIFEST_FILENAME,
    GENERATION_MARKDOWN_FILENAME,
    GENERATIONS_DIRNAME,
    DigestWriter,
    DigestWriterResult,
)

FIXED_TIME = "2026-09-18T00:10:00Z"
ARTIFACT_KEY = "2026-09-17_ai_daily"


@pytest.fixture
def sample_request() -> DigestRequest:
    return DigestRequest(
        digest_name="ai_daily",
        target_date="2026-09-17",
        source=SourceConfig(collections=["coding"]),
        selection=SelectionConfig(max_notes=5),
    )


@pytest.fixture
def sample_bundle() -> EvidenceBundle:
    note1 = SelectedNote(
        note_id="6aa62ab4000000001001fc4f",
        title="一个月 20 刀的 Antigravity CLI，可能被很多人低估了",
        author_name="艾康的AI自留地",
        primary_collection="coding",
        vault_collection_position=1,
        memberships=[
            CollectionMembership(
                collection_id="col_coding",
                collection_name="coding",
                vault_collection_position=1,
            )
        ],
        content_text="一个月 20 刀的 Antigravity CLI，可能被很多人低估了。\n\n谷歌 Gemini CLI 6月18日停止个人账号支持，推荐Antigravity CLI替代...",
        file_path="notes/6aa62ab4000000001001fc4f.md",
        file_sha256="sha256_note1_hash",
    )
    note2 = SelectedNote(
        note_id="6a97a054000000002802f761",
        title="Agent面试连环追问，你能坚持到第几关",
        author_name="J同学qej",
        primary_collection="coding",
        vault_collection_position=2,
        memberships=[
            CollectionMembership(
                collection_id="col_coding",
                collection_name="coding",
                vault_collection_position=2,
            )
        ],
        content_text="面试官连环追问核心在于评测集的代表性与可复现性。\n\n需要准备端到端测试。",
        file_path="notes/6a97a054000000002802f761.md",
        file_sha256="sha256_note2_hash",
    )
    return EvidenceBundle(
        bundle_id="bundle_20260917_ai_daily",
        request_fingerprint="req_fp_123456",
        created_at="2026-09-18T00:00:00Z",
        total_notes=2,
        notes=[note1, note2],
    )


# =============================================================================
# Helpers
# =============================================================================


def valid_excerpt() -> EvidenceExcerpt:
    return EvidenceExcerpt(
        note_id="6aa62ab4000000001001fc4f",
        source_file_sha256="sha256_note1_hash",
        verbatim_quote="一个月 20 刀的 Antigravity CLI，可能被很多人低估了。",
    )


def pass_result(excerpts: list[EvidenceExcerpt] | None = None) -> ValidationResult:
    excerpts = excerpts if excerpts is not None else [valid_excerpt()]
    return ValidationResult(
        status="PASS",
        validated_excerpts=excerpts,
        omitted_excerpts=[],
        validated_count=len(excerpts),
        failed_count=0,
        errors=[],
    )


def zero_valid_result(message: str = "Zero evidence.") -> ValidationResult:
    return ValidationResult(
        status="FAILED",
        validated_excerpts=[],
        omitted_excerpts=[],
        validated_count=0,
        failed_count=0,
        errors=[ValidationError(code="EMPTY_EVIDENCE", note_id="", message=message)],
    )


def artifact_dir_of(tmp_path: Path) -> Path:
    return tmp_path / "digests" / ARTIFACT_KEY


def load_pointer(tmp_path: Path) -> dict:
    pointer_file = artifact_dir_of(tmp_path) / CURRENT_POINTER_FILENAME
    return json.loads(pointer_file.read_text(encoding="utf-8"))


def tree_snapshot(root: Path) -> dict[str, bytes]:
    """Maps relative file path -> file bytes for every regular file under root."""
    snapshot: dict[str, bytes] = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            snapshot[str(path.relative_to(root))] = path.read_bytes()
    return snapshot


def hidden_leftovers(root: Path) -> list[str]:
    """All hidden staging/tmp leftovers under root (should be none after any run)."""
    if not root.exists():
        return []
    return [str(p.relative_to(root)) for p in root.rglob(".*") if p.name != ".DS_Store" and p.exists()]


# =============================================================================
# A. ValidationResult consistency (P0-A)
# =============================================================================


def test_pass_with_nonempty_errors_is_rejected_before_publication(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """A1 (required regression): status=PASS + valid excerpt + non-empty errors must be rejected."""
    writer = DigestWriter(vault_dir=tmp_path)

    inconsistent = ValidationResult(
        status="PASS",
        validated_excerpts=[valid_excerpt()],
        omitted_excerpts=[],
        validated_count=1,
        failed_count=0,
        errors=[ValidationError(code="QUOTE_NOT_FOUND", note_id="x", message="stale error")],
    )

    with pytest.raises(ValueError, match="PASS but errors is non-empty"):
        writer.write(sample_request, sample_bundle, inconsistent)

    # Rejected BEFORE publication: nothing was created on disk.
    assert not (tmp_path / "digests").exists()


def test_failed_with_empty_errors_is_rejected(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """A1b: status=FAILED with zero errors is an inconsistent state and must be rejected."""
    writer = DigestWriter(vault_dir=tmp_path)

    inconsistent = ValidationResult(
        status="FAILED",
        validated_excerpts=[],
        omitted_excerpts=[],
        validated_count=0,
        failed_count=0,
        errors=[],
    )

    with pytest.raises(ValueError, match="FAILED but errors is empty"):
        writer.write(sample_request, sample_bundle, inconsistent)

    assert not (tmp_path / "digests").exists()


def test_unknown_status_is_rejected(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """A1c: statuses other than PASS/FAILED are rejected."""
    writer = DigestWriter(vault_dir=tmp_path)

    bogus = ValidationResult(
        status="PARTIAL",
        validated_excerpts=[valid_excerpt()],
        omitted_excerpts=[],
        validated_count=1,
        failed_count=0,
        errors=[],
    )

    with pytest.raises(ValueError, match="invalid status"):
        writer.write(sample_request, sample_bundle, bogus)

    assert not (tmp_path / "digests").exists()


def test_validated_count_mismatch_is_rejected(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """A2: validated_count != len(validated_excerpts) is rejected."""
    writer = DigestWriter(vault_dir=tmp_path)

    forged = ValidationResult(
        status="PASS",
        validated_excerpts=[valid_excerpt()],
        omitted_excerpts=[],
        validated_count=99,
        failed_count=0,
        errors=[],
    )

    with pytest.raises(ValueError, match="validated_count"):
        writer.write(sample_request, sample_bundle, forged)

    assert not (tmp_path / "digests").exists()


def test_failed_count_mismatch_is_rejected(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """A3: failed_count != len(omitted_excerpts) is rejected."""
    writer = DigestWriter(vault_dir=tmp_path)

    forged = ValidationResult(
        status="FAILED",
        validated_excerpts=[valid_excerpt()],
        omitted_excerpts=[],
        validated_count=1,
        failed_count=5,
        errors=[ValidationError(code="X", note_id="", message="m")],
    )

    with pytest.raises(ValueError, match="failed_count"):
        writer.write(sample_request, sample_bundle, forged)

    assert not (tmp_path / "digests").exists()


def test_forged_validated_excerpts_are_rejected(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """A4: every validated excerpt must re-pass structural provenance against the bundle."""
    writer = DigestWriter(vault_dir=tmp_path)

    # Forged quote: not an exact substring of note content_text
    forged_quote = EvidenceExcerpt(
        note_id="6aa62ab4000000001001fc4f",
        source_file_sha256="sha256_note1_hash",
        verbatim_quote="完全捏造且不存在的虚假文本",
    )
    with pytest.raises(ValueError, match="QUOTE_NOT_FOUND"):
        writer.write(sample_request, sample_bundle, pass_result([forged_quote]))

    # Forged source hash
    forged_hash = EvidenceExcerpt(
        note_id="6aa62ab4000000001001fc4f",
        source_file_sha256="tampered_sha256_value",
        verbatim_quote="一个月 20 刀的 Antigravity CLI，可能被很多人低估了。",
    )
    with pytest.raises(ValueError, match="SOURCE_HASH_MISMATCH"):
        writer.write(sample_request, sample_bundle, pass_result([forged_hash]))

    # Foreign note_id not in bundle
    foreign_note = EvidenceExcerpt(
        note_id="foreign_ghost_note",
        source_file_sha256="sha256_note1_hash",
        verbatim_quote="一个月 20 刀的 Antigravity CLI，可能被很多人低估了。",
    )
    with pytest.raises(ValueError, match="INVALID_NOTE_REFERENCE"):
        writer.write(sample_request, sample_bundle, pass_result([foreign_note]))

    assert not (tmp_path / "digests").exists()


# =============================================================================
# B. Generation publication
# =============================================================================


def test_successful_generation_contains_digest_and_manifest(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """B5: a COMPLETE generation directory contains exactly digest.md and manifest.json."""
    writer = DigestWriter(vault_dir=tmp_path)
    res = writer.write(sample_request, sample_bundle, pass_result(), generated_at=FIXED_TIME)

    assert isinstance(res, DigestWriterResult)
    assert res.artifact_status == "COMPLETE"
    assert res.content_mode == "VERIFIED_SOURCE_EXCERPTS"
    assert res.verified_excerpts_count == 1
    assert res.omitted_excerpts_count == 0

    gen_dir = artifact_dir_of(tmp_path) / GENERATIONS_DIRNAME / res.generation_id
    assert gen_dir.is_dir()
    assert {p.name for p in gen_dir.iterdir()} == {GENERATION_MARKDOWN_FILENAME, GENERATION_MANIFEST_FILENAME}
    assert res.generation_dir == gen_dir
    assert res.markdown_path == gen_dir / GENERATION_MARKDOWN_FILENAME
    assert res.manifest_path == gen_dir / GENERATION_MANIFEST_FILENAME
    assert res.markdown_path.exists() and res.manifest_path.exists()

    md_content = res.markdown_path.read_text(encoding="utf-8")
    assert 'digest_name: "ai_daily"' in md_content
    assert 'target_date: "2026-09-17"' in md_content
    assert 'artifact_status: "COMPLETE"' in md_content
    assert 'content_mode: "VERIFIED_SOURCE_EXCERPTS"' in md_content
    assert "### [[6aa62ab4000000001001fc4f|一个月 20 刀的 Antigravity CLI，可能被很多人低估了]] — *艾康的AI自留地*" in md_content
    assert '- > "一个月 20 刀的 Antigravity CLI，可能被很多人低估了。"' in md_content

    manifest = json.loads(res.manifest_path.read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == "1.0"
    assert manifest["digest_name"] == "ai_daily"
    assert manifest["target_date"] == "2026-09-17"
    assert manifest["source_collection"] == "coding"
    assert manifest["generated_at"] == FIXED_TIME
    assert manifest["bundle_id"] == "bundle_20260917_ai_daily"
    assert manifest["request_fingerprint"] == "req_fp_123456"
    assert manifest["bundle_content_hash"] == sample_bundle.bundle_content_hash
    assert len(manifest["inputs"]) == 2
    assert manifest["inputs"][0]["note_id"] == "6aa62ab4000000001001fc4f"
    assert manifest["verified_excerpts_count"] == 1
    assert manifest["omitted_excerpts_count"] == 0
    assert manifest["artifact_status"] == "COMPLETE"
    assert manifest["content_mode"] == "VERIFIED_SOURCE_EXCERPTS"
    assert manifest["errors"] == []
    assert manifest["verified_excerpts"] == [
        {
            "note_id": "6aa62ab4000000001001fc4f",
            "source_file_sha256": "sha256_note1_hash",
            "verbatim_quote": "一个月 20 刀的 Antigravity CLI，可能被很多人低估了。",
        }
    ]

    # The legacy flat-file layout must not reappear.
    assert not (tmp_path / "digests" / "2026-09-17_ai_daily.md").exists()
    assert not (tmp_path / "digests" / "2026-09-17_ai_daily.manifest.json").exists()
    # No staging leftovers.
    assert hidden_leftovers(tmp_path / "digests") == []


def test_current_json_points_to_exact_generation(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """B6: current.json is a regular file pointing at exactly the published generation."""
    writer = DigestWriter(vault_dir=tmp_path)
    res = writer.write(sample_request, sample_bundle, pass_result(), generated_at=FIXED_TIME)

    pointer_file = artifact_dir_of(tmp_path) / CURRENT_POINTER_FILENAME
    assert res.current_pointer_path == pointer_file
    assert pointer_file.is_file()
    assert not pointer_file.is_symlink(), "current.json must be a regular file, never a symlink"

    pointer = load_pointer(tmp_path)
    assert pointer["pointer_version"] == "1.0"
    assert pointer["artifact_key"] == ARTIFACT_KEY
    assert pointer["generation_id"] == res.generation_id
    assert pointer["artifact_status"] == "COMPLETE"
    assert pointer["manifest"] == f"generations/{res.generation_id}/manifest.json"
    assert pointer["markdown"] == f"generations/{res.generation_id}/digest.md"

    # Pointer paths resolve, stay inside the artifact directory, and hit real files.
    adir = artifact_dir_of(tmp_path)
    manifest_path = (adir / pointer["manifest"]).resolve()
    markdown_path = (adir / pointer["markdown"]).resolve()
    assert manifest_path.is_relative_to(adir.resolve())
    assert markdown_path.is_relative_to(adir.resolve())
    assert manifest_path == res.manifest_path
    assert markdown_path == res.markdown_path
    assert manifest_path.is_file() and markdown_path.is_file()


def test_multiline_verbatim_quote_survives_manifest_json_roundtrip(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """B7: raw multiline verbatim_quote is byte-identical after manifest JSON decode."""
    writer = DigestWriter(vault_dir=tmp_path)

    raw_quote = (
        "一个月 20 刀的 Antigravity CLI，可能被很多人低估了。\n\n"
        "谷歌 Gemini CLI 6月18日停止个人账号支持，推荐Antigravity CLI替代..."
    )
    exc = EvidenceExcerpt(
        note_id="6aa62ab4000000001001fc4f",
        source_file_sha256="sha256_note1_hash",
        verbatim_quote=raw_quote,
    )

    res = writer.write(sample_request, sample_bundle, pass_result([exc]), generated_at=FIXED_TIME)

    manifest = json.loads(res.manifest_path.read_text(encoding="utf-8"))
    saved_quote = manifest["verified_excerpts"][0]["verbatim_quote"]

    # JSON escaping is serialization only: decoded value equals the original exactly.
    assert saved_quote == raw_quote
    assert "\n\n" in saved_quote
    assert not saved_quote.startswith(">")
    assert not saved_quote.startswith(" ")
    assert not saved_quote.endswith(" ")


def test_multiline_quote_markdown_blockquote_formatting(
    tmp_path: Path, sample_request: DigestRequest
):
    """Multiline quotes render as clean blockquotes in digest.md while manifest stays raw."""
    writer = DigestWriter(vault_dir=tmp_path)

    multiline_quote = "第一行内容。\n第二行说明。\n第三行总结。"
    custom_note = SelectedNote(
        note_id="6aa62ab4000000001001fc4f",
        title="测试多行笔记",
        author_name="艾康",
        primary_collection="coding",
        vault_collection_position=1,
        memberships=[],
        content_text="引言部分。\n\n第一行内容。\n第二行说明。\n第三行总结。\n\n结尾部分。",
        file_path="notes/custom.md",
        file_sha256="sha256_custom_hash",
    )
    bundle = EvidenceBundle(
        bundle_id="bundle_custom",
        request_fingerprint="req_custom",
        created_at="2026-09-18T00:00:00Z",
        total_notes=1,
        notes=[custom_note],
    )
    exc = EvidenceExcerpt(
        note_id="6aa62ab4000000001001fc4f",
        source_file_sha256="sha256_custom_hash",
        verbatim_quote=multiline_quote,
    )

    res = writer.write(sample_request, bundle, pass_result([exc]), generated_at=FIXED_TIME)
    md = res.markdown_path.read_text(encoding="utf-8")

    assert '- > "第一行内容。\n  > 第二行说明。\n  > 第三行总结。"' in md
    assert json.loads(res.manifest_path.read_text(encoding="utf-8"))["verified_excerpts"][0][
        "verbatim_quote"
    ] == multiline_quote


def test_partial_validation_publishes_incomplete_generation_with_markdown(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """Mixed valid/omitted results publish an INCOMPLETE generation that still has digest.md."""
    writer = DigestWriter(vault_dir=tmp_path)

    bad_exc = EvidenceExcerpt(
        note_id="6a97a054000000002802f761",
        source_file_sha256="sha256_note2_hash",
        verbatim_quote="幻觉内容",
    )
    partial = ValidationResult(
        status="FAILED",
        validated_excerpts=[valid_excerpt()],
        omitted_excerpts=[bad_exc],
        validated_count=1,
        failed_count=1,
        errors=[ValidationError(code="QUOTE_NOT_FOUND", note_id="6a97a054000000002802f761", message="Quote mismatch.")],
    )

    res = writer.write(sample_request, sample_bundle, partial, generated_at=FIXED_TIME)

    assert res.artifact_status == "INCOMPLETE"
    assert res.verified_excerpts_count == 1
    assert res.omitted_excerpts_count == 1
    assert res.markdown_path is not None and res.markdown_path.exists()
    assert 'artifact_status: "INCOMPLETE"' in res.markdown_path.read_text(encoding="utf-8")

    manifest = json.loads(res.manifest_path.read_text(encoding="utf-8"))
    assert manifest["artifact_status"] == "INCOMPLETE"
    assert manifest["errors"][0]["code"] == "QUOTE_NOT_FOUND"
    assert load_pointer(tmp_path)["artifact_status"] == "INCOMPLETE"
    assert load_pointer(tmp_path)["markdown"] is not None


# =============================================================================
# C. Crash/failure behavior at the commit boundary
# =============================================================================


def test_failure_before_pointer_commit_leaves_previous_generation_current(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle, monkeypatch
):
    """C8a: failure at current.json replacement leaves the previous pointer authoritative.

    Verifies the designed commit boundary (single atomic pointer replacement),
    not literal SIGKILL/power-loss atomicity.
    """
    writer = DigestWriter(vault_dir=tmp_path)

    res1 = writer.write(sample_request, sample_bundle, pass_result(), generated_at=FIXED_TIME)
    pointer_before = (artifact_dir_of(tmp_path) / CURRENT_POINTER_FILENAME).read_bytes()
    tree_before = tree_snapshot(artifact_dir_of(tmp_path))
    gen1_dir = res1.generation_dir

    # Different content (different generated_at) -> different deterministic generation.
    orig_replace = os.replace

    def fail_pointer_replace(src, dst):
        if str(dst).endswith(CURRENT_POINTER_FILENAME):
            raise OSError("Simulated crash before pointer commit")
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_pointer_replace)

    with pytest.raises(OSError, match="Simulated crash before pointer commit"):
        writer.write(sample_request, sample_bundle, pass_result(), generated_at="2026-09-18T00:20:00Z")

    monkeypatch.undo()

    # Previous current.json remains authoritative and byte-identical.
    pointer_after = (artifact_dir_of(tmp_path) / CURRENT_POINTER_FILENAME).read_bytes()
    assert pointer_after == pointer_before
    assert json.loads(pointer_after)["generation_id"] == res1.generation_id

    # Previous generation remains fully intact.
    assert tree_snapshot(gen1_dir) == tree_snapshot(res1.generation_dir)
    assert (gen1_dir / GENERATION_MARKDOWN_FILENAME).read_bytes() == tree_before[
        f"{GENERATIONS_DIRNAME}/{res1.generation_id}/{GENERATION_MARKDOWN_FILENAME}"
    ]
    assert (gen1_dir / GENERATION_MANIFEST_FILENAME).is_file()

    # A newer orphan generation may exist, but it is NOT current.
    pointer = json.loads(pointer_after)
    assert pointer["generation_id"] == res1.generation_id
    gen_ids = {p.name for p in (artifact_dir_of(tmp_path) / GENERATIONS_DIRNAME).iterdir()}
    orphan_ids = gen_ids - {res1.generation_id}
    for orphan in orphan_ids:
        assert orphan != pointer["generation_id"]

    # No staging/tmp leftovers; nothing was destroyed.
    assert hidden_leftovers(artifact_dir_of(tmp_path)) == []


def test_failure_during_generation_staging_leaves_previous_generation_current(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle, monkeypatch
):
    """C8b: failure while finalizing the new generation leaves the previous pointer current."""
    writer = DigestWriter(vault_dir=tmp_path)

    res1 = writer.write(sample_request, sample_bundle, pass_result(), generated_at=FIXED_TIME)
    pointer_before = (artifact_dir_of(tmp_path) / CURRENT_POINTER_FILENAME).read_bytes()

    orig_rename = os.rename

    def fail_rename(src, dst):
        raise OSError("Simulated crash while finalizing generation")

    monkeypatch.setattr(os, "rename", fail_rename)

    with pytest.raises(OSError, match="Simulated crash while finalizing generation"):
        writer.write(sample_request, sample_bundle, pass_result(), generated_at="2026-09-18T00:20:00Z")

    monkeypatch.undo()

    # Previous pointer untouched and still authoritative.
    assert (artifact_dir_of(tmp_path) / CURRENT_POINTER_FILENAME).read_bytes() == pointer_before
    assert json.loads(pointer_before)["generation_id"] == res1.generation_id

    # Staging directory was cleaned up; no new generation, no leftovers.
    assert hidden_leftovers(artifact_dir_of(tmp_path)) == []
    gen_ids = {p.name for p in (artifact_dir_of(tmp_path) / GENERATIONS_DIRNAME).iterdir()}
    assert gen_ids == {res1.generation_id}

    # Previous generation intact.
    assert (res1.generation_dir / GENERATION_MARKDOWN_FILENAME).is_file()
    assert (res1.generation_dir / GENERATION_MANIFEST_FILENAME).is_file()


def test_no_backup_cleanup_path_can_destroy_previous_artifact(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle, monkeypatch
):
    """P1: no backup/rollback cleanup exists; failed rerun cannot damage the published artifact."""
    writer = DigestWriter(vault_dir=tmp_path)

    res1 = writer.write(sample_request, sample_bundle, pass_result(), generated_at=FIXED_TIME)
    tree_before = tree_snapshot(artifact_dir_of(tmp_path))

    orig_replace = os.replace

    def fail_pointer_replace(src, dst):
        if str(dst).endswith(CURRENT_POINTER_FILENAME):
            raise OSError("Simulated crash before pointer commit")
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_pointer_replace)
    with pytest.raises(OSError):
        writer.write(sample_request, sample_bundle, pass_result(), generated_at="2026-09-18T00:20:00Z")
    monkeypatch.undo()

    # No backup/rollback/temp mechanism left any trace, and no previous byte changed.
    present = tree_snapshot(artifact_dir_of(tmp_path))
    for rel_path, content in tree_before.items():
        assert present.get(rel_path) == content, f"Previous artifact byte changed: {rel_path}"
    assert not any(".bak_" in name or ".tmp_" in name or ".staging_" in name for name in present)
    assert hidden_leftovers(artifact_dir_of(tmp_path)) == []


def test_successful_pointer_switch_references_fully_prepared_generation(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """C9: after the commit, the pointer references a generation with complete expected files."""
    writer = DigestWriter(vault_dir=tmp_path)

    res = writer.write(sample_request, sample_bundle, pass_result(), generated_at=FIXED_TIME)

    pointer = load_pointer(tmp_path)
    gen_dir = artifact_dir_of(tmp_path) / GENERATIONS_DIRNAME / pointer["generation_id"]

    # Generation contains exactly the expected complete file set.
    assert gen_dir.is_dir()
    assert {p.name for p in gen_dir.iterdir()} == {GENERATION_MARKDOWN_FILENAME, GENERATION_MANIFEST_FILENAME}

    # Markdown and manifest inside the authoritative generation are mutually consistent.
    md_bytes = (gen_dir / GENERATION_MARKDOWN_FILENAME).read_bytes()
    manifest_bytes = (gen_dir / GENERATION_MANIFEST_FILENAME).read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    assert manifest["output_sha256"] == hashlib.sha256(md_bytes).hexdigest()
    assert manifest["artifact_status"] == pointer["artifact_status"] == res.artifact_status
    assert res.markdown_sha256 == hashlib.sha256(md_bytes).hexdigest()
    assert res.manifest_sha256 == hashlib.sha256(manifest_bytes).hexdigest()


# =============================================================================
# D. Zero-valid rerun semantics
# =============================================================================


def test_fail_closed_zero_evidence_first_run(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """Zero validated excerpts on first run: manifest-only INCOMPLETE generation, no digest.md."""
    writer = DigestWriter(vault_dir=tmp_path)

    res = writer.write(sample_request, sample_bundle, zero_valid_result(), generated_at=FIXED_TIME)

    assert res.artifact_status == "INCOMPLETE"
    assert res.markdown_path is None
    assert res.markdown_sha256 == ""
    assert res.verified_excerpts_count == 0

    pointer = load_pointer(tmp_path)
    assert pointer["artifact_status"] == "INCOMPLETE"
    assert pointer["markdown"] is None
    gen_dir = artifact_dir_of(tmp_path) / GENERATIONS_DIRNAME / pointer["generation_id"]
    assert {p.name for p in gen_dir.iterdir()} == {GENERATION_MANIFEST_FILENAME}

    manifest = json.loads((gen_dir / GENERATION_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["artifact_status"] == "INCOMPLETE"
    assert manifest["content_mode"] == "VERIFIED_SOURCE_EXCERPTS"
    assert manifest["verified_excerpts_count"] == 0
    assert manifest["verified_excerpts"] == []
    assert manifest["output_file"] == ""
    assert manifest["errors"][0]["code"] == "EMPTY_EVIDENCE"


def test_zero_valid_rerun_points_current_to_incomplete_generation(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """D10+D11: zero-valid rerun makes a manifest-only INCOMPLETE generation current."""
    writer = DigestWriter(vault_dir=tmp_path)

    # 10. First publish a valid generation.
    res1 = writer.write(sample_request, sample_bundle, pass_result(), generated_at=FIXED_TIME)
    gen1_tree = tree_snapshot(res1.generation_dir)
    gen1_dir = res1.generation_dir

    # 11. Rerun with zero valid evidence.
    res2 = writer.write(
        sample_request, sample_bundle, zero_valid_result(), generated_at="2026-09-18T00:20:00Z"
    )

    # current.json now points to the NEW INCOMPLETE generation.
    pointer = load_pointer(tmp_path)
    assert pointer["generation_id"] == res2.generation_id
    assert pointer["artifact_status"] == "INCOMPLETE"
    assert pointer["markdown"] is None

    gen2_dir = artifact_dir_of(tmp_path) / GENERATIONS_DIRNAME / pointer["generation_id"]
    assert gen2_dir != gen1_dir
    # New generation has manifest.json and NO digest.md.
    assert {p.name for p in gen2_dir.iterdir()} == {GENERATION_MANIFEST_FILENAME}
    assert not (gen2_dir / GENERATION_MARKDOWN_FILENAME).exists()

    manifest2 = json.loads((gen2_dir / GENERATION_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest2["artifact_status"] == "INCOMPLETE"
    assert manifest2["verified_excerpts_count"] == 0

    # Old generation remains intact (immutable history)...
    assert gen1_dir.is_dir()
    assert tree_snapshot(gen1_dir) == gen1_tree
    assert (gen1_dir / GENERATION_MARKDOWN_FILENAME).is_file()
    assert (gen1_dir / GENERATION_MANIFEST_FILENAME).is_file()

    # ...but is no longer current.
    assert pointer["generation_id"] != res1.generation_id
    assert res2.markdown_path is None
    assert hidden_leftovers(artifact_dir_of(tmp_path)) == []


# =============================================================================
# E. Integrity: hashes vs disk, boundary/symlink/unknown-file protections
# =============================================================================


def test_returned_hashes_equal_bytes_reread_from_disk(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """E12+E13: returned markdown/manifest hashes equal SHA256 of bytes reread from disk."""
    writer = DigestWriter(vault_dir=tmp_path)
    res = writer.write(sample_request, sample_bundle, pass_result(), generated_at=FIXED_TIME)

    disk_md = res.markdown_path.read_bytes()
    disk_manifest = res.manifest_path.read_bytes()

    assert hashlib.sha256(disk_md).hexdigest() == res.markdown_sha256
    assert hashlib.sha256(disk_manifest).hexdigest() == res.manifest_sha256

    manifest = json.loads(disk_manifest.decode("utf-8"))
    assert manifest["output_sha256"] == res.markdown_sha256


def test_boundary_violation_escape_attempt(
    tmp_path: Path, sample_bundle: EvidenceBundle
):
    """E14: path traversal in digest_name or target_date raises BoundaryViolationError."""
    writer = DigestWriter(vault_dir=tmp_path)

    # Corrupt an otherwise valid request field to exercise the writer-level guard
    # (DigestRequest.__post_init__ would already reject these values at construction).
    good_req = DigestRequest(
        digest_name="ai_daily",
        target_date="2026-09-17",
        source=SourceConfig(collections=["coding"]),
    )
    object.__setattr__(good_req, "digest_name", "../../escaped_digest")
    with pytest.raises(BoundaryViolationError):
        writer.write(good_req, sample_bundle, pass_result())

    bad_date_req = DigestRequest(
        digest_name="ai_daily",
        target_date="2026-09-17",
        source=SourceConfig(collections=["coding"]),
    )
    object.__setattr__(bad_date_req, "target_date", "2026/09/17")
    with pytest.raises(BoundaryViolationError):
        writer.write(bad_date_req, sample_bundle, pass_result())

    assert not (tmp_path / "digests").exists()


def test_boundary_violation_symlink_rejection(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """E14: symlinks inside the digest publication tree are rejected."""
    writer = DigestWriter(vault_dir=tmp_path)

    outside_dir = tmp_path.parent / "secret_outside_digests"
    outside_dir.mkdir(parents=True, exist_ok=True)

    # Symlinked artifact directory
    adir = tmp_path / "digests" / ARTIFACT_KEY
    adir.parent.mkdir(parents=True, exist_ok=True)
    adir.symlink_to(outside_dir)

    with pytest.raises(BoundaryViolationError, match="(?i)symlink"):
        writer.write(sample_request, sample_bundle, pass_result())

    # Symlinked current.json
    adir.unlink()
    real_adir = tmp_path / "digests" / ARTIFACT_KEY
    real_adir.mkdir(parents=True, exist_ok=True)
    outside_pointer = tmp_path.parent / "outside_current.json"
    outside_pointer.write_text("{}", encoding="utf-8")
    (real_adir / CURRENT_POINTER_FILENAME).symlink_to(outside_pointer)

    with pytest.raises(BoundaryViolationError, match="(?i)symlink"):
        writer.write(sample_request, sample_bundle, pass_result())


def test_refuses_foreign_files_and_directories(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """E14: unknown/unmanaged files block publication; nothing is overwritten."""
    writer = DigestWriter(vault_dir=tmp_path)
    digests_dir = tmp_path / "digests"

    # Foreign regular file occupying the artifact directory path
    digests_dir.mkdir(parents=True, exist_ok=True)
    foreign_file = digests_dir / ARTIFACT_KEY
    foreign_file.write_text("personal notes", encoding="utf-8")
    with pytest.raises(BoundaryViolationError, match="not a directory"):
        writer.write(sample_request, sample_bundle, pass_result())
    assert foreign_file.read_text(encoding="utf-8") == "personal notes"
    foreign_file.unlink()

    # Foreign directory with unknown content
    foreign_dir = digests_dir / ARTIFACT_KEY
    foreign_dir.mkdir()
    (foreign_dir / "my_notes.txt").write_text("do not touch", encoding="utf-8")
    with pytest.raises(BoundaryViolationError, match="unknown/non-digest"):
        writer.write(sample_request, sample_bundle, pass_result())
    assert (foreign_dir / "my_notes.txt").read_text(encoding="utf-8") == "do not touch"
    import shutil as _shutil

    _shutil.rmtree(foreign_dir)

    # Foreign current.json
    managed_dir = digests_dir / ARTIFACT_KEY
    managed_dir.mkdir()
    (managed_dir / CURRENT_POINTER_FILENAME).write_text("not a pointer", encoding="utf-8")
    with pytest.raises(BoundaryViolationError, match="unknown/non-digest"):
        writer.write(sample_request, sample_bundle, pass_result())
    assert (managed_dir / CURRENT_POINTER_FILENAME).read_text(encoding="utf-8") == "not a pointer"


def test_overwrite_false_rejects_republish(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """overwrite=False refuses to replace an existing publication pointer."""
    writer = DigestWriter(vault_dir=tmp_path)
    writer.write(sample_request, sample_bundle, pass_result(), generated_at=FIXED_TIME)

    with pytest.raises(FileExistsError):
        writer.write(
            sample_request, sample_bundle, pass_result(),
            generated_at="2026-09-18T00:20:00Z", overwrite=False,
        )


# =============================================================================
# F. Reproducibility (deterministic generation identity)
# =============================================================================


def test_deterministic_reproducibility_fixed_inputs(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """F15: fixed generated_at + identical inputs reproduce identical generation and bytes."""
    writer = DigestWriter(vault_dir=tmp_path)

    res1 = writer.write(sample_request, sample_bundle, pass_result(), generated_at=FIXED_TIME)
    gen1_tree = tree_snapshot(artifact_dir_of(tmp_path))
    pointer1 = (artifact_dir_of(tmp_path) / CURRENT_POINTER_FILENAME).read_bytes()

    # Identical rerun: the byte-identical deterministic generation is reused, not duplicated.
    res2 = writer.write(sample_request, sample_bundle, pass_result(), generated_at=FIXED_TIME)

    assert res2.generation_id == res1.generation_id
    assert res2.generation_dir == res1.generation_dir
    assert res2.markdown_sha256 == res1.markdown_sha256
    assert res2.manifest_sha256 == res1.manifest_sha256
    assert res2.markdown_path.read_bytes() == res1.markdown_path.read_bytes()
    assert res2.manifest_path.read_bytes() == res1.manifest_path.read_bytes()

    # Exactly one generation exists; no leftovers were created by the reuse path.
    assert tree_snapshot(artifact_dir_of(tmp_path)) == gen1_tree
    assert hidden_leftovers(artifact_dir_of(tmp_path)) == []

    # The committed pointer is byte-identical as well.
    assert (artifact_dir_of(tmp_path) / CURRENT_POINTER_FILENAME).read_bytes() == pointer1


def test_conflicting_existing_generation_content_fails_closed(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """F15b: a deterministic generation path holding different content is never overwritten."""
    writer = DigestWriter(vault_dir=tmp_path)

    res1 = writer.write(sample_request, sample_bundle, pass_result(), generated_at=FIXED_TIME)

    # Tamper with the published immutable generation.
    gen_manifest = res1.generation_dir / GENERATION_MANIFEST_FILENAME
    original = gen_manifest.read_bytes()
    gen_manifest.write_bytes(original.replace(b'"verified_excerpts_count": 1', b'"verified_excerpts_count": 7'))

    with pytest.raises(ValueError, match="Refusing to publish"):
        writer.write(sample_request, sample_bundle, pass_result(), generated_at=FIXED_TIME)

    # Tampered content is left as-is (writer never mutates it); nothing else published.
    assert gen_manifest.read_bytes() != original
    assert hidden_leftovers(artifact_dir_of(tmp_path)) == []
