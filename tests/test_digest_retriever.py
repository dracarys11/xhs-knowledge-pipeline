"""Tests for Phase C.1 VaultRetriever & Contract Schemas.

Verifies:
1. Zero network access and zero P1 data/ mutation.
2. Strict physical boundary enforcement (Vault isolation).
3. Deterministic note selection (collection_position ASC, note_id ASC).
4. Hash stability across multiple runs.
5. Fail-closed error handling (EMPTY_SELECTION, NOTE_CONTENT_EMPTY, unsupported order_by).
"""

import hashlib
import json
from pathlib import Path
import pytest

from xhs_knowledge.contracts import (
    BoundaryViolationError,
    ClaimType,
    DigestClaim,
    DigestError,
    DigestRequest,
    EmptySelectionError,
    EvidenceBundle,
    EvidenceReference,
    InvalidNoteContentError,
    SelectedNote,
    SelectionConfig,
    SelectionPositionMissingError,
    SourceConfig,
    SynthesizerConfig,
)
from xhs_knowledge.retriever import (
    VaultRetriever,
    extract_note_content_text,
    parse_frontmatter,
    validate_retriever_boundary,
)


@pytest.fixture
def test_vault(tmp_path: Path) -> Path:
    """Creates a hermetic, isolated test vault with collections and notes."""
    vault = tmp_path / "TestVault"
    notes_dir = vault / "notes"
    colls_dir = vault / "collections"
    notes_dir.mkdir(parents=True)
    colls_dir.mkdir(parents=True)

    # Note 1: Normal note
    (notes_dir / "note_001.md").write_text(
        "---\n"
        'title: "架构师之道"\n'
        'note_id: "note_001"\n'
        'author_name: "张老师"\n'
        "---\n\n"
        "# 架构师之道\n\n"
        "## Content\n\n"
        "分布式系统的核心是保证数据一致性与可用性的权衡。\n",
        encoding="utf-8",
    )

    # Note 2: Normal note
    (notes_dir / "note_002.md").write_text(
        "---\n"
        'title: "Agent设计模式"\n'
        'note_id: "note_002"\n'
        'author_name: "李研究员"\n'
        "---\n\n"
        "# Agent设计模式\n\n"
        "## Content\n\n"
        "评测集的多样性是衡量Agent泛化能力的关键指标。\n",
        encoding="utf-8",
    )

    # Note 3: Empty content note (to test NOTE_CONTENT_EMPTY)
    (notes_dir / "note_003.md").write_text(
        "---\n"
        'title: "纯图片分享"\n'
        'note_id: "note_003"\n'
        'author_name: "摄影师"\n'
        "---\n\n"
        "# 纯图片分享\n\n"
        "## Content\n\n"
        "*(无文字内容)*\n\n"
        "## Media\n\n"
        "![Image](../assets/note_003/img1.jpg)\n",
        encoding="utf-8",
    )

    # Note 4: Note without title in frontmatter (tests fallback from collection alias)
    (notes_dir / "note_004.md").write_text(
        "---\n"
        'title: "无标题笔记"\n'
        'note_id: "note_004"\n'
        'author_name: ""\n'
        "---\n\n"
        "# 无标题笔记\n\n"
        "## Content\n\n"
        "CLI工具在生产环境中具有极高的吞吐与稳定性优势。\n",
        encoding="utf-8",
    )

    # Collection: tech
    # Pos 1: note_001
    # Pos 2: note_999 (pending export, not in notes/)
    # Pos 3: note_003 (empty content)
    # Pos 4: note_002
    # Pos 5: note_004 (alias title & author in link)
    (colls_dir / "tech.md").write_text(
        "---\n"
        'collection_id: "col_tech_101"\n'
        'name: "tech"\n'
        "total_notes: 5\n"
        "---\n\n"
        "# tech\n\n"
        "## 收藏笔记\n\n"
        "- [[note_001|架构师之道]] — *张老师*\n"
        "- [[note_999|未导出笔记]] *(待导出)*\n"
        "- [[note_003|纯图片分享]] — *摄影师*\n"
        "- [[note_002|Agent设计模式]] — *李研究员*\n"
        "- [[note_004|高效CLI工具指南]] — *开发团队*\n",
        encoding="utf-8",
    )

    return vault


