"""Unit tests for Phase C.2 ProvenanceValidator (Module 2).

Verifies:
1. Valid excerpt passes: note_id matches, sha256 matches, quote is exact substring.
2. Invalid note_id fails with INVALID_NOTE_REFERENCE.
3. Hash mismatch fails with SOURCE_HASH_MISMATCH.
4. Quote mismatch fails with QUOTE_NOT_FOUND.
5. Deterministic validation order and omission handling for multiple excerpts.
6. Absolute immutability of EvidenceBundle and EvidenceExcerpt inputs.
7. Empty evidence list fails closed with EMPTY_EVIDENCE and is_valid=False.
8. None input item is handled safely (omitted with MALFORMED_EXCERPT, no crash).
9. Malformed field types and invalid objects produce MALFORMED_EXCERPT without crashing.
10. Mixed input (valid, None, malformed, invalid quote) partitions cleanly and deterministically.
"""

import copy
import pytest

from xhs_knowledge.contracts import (
    CollectionMembership,
    EvidenceBundle,
    EvidenceExcerpt,
    SelectedNote,
)
from xhs_knowledge.validator import (
    ProvenanceValidator,
    ValidationError,
    ValidationResult,
)


@pytest.fixture
def sample_bundle() -> EvidenceBundle:
    note1 = SelectedNote(
        note_id="note_001",
        title="分布式系统设计",
        author_name="李工",
        primary_collection="tech",
        vault_collection_position=1,
        memberships=[
            CollectionMembership(
                collection_id="col_tech",
                collection_name="tech",
                vault_collection_position=1,
            )
        ],
        content_text="分布式系统的核心是在不可靠网络上保证数据一致性与可用性。\n\nCAP定理是基础。",
        file_path="notes/note_001.md",
        file_sha256="sha256_note1_abc123",
    )
    note2 = SelectedNote(
        note_id="note_002",
        title="命令行工具指南",
        author_name="张工",
        primary_collection="tech",
        vault_collection_position=2,
        memberships=[
            CollectionMembership(
                collection_id="col_tech",
                collection_name="tech",
                vault_collection_position=2,
            )
        ],
        content_text="CLI工具在本地开发中提供确定性与高吞吐。\n\n支持脚本化集成。",
        file_path="notes/note_002.md",
        file_sha256="sha256_note2_def456",
    )
    return EvidenceBundle(
        bundle_id="bundle_val_01",
        request_fingerprint="req_fp_01",
        created_at="2026-09-18T00:00:00Z",
        total_notes=2,
        notes=[note1, note2],
    )


def test_valid_excerpt_passes(sample_bundle: EvidenceBundle):
    """A structurally valid excerpt passes validation with zero errors."""
    validator = ProvenanceValidator()
    excerpt = EvidenceExcerpt(
        note_id="note_001",
        source_file_sha256="sha256_note1_abc123",
        verbatim_quote="CAP定理是基础。",
    )

    result = validator.validate([excerpt], sample_bundle)
    assert result.status == "PASS"
    assert result.is_valid is True
    assert result.validated_count == 1
    assert result.failed_count == 0
    assert len(result.errors) == 0
    assert result.validated_excerpts == [excerpt]
    assert result.omitted_excerpts == []


def test_invalid_note_id_fails(sample_bundle: EvidenceBundle):
    """Excerpt citing a note_id not present in the bundle fails with INVALID_NOTE_REFERENCE."""
    validator = ProvenanceValidator()
    foreign_excerpt = EvidenceExcerpt(
        note_id="foreign_note_999",
        source_file_sha256="sha256_note1_abc123",
        verbatim_quote="CAP定理是基础。",
    )

    result = validator.validate([foreign_excerpt], sample_bundle)
    assert result.status == "FAILED"
    assert result.is_valid is False
    assert result.validated_count == 0
    assert result.failed_count == 1
    assert len(result.errors) == 1
    assert result.errors[0].code == "INVALID_NOTE_REFERENCE"
    assert result.errors[0].note_id == "foreign_note_999"
    assert result.omitted_excerpts == [foreign_excerpt]


def test_hash_mismatch_fails(sample_bundle: EvidenceBundle):
    """Excerpt with mismatched source_file_sha256 fails with SOURCE_HASH_MISMATCH."""
    validator = ProvenanceValidator()
    tampered_hash_excerpt = EvidenceExcerpt(
        note_id="note_001",
        source_file_sha256="sha256_corrupted_hash_xyz",
        verbatim_quote="CAP定理是基础。",
    )

    result = validator.validate([tampered_hash_excerpt], sample_bundle)
    assert result.status == "FAILED"
    assert result.is_valid is False
    assert result.validated_count == 0
    assert result.failed_count == 1
    assert len(result.errors) == 1
    assert result.errors[0].code == "SOURCE_HASH_MISMATCH"
    assert result.errors[0].note_id == "note_001"


