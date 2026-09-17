"""Unit tests for Phase C.2 DigestWriter (Module 3).

Verifies:
1. Complete successful write: writes .md and .manifest.json matching exact schema with artifact_status: COMPLETE.
2. Fail-closed on zero validated excerpts: zero bytes written to .md (file omitted), writes INCOMPLETE manifest.
3. Partial validation result: writes markdown and manifest with artifact_status: INCOMPLETE.
4. Path safety and traversal prevention: illegal characters or escaping names raise BoundaryViolationError.
5. Symlink boundary protection: rejects symlinked target or ancestor paths.
6. Unknown file protection: refuses to overwrite files that do not have digest artifact signatures.
7. Atomic deterministic reproducibility: identical inputs produce bit-for-bit identical artifacts and hashes.
8. Multiline quote formatting: preserves verbatim fidelity and blockquote indentations.
"""

import json
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
    DigestWriter,
    DigestWriterResult,
)


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


def test_successful_digest_write_complete(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """Writes valid markdown and manifest with artifact_status: COMPLETE."""
    writer = DigestWriter(vault_dir=tmp_path)

    excerpt1 = EvidenceExcerpt(
        note_id="6aa62ab4000000001001fc4f",
        source_file_sha256="sha256_note1_hash",
        verbatim_quote="一个月 20 刀的 Antigravity CLI，可能被很多人低估了。",
    )
    excerpt2 = EvidenceExcerpt(
        note_id="6aa62ab4000000001001fc4f",
        source_file_sha256="sha256_note1_hash",
        verbatim_quote="谷歌 Gemini CLI 6月18日停止个人账号支持，推荐Antigravity CLI替代...",
    )
    excerpt3 = EvidenceExcerpt(
        note_id="6a97a054000000002802f761",
        source_file_sha256="sha256_note2_hash",
        verbatim_quote="面试官连环追问核心在于评测集的代表性与可复现性。",
    )

    val_result = ValidationResult(
        status="PASS",
        validated_excerpts=[excerpt1, excerpt2, excerpt3],
        omitted_excerpts=[],
        validated_count=3,
        failed_count=0,
        errors=[],
    )

    fixed_time = "2026-09-18T00:10:00Z"
    result = writer.write(
        request=sample_request,
        bundle=sample_bundle,
        validation_result=val_result,
        generated_at=fixed_time,
    )

    assert result.artifact_status == "COMPLETE"
    assert result.content_mode == "VERIFIED_SOURCE_EXCERPTS"
    assert result.verified_excerpts_count == 3
    assert result.omitted_excerpts_count == 0
    assert result.markdown_path is not None
    assert result.markdown_path.exists()
    assert result.manifest_path.exists()

    # Verify Markdown contents
    md_content = result.markdown_path.read_text(encoding="utf-8")
    assert 'digest_name: "ai_daily"' in md_content
    assert 'target_date: "2026-09-17"' in md_content
    assert 'source_collection: "coding"' in md_content
    assert 'artifact_status: "COMPLETE"' in md_content
    assert 'content_mode: "VERIFIED_SOURCE_EXCERPTS"' in md_content
    assert "### [[6aa62ab4000000001001fc4f|一个月 20 刀的 Antigravity CLI，可能被很多人低估了]] — *艾康的AI自留地*" in md_content
    assert '- > "一个月 20 刀的 Antigravity CLI，可能被很多人低估了。"' in md_content
    assert '- > "谷歌 Gemini CLI 6月18日停止个人账号支持，推荐Antigravity CLI替代..."' in md_content
    assert "### [[6a97a054000000002802f761|Agent面试连环追问，你能坚持到第几关]] — *J同学qej*" in md_content
    assert '- > "面试官连环追问核心在于评测集的代表性与可复现性。"' in md_content
    assert "- **Audit Manifest**: `digests/2026-09-17_ai_daily.manifest.json`" in md_content

    # Verify Manifest contents
    manifest_dict = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest_dict["manifest_version"] == "1.0"
    assert manifest_dict["digest_name"] == "ai_daily"
    assert manifest_dict["target_date"] == "2026-09-17"
    assert manifest_dict["source_collection"] == "coding"
    assert manifest_dict["bundle_id"] == "bundle_20260917_ai_daily"
    assert manifest_dict["artifact_status"] == "COMPLETE"
    assert manifest_dict["content_mode"] == "VERIFIED_SOURCE_EXCERPTS"
    assert manifest_dict["verified_excerpts_count"] == 3
    assert manifest_dict["omitted_excerpts_count"] == 0
    assert manifest_dict["output_file"] == "digests/2026-09-17_ai_daily.md"
    assert manifest_dict["output_sha256"] == result.markdown_sha256
    assert len(manifest_dict["inputs"]) == 2
    assert manifest_dict["inputs"][0]["note_id"] == "6aa62ab4000000001001fc4f"
    assert len(manifest_dict["errors"]) == 0


def test_fail_closed_on_zero_validated_excerpts(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """Zero validated excerpts results in zero bytes written to .md and an INCOMPLETE manifest."""
    writer = DigestWriter(vault_dir=tmp_path)

    val_result = ValidationResult(
        status="FAILED",
        validated_excerpts=[],
        omitted_excerpts=[],
        validated_count=0,
        failed_count=0,
        errors=[ValidationError(code="EMPTY_EVIDENCE", note_id="", message="No excerpts provided.")],
    )

    result = writer.write(
        request=sample_request,
        bundle=sample_bundle,
        validation_result=val_result,
        generated_at="2026-09-18T00:10:00Z",
    )

    assert result.artifact_status == "INCOMPLETE"
    assert result.markdown_path is None
    assert result.markdown_sha256 == ""
    assert result.verified_excerpts_count == 0

    # Ensure markdown file was NOT created on disk
    expected_md = tmp_path / "digests" / "2026-09-17_ai_daily.md"
    assert not expected_md.exists(), "Markdown file must not exist when 0 excerpts validated!"

    # Ensure manifest was created with audit trail
    assert result.manifest_path.exists()
    manifest_dict = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest_dict["artifact_status"] == "INCOMPLETE"
    assert manifest_dict["verified_excerpts_count"] == 0
    assert manifest_dict["output_file"] == ""
    assert manifest_dict["output_sha256"] == ""
    assert len(manifest_dict["errors"]) == 1
    assert manifest_dict["errors"][0]["code"] == "EMPTY_EVIDENCE"


def test_partial_validation_writes_incomplete_artifact(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """Partial validation (some valid, some omitted) writes markdown and manifest with artifact_status: INCOMPLETE."""
    writer = DigestWriter(vault_dir=tmp_path)

    valid_exc = EvidenceExcerpt(
        note_id="6aa62ab4000000001001fc4f",
        source_file_sha256="sha256_note1_hash",
        verbatim_quote="一个月 20 刀的 Antigravity CLI，可能被很多人低估了。",
    )
    bad_exc = EvidenceExcerpt(
        note_id="6a97a054000000002802f761",
        source_file_sha256="sha256_note2_hash",
        verbatim_quote="幻觉内容",
    )

    val_result = ValidationResult(
        status="FAILED",
        validated_excerpts=[valid_exc],
        omitted_excerpts=[bad_exc],
        validated_count=1,
        failed_count=1,
        errors=[ValidationError(code="QUOTE_NOT_FOUND", note_id="6a97a054000000002802f761", message="Quote mismatch.")],
    )

    result = writer.write(
        request=sample_request,
        bundle=sample_bundle,
        validation_result=val_result,
        generated_at="2026-09-18T00:10:00Z",
    )

    assert result.artifact_status == "INCOMPLETE"
    assert result.verified_excerpts_count == 1
    assert result.omitted_excerpts_count == 1
    assert result.markdown_path is not None
    assert result.markdown_path.exists()

    md_content = result.markdown_path.read_text(encoding="utf-8")
    assert 'artifact_status: "INCOMPLETE"' in md_content
    assert '- > "一个月 20 刀的 Antigravity CLI，可能被很多人低估了。"' in md_content

    manifest_dict = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest_dict["artifact_status"] == "INCOMPLETE"
    assert manifest_dict["verified_excerpts_count"] == 1
    assert manifest_dict["omitted_excerpts_count"] == 1
    assert manifest_dict["errors"][0]["code"] == "QUOTE_NOT_FOUND"


def test_boundary_violation_escape_attempt(
    tmp_path: Path, sample_bundle: EvidenceBundle
):
    """Path traversal in digest_name or target_date raises BoundaryViolationError."""
    writer = DigestWriter(vault_dir=tmp_path)

    val_result = ValidationResult(
        status="PASS",
        validated_excerpts=[
            EvidenceExcerpt(
                note_id="6aa62ab4000000001001fc4f",
                source_file_sha256="sha256_note1_hash",
                verbatim_quote="一个月 20 刀的 Antigravity CLI，可能被很多人低估了。",
            )
        ],
        omitted_excerpts=[],
        validated_count=1,
        failed_count=0,
        errors=[],
    )

    # Path traversal in digest_name
    bad_req_name = DigestRequest(
        digest_name="../../escaped_digest",
        target_date="2026-09-17",
        source=SourceConfig(collections=["coding"]),
    )
    with pytest.raises(BoundaryViolationError):
        writer.write(bad_req_name, sample_bundle, val_result)

    # Slashes in target_date (bypassing post_init to test writer-level boundary guard)
    bad_req_date = DigestRequest(
        digest_name="ai_daily",
        target_date="2026-09-17",
        source=SourceConfig(collections=["coding"]),
    )
    object.__setattr__(bad_req_date, "target_date", "2026/09/17")
    with pytest.raises(BoundaryViolationError):
        writer.write(bad_req_date, sample_bundle, val_result)


def test_boundary_violation_symlink_rejection(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """Symlinks inside Vault/digests/ are rejected with BoundaryViolationError."""
    writer = DigestWriter(vault_dir=tmp_path)
    digests_dir = tmp_path / "digests"
    digests_dir.mkdir(parents=True, exist_ok=True)

    outside_file = tmp_path.parent / "secret_outside.md"
    outside_file.write_text("classified", encoding="utf-8")

    symlink_target = digests_dir / "2026-09-17_ai_daily.md"
    symlink_target.symlink_to(outside_file)

    val_result = ValidationResult(
        status="PASS",
        validated_excerpts=[
            EvidenceExcerpt(
                note_id="6aa62ab4000000001001fc4f",
                source_file_sha256="sha256_note1_hash",
                verbatim_quote="一个月 20 刀的 Antigravity CLI，可能被很多人低估了。",
            )
        ],
        omitted_excerpts=[],
        validated_count=1,
        failed_count=0,
        errors=[],
    )

    with pytest.raises(BoundaryViolationError, match="(?i)symlink"):
        writer.write(sample_request, sample_bundle, val_result)


def test_refuse_to_overwrite_unknown_file(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """Refuses to overwrite existing files that lack digest artifact signatures."""
    writer = DigestWriter(vault_dir=tmp_path)
    digests_dir = tmp_path / "digests"
    digests_dir.mkdir(parents=True, exist_ok=True)

    # Place an arbitrary user note
    foreign_file = digests_dir / "2026-09-17_ai_daily.md"
    foreign_file.write_text("# My Personal Handwritten Notes\nDo not delete!", encoding="utf-8")

    val_result = ValidationResult(
        status="PASS",
        validated_excerpts=[
            EvidenceExcerpt(
                note_id="6aa62ab4000000001001fc4f",
                source_file_sha256="sha256_note1_hash",
                verbatim_quote="一个月 20 刀的 Antigravity CLI，可能被很多人低估了。",
            )
        ],
        omitted_excerpts=[],
        validated_count=1,
        failed_count=0,
        errors=[],
    )

    with pytest.raises(BoundaryViolationError, match="Refusing to overwrite unknown/non-digest file"):
        writer.write(sample_request, sample_bundle, val_result)

    # Assert foreign file was NOT modified
    assert foreign_file.read_text(encoding="utf-8") == "# My Personal Handwritten Notes\nDo not delete!"


def test_atomic_deterministic_reproducibility(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """Two executions with identical input produce bit-for-bit identical hashes and contents."""
    writer = DigestWriter(vault_dir=tmp_path)

    val_result = ValidationResult(
        status="PASS",
        validated_excerpts=[
            EvidenceExcerpt(
                note_id="6aa62ab4000000001001fc4f",
                source_file_sha256="sha256_note1_hash",
                verbatim_quote="一个月 20 刀的 Antigravity CLI，可能被很多人低估了。",
            )
        ],
        omitted_excerpts=[],
        validated_count=1,
        failed_count=0,
        errors=[],
    )

    fixed_time = "2026-09-18T00:10:00Z"

    # First run
    res1 = writer.write(sample_request, sample_bundle, val_result, generated_at=fixed_time)

    # Second run (replaces existing known digest artifact)
    res2 = writer.write(sample_request, sample_bundle, val_result, generated_at=fixed_time)

    assert res1.markdown_sha256 == res2.markdown_sha256
    assert res1.manifest_sha256 == res2.manifest_sha256
    assert res1.markdown_path.read_text(encoding="utf-8") == res2.markdown_path.read_text(encoding="utf-8")
    assert res1.manifest_path.read_text(encoding="utf-8") == res2.manifest_path.read_text(encoding="utf-8")


def test_multiline_verbatim_quote_formatting(
    tmp_path: Path, sample_request: DigestRequest, sample_bundle: EvidenceBundle
):
    """Multiline verbatim quotes are formatted cleanly with blockquote indentation."""
    writer = DigestWriter(vault_dir=tmp_path)

    multiline_quote = "第一行内容。\n第二行说明。\n第三行总结。"
    val_result = ValidationResult(
        status="PASS",
        validated_excerpts=[
            EvidenceExcerpt(
                note_id="6aa62ab4000000001001fc4f",
                source_file_sha256="sha256_note1_hash",
                verbatim_quote=multiline_quote,
            )
        ],
        omitted_excerpts=[],
        validated_count=1,
        failed_count=0,
        errors=[],
    )

    res = writer.write(sample_request, sample_bundle, val_result, generated_at="2026-09-18T00:10:00Z")
    md = res.markdown_path.read_text(encoding="utf-8")

    assert '- > "第一行内容。\n  > 第二行说明。\n  > 第三行总结。"' in md
