"""Tests for xhs_knowledge (P2.2b Collection Projection Layer)."""

import json
from pathlib import Path
import pytest

from xhs_knowledge import CollectionIndexer, sanitize_url
from xhs_knowledge.collection_indexer import (
    sanitize_filename,
    validate_vault_boundary,
    main as cli_main,
)


# ---------------------------------------------------------------------------
# 1. URL Sanitization (Evidence Hygiene Gate)
# ---------------------------------------------------------------------------


def test_sanitize_url():
    # 1. Standard XHS explore note URL with tracking tokens
    raw = "https://www.xiaohongshu.com/explore/63fee6390000000027012206?xsec_token=AB123&xsec_source=pc_fav&utm_source=share"
    clean = sanitize_url(raw, "63fee6390000000027012206")
    assert clean == "https://www.xiaohongshu.com/explore/63fee6390000000027012206"
    assert "xsec_token" not in clean
    assert "utm_source" not in clean

    # 2. XHS URL without note_id argument
    clean_no_id = sanitize_url(raw)
    assert clean_no_id == "https://www.xiaohongshu.com/explore/63fee6390000000027012206"

    # 3. Third party URL with tracking query parameters stripped
    ext_url = "https://github.com/foo/bar?utm_source=twitter&ref=newsletter&feature=auto"
    assert sanitize_url(ext_url) == "https://github.com/foo/bar?feature=auto"

    # 4. Clean URL preserved
    pure = "https://example.com/docs"
    assert sanitize_url(pure) == pure

    # 5. Empty URL
    assert sanitize_url("") == ""
    assert sanitize_url("", "abc123") == "https://www.xiaohongshu.com/explore/abc123"


# ---------------------------------------------------------------------------
# 2. Boundary and Safety Validation
# ---------------------------------------------------------------------------


def test_validate_vault_boundary(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    state_dir = tmp_path / ".xhs-state"
    state_dir.mkdir()

    # Valid external vault
    vault_dir = tmp_path / "Vault"
    validate_vault_boundary(vault_dir, data_dir, state_dir)

    # Vault inside data_dir must fail
    with pytest.raises(ValueError, match="cannot be inside or equal to data directory"):
        validate_vault_boundary(data_dir / "Vault", data_dir, state_dir)

    # Vault equal to data_dir must fail
    with pytest.raises(ValueError, match="cannot be inside or equal to data directory"):
        validate_vault_boundary(data_dir, data_dir, state_dir)

    # Vault inside state_dir must fail
    with pytest.raises(ValueError, match="cannot be inside or equal to state directory"):
        validate_vault_boundary(state_dir / "subvault", data_dir, state_dir)


def test_sanitize_filename():
    assert sanitize_filename("Normal Name") == "Normal Name"
    assert sanitize_filename("AI/ML: Coding Tips?") == "AI_ML_ Coding Tips_"
    assert sanitize_filename("吃") == "吃"
    assert sanitize_filename("") == "collection"


# ---------------------------------------------------------------------------
# 3. Full Collection Indexing Workflow (Happy Path & Fallbacks)
# ---------------------------------------------------------------------------


@pytest.fixture()
def indexer_env(tmp_path):
    vault_dir = tmp_path / "Vault"
    notes_dir = vault_dir / "notes"
    notes_dir.mkdir(parents=True)

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)

    evidence_dir = tmp_path / "evidence" / "collections"
    evidence_dir.mkdir(parents=True)

    # Collection 1: "coding" (2 notes)
    c1_dir = evidence_dir / "board_coding_123"
    c1_dir.mkdir(parents=True)
    c1_manifest = {
        "board_id": "board_coding_123",
        "name": "coding",
        "reported_total": 2,
        "observed_total": 2,
        "completion_proof": {"type": "api_has_more_false"},
    }
    (c1_dir / "manifest.json").write_text(json.dumps(c1_manifest), encoding="utf-8")
    c1_page = {
        "notes_count": 2,
        "notes": [
            {"note_id": "note_001", "title": "Fallback Title 1", "type": "normal"},
            {"note_id": "note_002", "title": "Fallback Title 2", "type": "normal"},
        ],
    }
    (c1_dir / "page_001.json").write_text(json.dumps(c1_page), encoding="utf-8")

    # Canonical note for note_001 (overrides fallback title)
    n1_dir = data_dir / "note_001"
    n1_dir.mkdir(parents=True)
    (n1_dir / "canonical.json").write_text(
        json.dumps({
            "content": {"title": "Canonical Title: Agentic Python"},
            "author": {"name": "Senior Eng"},
        }),
        encoding="utf-8",
    )
    # note_001 is already exported into Vault
    (notes_dir / "note_001.md").write_text("# Note 1", encoding="utf-8")

    # note_002 has NO canonical and is NOT exported in Vault (tests fallback & pending status)

    return {
        "vault_dir": vault_dir,
        "evidence_dir": evidence_dir,
        "data_dir": data_dir,
    }