def test_quote_not_found_fails(sample_bundle: EvidenceBundle):
    """Excerpt whose verbatim_quote is not an exact substring fails with QUOTE_NOT_FOUND."""
    validator = ProvenanceValidator()
    hallucinated_excerpt = EvidenceExcerpt(
        note_id="note_001",
        source_file_sha256="sha256_note1_abc123",
        verbatim_quote="CAP定理是不适用的，我们不需要一致性。",  # Tampered/hallucinated
    )

    result = validator.validate([hallucinated_excerpt], sample_bundle)
    assert result.status == "FAILED"
    assert result.is_valid is False
    assert result.validated_count == 0
    assert result.failed_count == 1
    assert len(result.errors) == 1
    assert result.errors[0].code == "QUOTE_NOT_FOUND"
    assert result.errors[0].note_id == "note_001"


def test_multiple_excerpts_mixed_and_deterministic(sample_bundle: EvidenceBundle):
    """Validates list with both valid and invalid excerpts; preserves order and partitions accurately."""
    validator = ProvenanceValidator()

    valid1 = EvidenceExcerpt(
        note_id="note_001",
        source_file_sha256="sha256_note1_abc123",
        verbatim_quote="CAP定理是基础。",
    )
    bad_note = EvidenceExcerpt(
        note_id="ghost_001",
        source_file_sha256="sha256_note1_abc123",
        verbatim_quote="CAP定理是基础。",
    )
    valid2 = EvidenceExcerpt(
        note_id="note_002",
        source_file_sha256="sha256_note2_def456",
        verbatim_quote="支持脚本化集成。",
    )
    bad_quote = EvidenceExcerpt(
        note_id="note_002",
        source_file_sha256="sha256_note2_def456",
        verbatim_quote="不存在的句子内容",
    )

    excerpts = [valid1, bad_note, valid2, bad_quote]
    result = validator.validate(excerpts, sample_bundle)

    assert result.status == "FAILED"
    assert result.validated_count == 2
    assert result.failed_count == 2
    assert result.validated_excerpts == [valid1, valid2]
    assert result.omitted_excerpts == [bad_note, bad_quote]
    assert [e.code for e in result.errors] == ["INVALID_NOTE_REFERENCE", "QUOTE_NOT_FOUND"]

    # Test determinism over 10 runs
    for _ in range(10):
        rerun = validator.validate(excerpts, sample_bundle)
        assert rerun.to_dict() == result.to_dict()


def test_validator_input_immutability(sample_bundle: EvidenceBundle):
    """ProvenanceValidator must not mutate EvidenceBundle, SelectedNotes, or EvidenceExcerpts."""
    validator = ProvenanceValidator()
    excerpt = EvidenceExcerpt(
        note_id="note_001",
        source_file_sha256="sha256_note1_abc123",
        verbatim_quote="CAP定理是基础。",
    )
    excerpts = [excerpt]

    bundle_snapshot = copy.deepcopy(sample_bundle.to_dict())
    excerpt_snapshot = copy.deepcopy(excerpt.to_dict())

    result = validator.validate(excerpts, sample_bundle)
    assert result.status == "PASS"

    assert sample_bundle.to_dict() == bundle_snapshot, "EvidenceBundle mutated during validation!"
    assert excerpt.to_dict() == excerpt_snapshot, "EvidenceExcerpt mutated during validation!"


def test_empty_excerpt_list_fails_closed(sample_bundle: EvidenceBundle):
    """Empty excerpt list must fail closed with EMPTY_EVIDENCE and is_valid=False."""
    validator = ProvenanceValidator()

    # Empty list
    res = validator.validate([], sample_bundle)
    assert res.status == "FAILED"
    assert res.is_valid is False
    assert res.validated_count == 0
    assert res.failed_count == 0
    assert len(res.errors) == 1
    assert res.errors[0].code == "EMPTY_EVIDENCE"
    assert res.validated_excerpts == []
    assert res.omitted_excerpts == []
    assert res.to_dict() == {
        "status": "FAILED",
        "validated_count": 0,
        "failed_count": 0,
        "errors": [
            {
                "code": "EMPTY_EVIDENCE",
                "note_id": "",
                "message": "Evidence excerpt list is empty; zero evidence cannot be validated as PASS.",
                "quote_preview": "",
            }
        ],
    }

    # None input
    res_none = validator.validate(None, sample_bundle)
    assert res_none.status == "FAILED"
    assert res_none.is_valid is False
    assert res_none.validated_count == 0
    assert res_none.failed_count == 0
    assert len(res_none.errors) == 1
    assert res_none.errors[0].code == "EMPTY_EVIDENCE"


