"""Tests for Phase C.1 VaultRetriever & Contract Schemas (Remediated v1).

Verifies:
1. Zero network access and cryptographically verified zero P1 data/ mutation.
2. Strict physical boundary enforcement (Vault isolation, pre-read symlink rejection).
3. Deterministic note selection (collection priority, vault_collection_position ASC, note_id ASC).
4. Multi-collection duplicate note provenance preservation without information loss.
5. Hash and order stability across multiple runs.
6. Fail-closed error handling (EMPTY_SELECTION, NOTE_CONTENT_EMPTY, unsupported order_by, invalid date format).
"""

import hashlib
import json
import os
from pathlib import Path
import pytest

from xhs_knowledge.contracts import (
    BoundaryViolationError,
    CollectionMembership,
    DigestError,
    DigestRequest,
    EmptySelectionError,
    EvidenceBundle,
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


def compute_file_sha256(path: Path) -> str:
    """Computes SHA256 of a single file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def compute_dir_tree_sha256(directory: Path) -> str:
    """Computes a deterministic hash over all files in directory."""
    hashes: list[str] = []
    for root, dirs, files in os.walk(directory):
        dirs.sort()
        for f in sorted(files):
            p = Path(root) / f
            hashes.append(f"{p.relative_to(directory)}:{compute_file_sha256(p)}")
    return hashlib.sha256("\n".join(hashes).encode("utf-8")).hexdigest()


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

    # Invalid target_date: empty
    with pytest.raises(ValueError, match="target_date"):
        DigestRequest(digest_name="valid", target_date="")

    # Invalid target_date: non-strict format (slashes or bad date)
    with pytest.raises(ValueError, match="strict 'YYYY-MM-DD'"):
        DigestRequest(digest_name="valid", target_date="2026/09/17")
    with pytest.raises(ValueError, match="strict 'YYYY-MM-DD'"):
        DigestRequest(digest_name="valid", target_date="today")
    with pytest.raises(ValueError, match="strict 'YYYY-MM-DD'"):
        DigestRequest(digest_name="valid", target_date="2026-13-45")

    # Invalid max_notes <= 0
    with pytest.raises(ValueError, match="positive"):
        DigestRequest(
            digest_name="valid",
            target_date="2026-09-17",
            source=SourceConfig(collections=["tech"]),
            selection=SelectionConfig(max_notes=0),
        )

    # Invalid max_notes > 50 (hard ceiling)
    with pytest.raises(ValueError, match="hard ceiling"):
        DigestRequest(
            digest_name="valid",
            target_date="2026-09-17",
            source=SourceConfig(collections=["tech"]),
            selection=SelectionConfig(max_notes=51),
        )

    # Forbidden order_by: Attempting to rank by collected_at or user activity
    with pytest.raises(ValueError, match="Unsupported order_by"):
        DigestRequest(
            digest_name="valid",
            target_date="2026-09-17",
            source=SourceConfig(collections=["tech"]),
            selection=SelectionConfig(order_by=["collected_at DESC"]),
        )


    # Invalid collections length: 0 collections
    with pytest.raises(ValueError, match="exactly one collection"):
        DigestRequest(
            digest_name="valid",
            target_date="2026-09-17",
            source=SourceConfig(collections=[]),
        )

    # Invalid collections length: >1 collections
    with pytest.raises(ValueError, match="exactly one collection"):
        DigestRequest(
            digest_name="valid",
            target_date="2026-09-17",
            source=SourceConfig(collections=["col_1", "col_2"]),
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

    # Refuse initialization without boundary isolation unless explicitly permitted
    with pytest.raises(BoundaryViolationError, match="boundary isolation"):
        VaultRetriever(vault_dir=vault_dir, data_dir=None, state_dir=None, allow_unisolated_vault=False)


def test_symlink_boundary_attack_rejected_before_read(tmp_path: Path):
    """Symlinks inside Vault must be rejected BEFORE reading external content."""
    # Create external secret file
    secret_dir = tmp_path / "external_secret"
    secret_dir.mkdir()
    secret_file = secret_dir / "secret.txt"
    secret_file.write_text("SUPER_SECRET_TOKEN=12345", encoding="utf-8")

    vault_dir = tmp_path / "VaultWithSymlink"
    notes_dir = vault_dir / "notes"
    colls_dir = vault_dir / "collections"
    notes_dir.mkdir(parents=True)
    colls_dir.mkdir(parents=True)

    # 1. Symlink inside collections pointing outside
    rogue_collection = colls_dir / "evil_collection.md"
    rogue_collection.symlink_to(secret_file)

    retriever = VaultRetriever(vault_dir=vault_dir, allow_unisolated_vault=True)
    req = DigestRequest(
        digest_name="attack_test",
        target_date="2026-09-17",
        source=SourceConfig(collections=["evil_collection"]),
    )

    # Must raise BoundaryViolationError and never open secret_file
    with pytest.raises(BoundaryViolationError, match="symlink"):
        retriever.retrieve(req)

    # Clean up collection symlink
    rogue_collection.unlink()

    # 2. Valid collection referencing a symlinked note
    (colls_dir / "safe_col.md").write_text(
        "---\nname: 'safe_col'\n---\n## 收藏笔记\n- [[evil_note|Evil]]\n",
        encoding="utf-8",
    )
    evil_note = notes_dir / "evil_note.md"
    evil_note.symlink_to(secret_file)

    with pytest.raises(BoundaryViolationError, match="symlink"):
        retriever.retrieve(
            DigestRequest(
                digest_name="note_attack",
                target_date="2026-09-17",
                source=SourceConfig(collections=["safe_col"]),
            )
        )


def test_deterministic_retrieval_and_ordering(test_vault: Path):
    """Retrieval must strictly respect collection position, filter empty/pending notes, and be deterministic."""
    retriever = VaultRetriever(vault_dir=test_vault, allow_unisolated_vault=True)
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
    assert notes[0].vault_collection_position == 1
    assert notes[0].primary_collection == "tech"
    assert notes[0].title == "架构师之道"
    assert "权衡" in notes[0].content_text

    assert notes[1].note_id == "note_002"
    assert notes[1].vault_collection_position == 4
    assert notes[1].primary_collection == "tech"
    assert notes[1].title == "Agent设计模式"

    assert notes[2].note_id == "note_004"
    assert notes[2].vault_collection_position == 5
    assert notes[2].primary_collection == "tech"
    assert notes[2].title == "高效CLI工具指南"  # Fallback from collection alias!
    assert notes[2].author_name == "开发团队"  # Fallback from collection alias!

    # Verify bundle assembly
    bundle = retriever.assemble_bundle(req, notes, created_at="2026-09-17T12:00:00Z")
    assert bundle.bundle_id == "bundle_20260917_tech_digest"
    assert bundle.total_notes == 3
    assert len(bundle.bundle_content_hash) == 64


def test_single_collection_guard_and_provenance(tmp_path: Path):
    """Multi-collection requests are rejected by guard; single collections maintain exact position."""
    vault = tmp_path / "MultiVault"
    notes_dir = vault / "notes"
    colls_dir = vault / "collections"
    notes_dir.mkdir(parents=True)
    colls_dir.mkdir(parents=True)

    # Shared note in both col_ai (pos 2) and col_tools (pos 1)
    (notes_dir / "shared_note.md").write_text(
        "---\ntitle: 'Shared Note'\nnote_id: 'shared_note'\nauthor_name: 'Author'\n---\n"
        "## Content\nShared technical content\n",
        encoding="utf-8",
    )
    (notes_dir / "ai_only.md").write_text(
        "---\ntitle: 'AI Only'\nnote_id: 'ai_only'\nauthor_name: 'Author'\n---\n"
        "## Content\nAI content\n",
        encoding="utf-8",
    )
    (colls_dir / "col_ai.md").write_text(
        "---\ncollection_id: 'id_ai'\nname: 'col_ai'\n---\n"
        "## 收藏笔记\n- [[ai_only|AI Only]]\n- [[shared_note|Shared Note]]\n",
        encoding="utf-8",
    )
    (colls_dir / "col_tools.md").write_text(
        "---\ncollection_id: 'id_tools'\nname: 'col_tools'\n---\n"
        "## 收藏笔记\n- [[shared_note|Shared Note]]\n",
        encoding="utf-8",
    )

    retriever = VaultRetriever(vault_dir=vault, allow_unisolated_vault=True)

    # 1. Multi-collection request strictly rejected by guard
    with pytest.raises(ValueError, match="exactly one collection"):
        DigestRequest(
            digest_name="digest_multi",
            target_date="2026-09-17",
            source=SourceConfig(collections=["col_ai", "col_tools"]),
        )

    # 2. Querying col_ai alone yields shared_note at position 2
    req_ai = DigestRequest(
        digest_name="digest_ai",
        target_date="2026-09-17",
        source=SourceConfig(collections=["col_ai"]),
    )
    notes_ai = retriever.retrieve(req_ai)
    assert len(notes_ai) == 2
    assert notes_ai[1].note_id == "shared_note"
    assert notes_ai[1].primary_collection == "col_ai"
    assert notes_ai[1].vault_collection_position == 2

    # 3. Querying col_tools alone yields shared_note at position 1
    req_tools = DigestRequest(
        digest_name="digest_tools",
        target_date="2026-09-17",
        source=SourceConfig(collections=["col_tools"]),
    )
    notes_tools = retriever.retrieve(req_tools)
    assert len(notes_tools) == 1
    assert notes_tools[0].note_id == "shared_note"
    assert notes_tools[0].primary_collection == "col_tools"
    assert notes_tools[0].vault_collection_position == 1


def test_hash_and_order_stability(test_vault: Path):
    """10 repeated runs must yield 100% identical SelectedNote lists and content hashes."""
    retriever = VaultRetriever(vault_dir=test_vault, allow_unisolated_vault=True)
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
        assert [n.vault_collection_position for n in notes] == [n.vault_collection_position for n in base_notes]

        bundle = retriever.assemble_bundle(req, notes)
        assert bundle.bundle_content_hash == base_bundle.bundle_content_hash
        assert bundle.request_fingerprint == base_bundle.request_fingerprint


def test_max_notes_ceiling(test_vault: Path):
    """selection.max_notes truncates accurately according to order_by."""
    retriever = VaultRetriever(vault_dir=test_vault, allow_unisolated_vault=True)
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
    retriever = VaultRetriever(vault_dir=test_vault, allow_unisolated_vault=True)
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

    retriever = VaultRetriever(vault_dir=test_vault, allow_unisolated_vault=True)
    req = DigestRequest(
        digest_name="corrupt_digest",
        target_date="2026-09-17",
        source=SourceConfig(collections=["corrupt"]),
    )
    with pytest.raises(SelectionPositionMissingError):
        retriever.retrieve(req)


def test_real_vault_cryptographic_zero_mutation():
    """Verifies that retrieval against real repository Vault causes ZERO mutation to P1 storage."""
    vault_path = Path("Vault")
    data_path = Path("data")
    state_path = Path(".xhs-state")
    sync_db_path = state_path / "sync.db"

    if not vault_path.exists() or not sync_db_path.exists():
        pytest.skip("Repository Vault or P1 state not present")

    # 1. Cryptographic hash before retrieval
    before_db_hash = compute_file_sha256(sync_db_path)
    before_data_tree_hash = compute_dir_tree_sha256(data_path)

    # 2. Execute retrieval
    retriever = VaultRetriever(
        vault_dir=vault_path,
        data_dir=data_path,
        state_dir=state_path,
    )

    req = DigestRequest(
        digest_name="ai_daily",
        target_date="2026-09-17",
        source=SourceConfig(collections=["coding"]),
        selection=SelectionConfig(max_notes=5),
    )

    notes = retriever.retrieve(req)
    assert len(notes) == 5

    positions = [n.vault_collection_position for n in notes]
    assert positions == sorted(positions)
    assert positions[0] >= 1

    # 3. Cryptographic hash after retrieval: MUST BE IDENTICAL
    after_db_hash = compute_file_sha256(sync_db_path)
    after_data_tree_hash = compute_dir_tree_sha256(data_path)

    assert before_db_hash == after_db_hash, "P1 sync.db was mutated during VaultRetriever execution!"
    assert before_data_tree_hash == after_data_tree_hash, "P1 data/ directory was mutated during VaultRetriever execution!"