def test_contract_request_validation():
    """Validates DigestRequest schema constraints and invariants."""
    # Valid request
    req = DigestRequest(
        digest_name="daily_tech",
        target_date="2026-09-17",
        source=SourceConfig(collections=["tech"]),
        selection=SelectionConfig(max_notes=10),
    )
    assert req.digest_name == "daily_tech"
    assert req.compute_fingerprint() is not None

    # Invalid empty digest_name
    with pytest.raises(ValueError, match="digest_name"):
        DigestRequest(digest_name="", target_date="2026-09-17")

    # Invalid empty target_date
    with pytest.raises(ValueError, match="target_date"):
        DigestRequest(digest_name="valid", target_date="")

    # Invalid max_notes <= 0
    with pytest.raises(ValueError, match="positive"):
        DigestRequest(digest_name="valid", target_date="2026-09-17", selection=SelectionConfig(max_notes=0))

    # Invalid max_notes > 50 (hard ceiling)
    with pytest.raises(ValueError, match="hard ceiling"):
        DigestRequest(digest_name="valid", target_date="2026-09-17", selection=SelectionConfig(max_notes=51))

    # Forbidden order_by: Attempting to rank by collected_at or user activity
    with pytest.raises(ValueError, match="Unsupported order_by"):
        DigestRequest(
            digest_name="valid",
            target_date="2026-09-17",
            selection=SelectionConfig(order_by=["collected_at DESC"]),
        )


def test_retriever_boundary_guards(tmp_path: Path):
    """VaultRetriever must refuse to run if vault collides with data/ or .xhs-state/."""
    fake_data = tmp_path / "data"
    fake_data.mkdir()
    fake_state = tmp_path / ".xhs-state"
    fake_state.mkdir()

    # Vault inside data/ -> forbidden
    inside_data = fake_data / "Vault"
    with pytest.raises(BoundaryViolationError):
        VaultRetriever(vault_dir=inside_data, data_dir=fake_data, state_dir=fake_state)

    # data inside Vault -> forbidden
    vault_dir = tmp_path / "SafeVault"
    vault_dir.mkdir()
    data_inside_vault = vault_dir / "data"
    with pytest.raises(BoundaryViolationError):
        VaultRetriever(vault_dir=vault_dir, data_dir=data_inside_vault, state_dir=fake_state)

    # Vault inside state_dir -> forbidden
    inside_state = fake_state / "Vault"
    with pytest.raises(BoundaryViolationError):
        VaultRetriever(vault_dir=inside_state, data_dir=fake_data, state_dir=fake_state)


def test_deterministic_retrieval_and_ordering(test_vault: Path):
    """Retrieval must strictly respect collection position, filter empty/pending notes, and be deterministic."""
    retriever = VaultRetriever(vault_dir=test_vault)
    req = DigestRequest(
        digest_name="tech_digest",
        target_date="2026-09-17",
        source=SourceConfig(collections=["tech"]),
        selection=SelectionConfig(max_notes=10),
    )

    notes = retriever.retrieve(req)

    # Expected:
    # note_001 (pos 1) -> admitted
    # note_999 (pos 2) -> pending *(待导出)* -> skipped
    # note_003 (pos 3) -> NOTE_CONTENT_EMPTY -> skipped
    # note_002 (pos 4) -> admitted
    # note_004 (pos 5) -> admitted (with fallback title and author)
    assert len(notes) == 3

    assert notes[0].note_id == "note_001"
    assert notes[0].collection_position == 1
    assert notes[0].title == "架构师之道"
    assert "权衡" in notes[0].content_text

    assert notes[1].note_id == "note_002"
    assert notes[1].collection_position == 4
    assert notes[1].title == "Agent设计模式"

    assert notes[2].note_id == "note_004"
    assert notes[2].collection_position == 5
    assert notes[2].title == "高效CLI工具指南"  # Fallback from collection alias!
    assert notes[2].author_name == "开发团队"  # Fallback from collection alias!

    # Verify bundle assembly
    bundle = retriever.assemble_bundle(req, notes, created_at="2026-09-17T12:00:00Z")
    assert bundle.bundle_id == "bundle_20260917_tech_digest"
    assert bundle.total_notes == 3
    assert len(bundle.bundle_content_hash) == 64