def test_none_input_item_handled_safely(sample_bundle: EvidenceBundle):
    """None item inside excerpt list must be omitted with MALFORMED_EXCERPT without raising AttributeError."""
    validator = ProvenanceValidator()

    res = validator.validate([None], sample_bundle)
    assert res.status == "FAILED"
    assert res.is_valid is False
    assert res.validated_count == 0
    assert res.failed_count == 1
    assert res.omitted_excerpts == [None]
    assert len(res.errors) == 1
    assert res.errors[0].code == "MALFORMED_EXCERPT"
    assert "None" in res.errors[0].message


def test_malformed_field_type_handled_safely(sample_bundle: EvidenceBundle):
    """Non-string field types and wrong object types must produce MALFORMED_EXCERPT without crashing."""
    validator = ProvenanceValidator()

    # Wrong object types
    res_dict = validator.validate([{"note_id": "note_001"}], sample_bundle)
    assert res_dict.status == "FAILED"
    assert res_dict.is_valid is False
    assert res_dict.failed_count == 1
    assert res_dict.errors[0].code == "MALFORMED_EXCERPT"
    assert "dict" in res_dict.errors[0].message

    res_str = validator.validate(["just a string"], sample_bundle)
    assert res_str.status == "FAILED"
    assert res_str.errors[0].code == "MALFORMED_EXCERPT"

    # Duck-typed object with non-string fields
    class BadExcerpt:
        note_id = 123
        source_file_sha256 = "sha256_note1_abc123"
        verbatim_quote = "CAP定理是基础。"

    res_bad_type = validator.validate([BadExcerpt()], sample_bundle)
    assert res_bad_type.status == "FAILED"
    assert res_bad_type.errors[0].code == "MALFORMED_EXCERPT"

    # Duck-typed object with empty/whitespace fields
    class EmptyFieldExcerpt:
        note_id = "   "
        source_file_sha256 = "sha256_note1_abc123"
        verbatim_quote = "CAP定理是基础。"

    res_empty_field = validator.validate([EmptyFieldExcerpt()], sample_bundle)
    assert res_empty_field.status == "FAILED"
    assert res_empty_field.errors[0].code == "MALFORMED_EXCERPT"


def test_mixed_valid_and_malformed_input(sample_bundle: EvidenceBundle):
    """Safely processes mixed input of valid, None, malformed, and tampered items without crashing."""
    validator = ProvenanceValidator()

    valid1 = EvidenceExcerpt(
        note_id="note_001",
        source_file_sha256="sha256_note1_abc123",
        verbatim_quote="CAP定理是基础。",
    )
    valid2 = EvidenceExcerpt(
        note_id="note_002",
        source_file_sha256="sha256_note2_def456",
        verbatim_quote="支持脚本化集成。",
    )
    bad_hash = EvidenceExcerpt(
        note_id="note_001",
        source_file_sha256="wrong_sha256",
        verbatim_quote="CAP定理是基础。",
    )
    bad_quote = EvidenceExcerpt(
        note_id="note_002",
        source_file_sha256="sha256_note2_def456",
        verbatim_quote="根本不存在的内容",
    )

    candidates = [
        valid1,
        None,
        {"some": "data"},
        bad_hash,
        valid2,
        "raw_string",
        bad_quote,
    ]

    result = validator.validate(candidates, sample_bundle)

    assert result.status == "FAILED"
    assert result.is_valid is False
    assert result.validated_count == 2
    assert result.failed_count == 5
    assert result.validated_excerpts == [valid1, valid2]
    assert result.omitted_excerpts == [
        None,
        {"some": "data"},
        bad_hash,
        "raw_string",
        bad_quote,
    ]
    error_codes = [e.code for e in result.errors]
    assert error_codes == [
        "MALFORMED_EXCERPT",
        "MALFORMED_EXCERPT",
        "SOURCE_HASH_MISMATCH",
        "MALFORMED_EXCERPT",
        "QUOTE_NOT_FOUND",
    ]