def test_indexer_happy_path(indexer_env):
    indexer = CollectionIndexer(
        vault_dir=indexer_env["vault_dir"],
        evidence_dir=indexer_env["evidence_dir"],
        data_dir=indexer_env["data_dir"],
    )
    result = indexer.index()

    assert result.collections_indexed == 1
    assert result.total_notes_referenced == 2
    assert result.exported_notes_count == 1
    assert result.missing_notes_count == 1

    col_file = indexer_env["vault_dir"] / "collections" / "coding.md"
    assert col_file.exists()
    content = col_file.read_text(encoding="utf-8")

    # Frontmatter validation
    assert 'collection_id: "board_coding_123"' in content
    assert 'name: "coding"' in content
    assert "total_notes: 2" in content
    assert "exported_notes: 1" in content

    # Heading
    assert "# coding" in content
    assert "- **Collection ID**: `board_coding_123`" in content
    assert "- **Vault Export Status**: 1/2 篇已同步" in content

    # Wikilinks:
    # note_001 has canonical title and author name
    assert "- [[note_001|Canonical Title: Agentic Python]] — *Senior Eng*" in content
    # note_002 has fallback title from page evidence and is pending export
    assert "- [[note_002|Fallback Title 2]] *(待导出)*" in content


def test_indexer_idempotent(indexer_env):
    indexer = CollectionIndexer(
        vault_dir=indexer_env["vault_dir"],
        evidence_dir=indexer_env["evidence_dir"],
        data_dir=indexer_env["data_dir"],
    )
    res1 = indexer.index()
    col_file = indexer_env["vault_dir"] / "collections" / "coding.md"
    content1 = col_file.read_text(encoding="utf-8")

    res2 = indexer.index()
    content2 = col_file.read_text(encoding="utf-8")

    # Apart from indexed_at timestamps, structure and links must match identically
    lines1 = [l for l in content1.splitlines() if not l.startswith("indexed_at:")]
    lines2 = [l for l in content2.splitlines() if not l.startswith("indexed_at:")]
    assert lines1 == lines2
    assert res1.collections_indexed == res2.collections_indexed


def test_indexer_missing_evidence_dir(tmp_path):
    indexer = CollectionIndexer(
        vault_dir=tmp_path / "Vault",
        evidence_dir=tmp_path / "non_existent_evidence",
        data_dir=tmp_path / "data",
    )
    with pytest.raises(FileNotFoundError, match="Evidence collections directory does not exist"):
        indexer.index()


def test_cli_main(indexer_env, monkeypatch, capsys):
    test_args = [
        "xhs-collection-indexer",
        "--vault-dir",
        str(indexer_env["vault_dir"]),
        "--evidence-dir",
        str(indexer_env["evidence_dir"]),
        "--data-dir",
        str(indexer_env["data_dir"]),
    ]
    monkeypatch.setattr("sys.argv", test_args)
    code = cli_main()
    assert code == 0

    captured = capsys.readouterr()
    assert "=== Obsidian Collection Indexer Summary ===" in captured.out
    assert "Collections Indexed:      1" in captured.out


def test_demo_synthetic_fixture_reproducible(tmp_path):
    """Verifies that the public demo fixtures under demo/ execute deterministically."""
    demo_dir = Path("demo")
    if not (demo_dir / "evidence" / "collections").exists():
        pytest.skip("demo fixtures not found")

    target_vault = tmp_path / "DemoVault"
    # Copy existing notes to test full link resolution
    notes_src = demo_dir / "Vault" / "notes"
    if notes_src.exists():
        import shutil
        shutil.copytree(notes_src, target_vault / "notes")

    indexer = CollectionIndexer(
        vault_dir=target_vault,
        evidence_dir=demo_dir / "evidence" / "collections",
        data_dir=demo_dir / "data",
    )
    result = indexer.index()

    assert result.collections_indexed == 2
    assert result.total_notes_referenced == 5
    assert (target_vault / "README.md").exists()
    assert (target_vault / "collections" / "AI Tools.md").exists()
    assert (target_vault / "collections" / "Restaurants.md").exists()

    ai_tools_content = (target_vault / "collections" / "AI Tools.md").read_text(encoding="utf-8")
    assert "[[demo_note_001|Building Agent Systems]]" in ai_tools_content
    assert "[[demo_note_002|Reliable LLM Evaluation]]" in ai_tools_content