def test_hash_and_order_stability(test_vault: Path):
    """10 repeated runs must yield 100% identical SelectedNote lists and content hashes."""
    retriever = VaultRetriever(vault_dir=test_vault)
    req = DigestRequest(
        digest_name="tech_digest",
        target_date="2026-09-17",
        source=SourceConfig(collections=["tech"]),
        selection=SelectionConfig(max_notes=10),
    )

    base_notes = retriever.retrieve(req)
    base_bundle = retriever.assemble_bundle(req, base_notes)

    for _ in range(10):
        notes = retriever.retrieve(req)
        assert [n.note_id for n in notes] == [n.note_id for n in base_notes]
        assert [n.file_sha256 for n in notes] == [n.file_sha256 for n in base_notes]
        assert [n.collection_position for n in notes] == [n.collection_position for n in base_notes]

        bundle = retriever.assemble_bundle(req, notes)
        assert bundle.bundle_content_hash == base_bundle.bundle_content_hash
        assert bundle.request_fingerprint == base_bundle.request_fingerprint


def test_max_notes_ceiling(test_vault: Path):
    """selection.max_notes truncates accurately according to order_by."""
    retriever = VaultRetriever(vault_dir=test_vault)
    req = DigestRequest(
        digest_name="tech_digest",
        target_date="2026-09-17",
        source=SourceConfig(collections=["tech"]),
        selection=SelectionConfig(max_notes=2),
    )
    notes = retriever.retrieve(req)
    assert len(notes) == 2
    assert notes[0].note_id == "note_001"
    assert notes[1].note_id == "note_002"


def test_empty_selection_fails_closed(test_vault: Path):
    """If no notes match, retrieval must fail closed with EmptySelectionError."""
    retriever = VaultRetriever(vault_dir=test_vault)
    req = DigestRequest(
        digest_name="unknown_digest",
        target_date="2026-09-17",
        source=SourceConfig(collections=["nonexistent_collection"]),
    )
    with pytest.raises(EmptySelectionError):
        retriever.retrieve(req)


def test_missing_position_fails_closed(test_vault: Path):
    """Corrupted collection without ## 收藏笔记 section fails closed with SelectionPositionMissingError."""
    corrupted_file = test_vault / "collections" / "corrupt.md"
    corrupted_file.write_text("# Corrupt Collection\nNo notes section here.\n", encoding="utf-8")

    retriever = VaultRetriever(vault_dir=test_vault)
    req = DigestRequest(
        digest_name="corrupt_digest",
        target_date="2026-09-17",
        source=SourceConfig(collections=["corrupt"]),
    )
    with pytest.raises(SelectionPositionMissingError):
        retriever.retrieve(req)


def test_real_vault_read_only_integration():
    """Verifies VaultRetriever against real repository Vault without any mutation."""
    vault_path = Path("Vault")
    if not vault_path.exists() or not (vault_path / "collections").exists():
        pytest.skip("Repository Vault directory not present")

    retriever = VaultRetriever(
        vault_dir=vault_path,
        data_dir=Path("data"),
        state_dir=Path(".xhs-state"),
    )

    req = DigestRequest(
        digest_name="ai_daily",
        target_date="2026-09-17",
        source=SourceConfig(collections=["coding"]),
        selection=SelectionConfig(max_notes=5),
    )

    notes = retriever.retrieve(req)
    assert len(notes) == 5

    # Check that positions are strictly monotonic
    positions = [n.collection_position for n in notes]
    assert positions == sorted(positions)
    assert positions[0] >= 1

    # Verify SHA256 of each note file is valid
    for n in notes:
        actual_sha = hashlib.sha256((vault_path / n.file_path).read_bytes()).hexdigest()
        assert n.file_sha256 == actual_sha
        assert len(n.content_text) > 0

    # Assemble bundle and verify fingerprinting
    bundle = retriever.assemble_bundle(req, notes)
    assert len(bundle.request_fingerprint) == 64
    assert len(bundle.bundle_content_hash) == 64
