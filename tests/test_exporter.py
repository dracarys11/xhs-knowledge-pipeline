"""Tests for xhs_export (P2.1 Exporter MVP).

Verifies strict compliance with docs/P2_EXPORTER_CONTRACT.md:
- Physical isolation & output boundary protection (output escape)
- Path traversal protection (note_id and media filename)
- Deterministic asset copy with SHA-256 (same size different content detection)
- Stale vault artifact reconciliation (managed file cleanup)
- Canonical schema validation (malformed JSON, non-dict, non-dict media, missing size_bytes)
- Manifest snapshot semantics (tracked_count, complete_candidates, evaluated_candidates, exported_count)
- P1 Zero-Mutation Isolation Guard
- CLI entry point execution
"""

import hashlib
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from xhs_export import VaultExporter, sanitize_note_url
from xhs_export.__main__ import main as cli_main
from xhs_export.exporter import (
    DEFAULT_DATA_DIR,
    DEFAULT_STATE_DB,
    DEFAULT_VAULT_DIR,
    is_safe_filename,
    render_vault_markdown,
    validate_output_boundary,
)

SCHEMA_SQL = """
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
"""


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _hash_dir(directory: Path) -> dict[str, str]:
    hashes = {}
    if not directory.exists():
        return hashes
    for p in sorted(directory.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(directory))
            hashes[rel] = _hash_file(p)
    return hashes


@pytest.fixture()
def p1_env(tmp_path: Path):
    """Creates a mock P1 environment with SQLite DB and data directory."""
    db_dir = tmp_path / ".xhs-state"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "sync.db"

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)

    vault_dir = tmp_path / "Vault"

    class P1Fixture:
        def __init__(self):
            self.db_path = db_path
            self.data_dir = data_dir
            self.vault_dir = vault_dir

        def add_db_note(
            self,
            note_id: str,
            status: str = "COMPLETE",
            title: str = "Test Title",
            source_url: str = "https://www.xiaohongshu.com/explore/test",
        ):
            c = sqlite3.connect(self.db_path)
            c.execute(
                """
                INSERT OR REPLACE INTO notes (
                    note_id, status, source_url, title, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '2026-09-17T00:00:00Z', '2026-09-17T00:00:00Z')
                """,
                (note_id, status, source_url, title),
            )
            c.commit()
            c.close()

        def demote_or_delete_note(self, note_id: str, new_status: str | None = None):
            c = sqlite3.connect(self.db_path)
            if new_status is None:
                c.execute("DELETE FROM notes WHERE note_id = ?", (note_id,))
            else:
                c.execute("UPDATE notes SET status = ? WHERE note_id = ?", (new_status, note_id))
            c.commit()
            c.close()

        def create_note_artifacts(
            self,
            note_id: str,
            canonical_data: any,
            media_files: dict[str, bytes] | None = None,
        ):
            note_dir = self.data_dir / note_id
            note_dir.mkdir(parents=True, exist_ok=True)
            canonical_path = note_dir / "canonical.json"
            if isinstance(canonical_data, (dict, list)):
                canonical_path.write_text(json.dumps(canonical_data, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                canonical_path.write_text(str(canonical_data), encoding="utf-8")

            if media_files:
                assets_dir = note_dir / "assets"
                assets_dir.mkdir(parents=True, exist_ok=True)
                for filename, content in media_files.items():
                    (assets_dir / filename).write_bytes(content)

    return P1Fixture()


# ---------------------------------------------------------------------------
# 1. Output Boundary Guard (Output Escape Protection)
# ---------------------------------------------------------------------------
def test_output_boundary_guard_rejects_data_dir(p1_env):
    # Case A: vault_dir == data_dir
    with pytest.raises(ValueError, match="cannot be inside or equal to data_dir"):
        VaultExporter(state_db=p1_env.db_path, data_dir=p1_env.data_dir, vault_dir=p1_env.data_dir)

    # Case B: vault_dir inside data_dir
    with pytest.raises(ValueError, match="cannot be inside or equal to data_dir"):
        VaultExporter(state_db=p1_env.db_path, data_dir=p1_env.data_dir, vault_dir=p1_env.data_dir / "vault_sub")

    # Case C: data_dir inside vault_dir
    with pytest.raises(ValueError, match="data_dir .* cannot be inside vault_dir"):
        VaultExporter(state_db=p1_env.db_path, data_dir=p1_env.data_dir, vault_dir=p1_env.data_dir.parent)


def test_output_boundary_guard_rejects_state_db_dir(p1_env, tmp_path_factory):
    db_parent = p1_env.db_path.parent

    # Case A: vault_dir == db_parent
    with pytest.raises(ValueError, match="cannot be inside or equal to state_db directory"):
        VaultExporter(state_db=p1_env.db_path, data_dir=p1_env.data_dir, vault_dir=db_parent)

    # Case B: vault_dir inside db_parent
    with pytest.raises(ValueError, match="cannot be inside or equal to state_db directory"):
        VaultExporter(state_db=p1_env.db_path, data_dir=p1_env.data_dir, vault_dir=db_parent / "nested_vault")

    # Case C: state_db directory inside vault_dir (data_dir is in a completely independent tmp dir)
    independent_dir = tmp_path_factory.mktemp("independent_data")
    with pytest.raises(ValueError, match="state_db directory .* cannot be inside vault_dir"):
        VaultExporter(state_db=p1_env.db_path, data_dir=independent_dir, vault_dir=db_parent.parent)

    # Case D: vault_dir == db_path file itself
    with pytest.raises(ValueError, match="cannot be inside or equal to state_db directory"):
        VaultExporter(state_db=p1_env.db_path, data_dir=p1_env.data_dir, vault_dir=p1_env.db_path)


# ---------------------------------------------------------------------------
# 2. Path Traversal Protection
# ---------------------------------------------------------------------------
def test_is_safe_filename_helper():
    assert is_safe_filename("valid_id_123") is True
    assert is_safe_filename("photo.jpg") is True
    assert is_safe_filename("../evil") is False
    assert is_safe_filename("..") is False
    assert is_safe_filename(".") is False
    assert is_safe_filename("sub/photo.jpg") is False
    assert is_safe_filename("sub\\photo.jpg") is False
    assert is_safe_filename("photo\0.jpg") is False
    assert is_safe_filename("") is False
    assert is_safe_filename("   ") is False
    assert is_safe_filename(None) is False
    assert is_safe_filename(123) is False


def test_path_traversal_note_id_rejected(p1_env):
    malicious_id = "../evil_escape"
    p1_env.add_db_note(malicious_id, status="COMPLETE")

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )
    result = exporter.export()

    assert result.exported_count == 0
    assert result.skipped_count == 1
    assert "INVALID_NOTE_ID_PATH" in result.skipped_notes[0]["reason"]
    # Ensure no files written outside vault
    assert not (p1_env.vault_dir.parent / "evil_escape.md").exists()


def test_path_traversal_media_filename_rejected(p1_env):
    note_id = "traversal_media_001"
    img_content = b"content"
    canonical = {
        "platform": "xhs",
        "note_id": note_id,
        "author": {"name": "博主"},
        "content": {"title": "越界测试", "text": "正文"},
        "media": [
            {
                "type": "image",
                "filename": "../../escaped.jpg",
                "size_bytes": len(img_content),
            }
        ],
    }
    p1_env.add_db_note(note_id, status="COMPLETE")
    p1_env.create_note_artifacts(note_id, canonical)

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )
    result = exporter.export()

    assert result.exported_count == 0
    assert result.skipped_count == 1
    assert "INVALID_MEDIA_FILENAME" in result.skipped_notes[0]["reason"]
    assert not (p1_env.vault_dir / "notes" / f"{note_id}.md").exists()


def test_reject_symlink_notes_dir(p1_env, tmp_path):
    p1_env.vault_dir.mkdir()
    outside = tmp_path / "outside-notes"
    outside.mkdir()
    (p1_env.vault_dir / "notes").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="notes_dir cannot be a symlink"):
        VaultExporter(p1_env.db_path, p1_env.data_dir, p1_env.vault_dir).export()


def test_reject_symlink_assets_dir(p1_env, tmp_path):
    p1_env.vault_dir.mkdir()
    outside = tmp_path / "outside-assets"
    outside.mkdir()
    (p1_env.vault_dir / "assets").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="assets_dir cannot be a symlink"):
        VaultExporter(p1_env.db_path, p1_env.data_dir, p1_env.vault_dir).export()


def test_reject_symlink_temp_target(p1_env, tmp_path):
    note_id = "symlink_temp_note"
    p1_env.add_db_note(note_id)
    p1_env.create_note_artifacts(
        note_id,
        {"note_id": note_id, "content": {"title": "T", "text": "B"}, "media": []},
    )
    notes_dir = p1_env.vault_dir / "notes"
    notes_dir.mkdir(parents=True)
    outside = tmp_path / "outside-temp"
    outside.write_text("do not overwrite", encoding="utf-8")
    (notes_dir / f".{note_id}.md.tmp").symlink_to(outside)

    with pytest.raises(ValueError, match="output temporary target cannot be a symlink"):
        VaultExporter(p1_env.db_path, p1_env.data_dir, p1_env.vault_dir).export()
    assert outside.read_text(encoding="utf-8") == "do not overwrite"


# ---------------------------------------------------------------------------
# 3. Deterministic Asset Copy (Same Size, Different Content Detection)
# ---------------------------------------------------------------------------
def test_same_size_different_content_overwritten(p1_env):
    note_id = "same_size_diff_content"
    old_content = b"AAAAAAAAAA"  # 10 bytes
    new_content = b"BBBBBBBBBB"  # 10 bytes (same size, different sha256)

    canonical = {
        "platform": "xhs",
        "note_id": note_id,
        "author": {"name": "博主"},
        "content": {"title": "Hash Check", "text": "Content"},
        "media": [
            {
                "type": "image",
                "filename": "asset.jpg",
                "size_bytes": len(new_content),
            }
        ],
    }
    p1_env.add_db_note(note_id, status="COMPLETE")
    p1_env.create_note_artifacts(note_id, canonical, media_files={"asset.jpg": new_content})

    # Pre-populate Vault with outdated content of identical size
    vault_asset = p1_env.vault_dir / "assets" / note_id / "asset.jpg"
    vault_asset.parent.mkdir(parents=True, exist_ok=True)
    vault_asset.write_bytes(old_content)
    assert vault_asset.read_bytes() == old_content

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )
    result = exporter.export()

    assert result.exported_count == 1
    # Check that asset in Vault was overwritten with new_content because SHA-256 differed
    assert vault_asset.read_bytes() == new_content


# ---------------------------------------------------------------------------
# 4. Stale Vault Artifact Reconciliation (Managed File Cleanup)
# ---------------------------------------------------------------------------
def test_stale_vault_artifact_cleanup(p1_env):
    note1 = "note_surviving"
    note2 = "note_stale"
    img1 = b"img1_content"
    img2 = b"img2_content"

    for nid, img in [(note1, img1), (note2, img2)]:
        p1_env.add_db_note(nid, status="COMPLETE")
        p1_env.create_note_artifacts(
            nid,
            {
                "platform": "xhs",
                "note_id": nid,
                "author": {"name": "A"},
                "content": {"title": f"Title {nid}", "text": "Body"},
                "media": [{"type": "image", "filename": f"{nid}.jpg", "size_bytes": len(img)}],
            },
            media_files={f"{nid}.jpg": img},
        )

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )

    # 1. First run: both notes exported
    res1 = exporter.export()
    assert res1.exported_count == 2
    assert (p1_env.vault_dir / "notes" / f"{note1}.md").exists()
    assert (p1_env.vault_dir / "notes" / f"{note2}.md").exists()
    assert (p1_env.vault_dir / "assets" / note1 / f"{note1}.jpg").exists()
    assert (p1_env.vault_dir / "assets" / note2 / f"{note2}.jpg").exists()

    # 2. Demote note2 in DB (e.g. demoted to RETRYABLE_FAILED)
    p1_env.demote_or_delete_note(note2, new_status="RETRYABLE_FAILED")

    # 3. Second run: full export (limit=None)
    res2 = exporter.export()
    assert res2.exported_count == 1
    assert res2.stale_cleaned_count == 2  # note2.md and assets/note2/

    # Surviving note remains intact
    assert (p1_env.vault_dir / "notes" / f"{note1}.md").exists()
    assert (p1_env.vault_dir / "assets" / note1 / f"{note1}.jpg").exists()

    # Stale artifacts pruned
    assert not (p1_env.vault_dir / "notes" / f"{note2}.md").exists()
    assert not (p1_env.vault_dir / "assets" / note2).exists()


def test_stale_cleanup_skipped_when_limit_is_active(p1_env):
    # Setup 2 COMPLETE notes
    for nid in ["note_a", "note_b"]:
        p1_env.add_db_note(nid, status="COMPLETE")
        p1_env.create_note_artifacts(
            nid,
            {
                "platform": "xhs",
                "note_id": nid,
                "author": {"name": "A"},
                "content": {"title": nid, "text": "Body"},
                "media": [],
            },
        )

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )

    # Run full export first
    exporter.export()
    assert (p1_env.vault_dir / "notes" / "note_a.md").exists()
    assert (p1_env.vault_dir / "notes" / "note_b.md").exists()

    # Now run partial export with limit=1: note_b should NOT be deleted
    res_partial = exporter.export(limit=1)
    assert res_partial.exported_count == 1
    assert res_partial.stale_cleaned_count == 0
    assert (p1_env.vault_dir / "notes" / "note_b.md").exists()


def test_cleanup_preserves_user_files(p1_env):
    note_id = "managed_note"
    p1_env.add_db_note(note_id)
    p1_env.create_note_artifacts(note_id, {"note_id": note_id, "media": []})
    exporter = VaultExporter(p1_env.db_path, p1_env.data_dir, p1_env.vault_dir)
    exporter.export()

    user_note = p1_env.vault_dir / "notes" / "my_note.md"
    user_note.write_text("user-owned", encoding="utf-8")
    user_asset_dir = p1_env.vault_dir / "assets" / "my_assets"
    user_asset_dir.mkdir()
    (user_asset_dir / "keep.txt").write_text("user-owned", encoding="utf-8")

    p1_env.demote_or_delete_note(note_id, new_status="RETRYABLE_FAILED")
    result = exporter.export()

    assert result.cleanup_status == "COMPLETED"
    assert not (p1_env.vault_dir / "notes" / f"{note_id}.md").exists()
    assert user_note.read_text(encoding="utf-8") == "user-owned"
    assert (user_asset_dir / "keep.txt").read_text(encoding="utf-8") == "user-owned"


def test_cleanup_without_owner_manifest_skips(p1_env):
    notes_dir = p1_env.vault_dir / "notes"
    assets_dir = p1_env.vault_dir / "assets"
    notes_dir.mkdir(parents=True)
    user_note = notes_dir / "old.md"
    user_note.write_text("unknown owner", encoding="utf-8")
    user_assets = assets_dir / "old"
    user_assets.mkdir(parents=True)
    (user_assets / "asset.jpg").write_bytes(b"unknown owner")

    result = VaultExporter(p1_env.db_path, p1_env.data_dir, p1_env.vault_dir).export()

    assert result.cleanup_status == "SKIPPED_NO_OWNERSHIP"
    assert user_note.exists()
    assert (user_assets / "asset.jpg").exists()


# ---------------------------------------------------------------------------
# 5. Canonical Schema Validation (Malformed JSON, Non-dict, Missing size_bytes)
# ---------------------------------------------------------------------------
def test_malformed_canonical_non_dict_json(p1_env):
    note_id = "non_dict_json_001"
    p1_env.add_db_note(note_id, status="COMPLETE")
    p1_env.create_note_artifacts(note_id, ["array", "not", "object"])

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )
    result = exporter.export()

    assert result.exported_count == 0
    assert result.skipped_count == 1
    assert "INVALID_CANONICAL_SCHEMA: expected JSON object" in result.skipped_notes[0]["reason"]


def test_malformed_canonical_media_not_list(p1_env):
    note_id = "media_not_list_001"
    p1_env.add_db_note(note_id, status="COMPLETE")
    p1_env.create_note_artifacts(
        note_id,
        {
            "platform": "xhs",
            "note_id": note_id,
            "author": {"name": "A"},
            "content": {"title": "Title", "text": "Body"},
            "media": "invalid_string_instead_of_list",
        },
    )

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )
    result = exporter.export()

    assert result.exported_count == 0
    assert result.skipped_count == 1
    assert "INVALID_CANONICAL_SCHEMA: media must be list" in result.skipped_notes[0]["reason"]


def test_malformed_canonical_media_item_not_dict(p1_env):
    note_id = "media_item_not_dict_001"
    p1_env.add_db_note(note_id, status="COMPLETE")
    p1_env.create_note_artifacts(
        note_id,
        {
            "platform": "xhs",
            "note_id": note_id,
            "author": {"name": "A"},
            "content": {"title": "Title", "text": "Body"},
            "media": ["not_a_dict"],
        },
    )

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )
    result = exporter.export()

    assert result.exported_count == 0
    assert result.skipped_count == 1
    assert "INVALID_CANONICAL_SCHEMA: media item must be dict" in result.skipped_notes[0]["reason"]


def test_malformed_canonical_media_missing_size_bytes(p1_env):
    note_id = "media_no_size_001"
    p1_env.add_db_note(note_id, status="COMPLETE")
    p1_env.create_note_artifacts(
        note_id,
        {
            "platform": "xhs",
            "note_id": note_id,
            "author": {"name": "A"},
            "content": {"title": "Title", "text": "Body"},
            "media": [{"type": "image", "filename": "photo.jpg"}],  # size_bytes omitted
        },
    )

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )
    result = exporter.export()

    assert result.exported_count == 0
    assert result.skipped_count == 1
    assert "MEDIA_MISSING_SIZE_BYTES: photo.jpg" in result.skipped_notes[0]["reason"]


# ---------------------------------------------------------------------------
# 6. Manifest Snapshot Semantics (Distinguishing candidate tiers)
# ---------------------------------------------------------------------------
def test_manifest_snapshot_semantics(p1_env):
    # Setup: 5 notes total (3 COMPLETE, 2 PENDING)
    for i in range(3):
        nid = f"note_comp_{i}"
        p1_env.add_db_note(nid, status="COMPLETE")
        p1_env.create_note_artifacts(
            nid,
            {
                "platform": "xhs",
                "note_id": nid,
                "author": {"name": "A"},
                "content": {"title": f"Title {i}", "text": "T"},
                "media": [],
            },
        )
    for i in range(2):
        nid = f"note_pend_{i}"
        p1_env.add_db_note(nid, status="PENDING")

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )
    # Run with limit=2
    result = exporter.export(limit=2)

    assert result.source_snapshot["tracked_count"] == 5
    assert result.source_snapshot["complete_candidates"] == 3
    assert result.source_snapshot["evaluated_candidates"] == 2
    assert result.exported_count == 2
    assert result.skipped_count == 0

    manifest = json.loads((p1_env.vault_dir / "export_manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_snapshot"]["tracked_count"] == 5
    assert manifest["source_snapshot"]["complete_candidates"] == 3
    assert manifest["source_snapshot"]["evaluated_candidates"] == 2
    assert manifest["exported_count"] == 2


# ---------------------------------------------------------------------------
# 7. Happy Path & Media Variations
# ---------------------------------------------------------------------------
def test_exporter_happy_path_multi_image(p1_env):
    note_id = "66ab001122"
    img1_bytes = b"fake_jpeg_1"
    img2_bytes = b"fake_png_2"

    canonical = {
        "platform": "xhs",
        "note_id": note_id,
        "source_url": f"https://www.xiaohongshu.com/explore/{note_id}",
        "collector": "chrome_collector_v1",
        "collected_at": "2026-09-17T10:00:00Z",
        "author": {
            "id": "author_888",
            "name": "摄影师阿强",
        },
        "content": {
            "title": "秋日银杏摄影指南",
            "text": "第一点：逆光拍摄。\n第二点：大光圈虚化。",
        },
        "media": [
            {
                "type": "image",
                "url": "https://img.xhscdn.com/1.jpg",
                "filename": "img_01.jpg",
                "size_bytes": len(img1_bytes),
            },
            {
                "type": "image",
                "url": "https://img.xhscdn.com/2.png",
                "filename": "img_02.png",
                "size_bytes": len(img2_bytes),
            },
        ],
        "stats": {
            "liked_count": 1200,
            "collected_count": 340,
            "comment_count": 45,
            "share_count": 12,
        },
    }

    p1_env.add_db_note(note_id, status="COMPLETE", title=canonical["content"]["title"])
    p1_env.create_note_artifacts(
        note_id,
        canonical,
        media_files={"img_01.jpg": img1_bytes, "img_02.png": img2_bytes},
    )

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )
    result = exporter.export()

    assert result.exported_count == 1
    assert result.skipped_count == 0
    assert result.failed_count == 0
    assert result.remote_coverage_proof == "UNPROVEN"

    # Verify Markdown note
    md_file = p1_env.vault_dir / "notes" / f"{note_id}.md"
    assert md_file.exists()
    content = md_file.read_text(encoding="utf-8")

    # Frontmatter validation
    assert 'title: "秋日银杏摄影指南"' in content
    assert f'note_id: "{note_id}"' in content
    assert 'author_name: "摄影师阿强"' in content
    assert 'author_id: "author_888"' in content
    assert "liked_count: 1200" in content
    assert "collected_count: 340" in content
    assert "tags:" not in content
    assert "publish_time:" not in content

    # Content validation
    assert "# 秋日银杏摄影指南" in content
    assert "[摄影师阿强](https://www.xiaohongshu.com/user/profile/author_888)" in content
    assert "第一点：逆光拍摄" in content
    assert f"![Image 1](../assets/{note_id}/img_01.jpg)" in content
    assert f"![Image 2](../assets/{note_id}/img_02.png)" in content

    # Assets validation
    asset1 = p1_env.vault_dir / "assets" / note_id / "img_01.jpg"
    asset2 = p1_env.vault_dir / "assets" / note_id / "img_02.png"
    assert asset1.exists() and asset1.read_bytes() == img1_bytes
    assert asset2.exists() and asset2.read_bytes() == img2_bytes

    # Manifest validation
    manifest_file = p1_env.vault_dir / "export_manifest.json"
    assert manifest_file.exists()
    manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest_data["exported_count"] == 1
    assert manifest_data["remote_coverage_proof"] == "UNPROVEN"
    assert manifest_data["source_snapshot"]["tracked_count"] == 1
    assert manifest_data["source_snapshot"]["complete_candidates"] == 1
    assert manifest_data["source_snapshot"]["evaluated_candidates"] == 1


def test_exporter_zero_media_note(p1_env):
    note_id = "text_only_001"
    canonical = {
        "platform": "xhs",
        "note_id": note_id,
        "author": {"name": "纯文字作者"},
        "content": {"title": "纯文本备忘", "text": "没有图片的纯文字笔记"},
        "media": [],
    }

    p1_env.add_db_note(note_id, status="COMPLETE", title=canonical["content"]["title"])
    p1_env.create_note_artifacts(note_id, canonical)

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )
    result = exporter.export()

    assert result.exported_count == 1
    md_file = p1_env.vault_dir / "notes" / f"{note_id}.md"
    assert md_file.exists()
    md_text = md_file.read_text(encoding="utf-8")
    assert "## Media" not in md_text
    assert "没有图片的纯文字笔记" in md_text
    assert not (p1_env.vault_dir / "assets" / note_id).exists()


def test_exporter_video_note(p1_env):
    note_id = "video_note_001"
    video_bytes = b"\x00\x00\x00\x18ftypmp42"
    canonical = {
        "platform": "xhs",
        "note_id": note_id,
        "author": {"name": "视频博主"},
        "content": {"title": "Vlog日常", "text": "周末生活记录"},
        "media": [
            {
                "type": "video",
                "filename": "vlog.mp4",
                "size_bytes": len(video_bytes),
            }
        ],
    }

    p1_env.add_db_note(note_id, status="COMPLETE", title=canonical["content"]["title"])
    p1_env.create_note_artifacts(note_id, canonical, media_files={"vlog.mp4": video_bytes})

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )
    result = exporter.export()

    assert result.exported_count == 1
    md_file = p1_env.vault_dir / "notes" / f"{note_id}.md"
    md_text = md_file.read_text(encoding="utf-8")
    assert f'<video controls src="../assets/{note_id}/vlog.mp4" width="100%"></video>' in md_text
    assert f"*(Video: [vlog.mp4](../assets/{note_id}/vlog.mp4))*" in md_text

    video_dst = p1_env.vault_dir / "assets" / note_id / "vlog.mp4"
    assert video_dst.exists()
    assert video_dst.read_bytes() == video_bytes


def test_exporter_title_and_body_fallbacks(p1_env):
    note_id = "fallback_note_001"
    canonical = {
        "platform": "xhs",
        "note_id": note_id,
        "author": {"name": "路人甲"},
        "content": {"title": "", "text": ""},
        "media": [],
    }

    p1_env.add_db_note(note_id, status="COMPLETE")
    p1_env.create_note_artifacts(note_id, canonical)

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )
    result = exporter.export()

    assert result.exported_count == 1
    md_file = p1_env.vault_dir / "notes" / f"{note_id}.md"
    md_text = md_file.read_text(encoding="utf-8")
    assert 'title: "无标题笔记"' in md_text
    assert "# 无标题笔记" in md_text
    assert "*(无文字内容)*" in md_text


def test_exporter_missing_canonical(p1_env):
    note_id = "missing_canon_001"
    p1_env.add_db_note(note_id, status="COMPLETE")

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )
    result = exporter.export()

    assert result.exported_count == 0
    assert result.skipped_count == 1
    assert result.skipped_notes[0]["reason"] == "MISSING_CANONICAL"


def test_exporter_invalid_canonical_json(p1_env):
    note_id = "bad_json_001"
    p1_env.add_db_note(note_id, status="COMPLETE")
    note_dir = p1_env.data_dir / note_id
    note_dir.mkdir(parents=True)
    (note_dir / "canonical.json").write_text("{corrupted json", encoding="utf-8")

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )
    result = exporter.export()

    assert result.exported_count == 0
    assert result.skipped_count == 1
    assert "INVALID_CANONICAL_JSON" in result.skipped_notes[0]["reason"]


def test_exporter_note_id_mismatch(p1_env):
    note_id = "db_note_123"
    canonical = {
        "platform": "xhs",
        "note_id": "different_note_456",
        "author": {"name": "博主"},
        "content": {"title": "标题", "text": "正文"},
    }
    p1_env.add_db_note(note_id, status="COMPLETE")
    p1_env.create_note_artifacts(note_id, canonical)

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )
    result = exporter.export()

    assert result.exported_count == 0
    assert result.skipped_count == 1
    assert "NOTE_ID_MISMATCH" in result.skipped_notes[0]["reason"]


def test_exporter_media_missing(p1_env):
    note_id = "media_missing_001"
    canonical = {
        "platform": "xhs",
        "note_id": note_id,
        "author": {"name": "博主"},
        "content": {"title": "图文", "text": "正文"},
        "media": [{"type": "image", "filename": "missing.jpg", "size_bytes": 100}],
    }
    p1_env.add_db_note(note_id, status="COMPLETE")
    p1_env.create_note_artifacts(note_id, canonical, media_files={})

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )
    result = exporter.export()

    assert result.exported_count == 0
    assert result.skipped_count == 1
    assert result.skipped_notes[0]["reason"] == "MEDIA_FILE_MISSING: missing.jpg"


def test_exporter_media_file_empty(p1_env):
    note_id = "media_empty_001"
    canonical = {
        "platform": "xhs",
        "note_id": note_id,
        "author": {"name": "博主"},
        "content": {"title": "图文", "text": "正文"},
        "media": [{"type": "image", "filename": "empty.jpg", "size_bytes": 10}],
    }
    p1_env.add_db_note(note_id, status="COMPLETE")
    p1_env.create_note_artifacts(note_id, canonical, media_files={"empty.jpg": b""})

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )
    result = exporter.export()

    assert result.exported_count == 0
    assert result.skipped_count == 1
    assert result.skipped_notes[0]["reason"] == "MEDIA_FILE_EMPTY: empty.jpg"


def test_exporter_media_size_mismatch(p1_env):
    note_id = "media_mismatch_001"
    canonical = {
        "platform": "xhs",
        "note_id": note_id,
        "author": {"name": "博主"},
        "content": {"title": "图文", "text": "正文"},
        "media": [{"type": "image", "filename": "tampered.jpg", "size_bytes": 1024}],
    }
    p1_env.add_db_note(note_id, status="COMPLETE")
    p1_env.create_note_artifacts(note_id, canonical, media_files={"tampered.jpg": b"1234"})

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )
    result = exporter.export()

    assert result.exported_count == 0
    assert result.skipped_count == 1
    assert "MEDIA_SIZE_MISMATCH: tampered.jpg" in result.skipped_notes[0]["reason"]


# ---------------------------------------------------------------------------
# 8. Idempotency & Duplicate Export (Preserves mtime on SHA-256 match)
# ---------------------------------------------------------------------------
def test_exporter_idempotency_and_no_redundant_media_copy(p1_env):
    note_id = "idempotent_001"
    img_bytes = b"image_payload_bytes"
    canonical = {
        "platform": "xhs",
        "note_id": note_id,
        "author": {"name": "博主"},
        "content": {"title": "幂等测试", "text": "内容"},
        "media": [{"type": "image", "filename": "photo.jpg", "size_bytes": len(img_bytes)}],
    }
    p1_env.add_db_note(note_id, status="COMPLETE")
    p1_env.create_note_artifacts(note_id, canonical, media_files={"photo.jpg": img_bytes})

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )

    # First run
    res1 = exporter.export()
    assert res1.exported_count == 1
    dst_asset = p1_env.vault_dir / "assets" / note_id / "photo.jpg"
    assert dst_asset.exists()
    mtime_run1 = dst_asset.stat().st_mtime_ns

    # Second run (duplicate export)
    res2 = exporter.export()
    assert res2.exported_count == 1
    assert res2.skipped_count == 0
    assert res2.failed_count == 0
    mtime_run2 = dst_asset.stat().st_mtime_ns

    # Asset was not re-copied / touched because SHA-256 matched
    assert mtime_run1 == mtime_run2


# ---------------------------------------------------------------------------
# 9. Interruption & Error Resilience (Atomic Manifest & Exception Handling)
# ---------------------------------------------------------------------------
def test_exporter_partial_failure_continues_and_manifest_recorded(p1_env):
    note1 = "note_good"
    note2 = "note_bad"

    for nid in [note1, note2]:
        p1_env.add_db_note(nid, status="COMPLETE")
        p1_env.create_note_artifacts(
            nid,
            {
                "platform": "xhs",
                "note_id": nid,
                "author": {"name": "A"},
                "content": {"title": f"Title {nid}", "text": "Body"},
                "media": [],
            },
        )

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )

    original_render = render_vault_markdown

    def faulty_render(canonical, nid):
        if nid == note2:
            raise RuntimeError("Simulated render crash")
        return original_render(canonical, nid)

    with patch("xhs_export.exporter.render_vault_markdown", side_effect=faulty_render):
        result = exporter.export()

    assert result.exported_count == 1
    assert result.failed_count == 1
    assert result.failed_notes[0]["note_id"] == note2
    assert "Simulated render crash" in result.failed_notes[0]["error"]

    assert (p1_env.vault_dir / "notes" / f"{note1}.md").exists()
    assert not (p1_env.vault_dir / "notes" / f"{note2}.md").exists()

    manifest = json.loads((p1_env.vault_dir / "export_manifest.json").read_text(encoding="utf-8"))
    assert manifest["exported_count"] == 1
    assert manifest["failed_count"] == 1


# ---------------------------------------------------------------------------
# 10. P1 Zero-Mutation Isolation Guard
# ---------------------------------------------------------------------------
def test_exporter_p1_zero_mutation_isolation_guard(p1_env):
    """Guarantees that running the exporter does NOT mutate sync.db or data/ artifacts."""
    note_id = "isolation_guard_note"
    img_content = b"isolation_media_payload"

    p1_env.add_db_note(note_id, status="COMPLETE")
    p1_env.create_note_artifacts(
        note_id,
        {
            "platform": "xhs",
            "note_id": note_id,
            "author": {"name": "Guard Author"},
            "content": {"title": "Isolation Test", "text": "Data preservation"},
            "media": [{"type": "image", "filename": "guard.jpg", "size_bytes": len(img_content)}],
        },
        media_files={"guard.jpg": img_content},
    )

    db_hash_before = _hash_file(p1_env.db_path)
    data_hashes_before = _hash_dir(p1_env.data_dir)

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )
    result = exporter.export()
    assert result.exported_count == 1

    db_hash_after = _hash_file(p1_env.db_path)
    data_hashes_after = _hash_dir(p1_env.data_dir)

    assert db_hash_before == db_hash_after, "P1 sync.db was mutated during export!"
    assert data_hashes_before == data_hashes_after, "P1 data directory was mutated during export!"

    with exporter._open_readonly_db() as ro_conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro_conn.execute("UPDATE notes SET status = 'TAMPERED'")


# ---------------------------------------------------------------------------
# 11. CLI Entry Point Tests
# ---------------------------------------------------------------------------
def test_cli_main_success(p1_env, monkeypatch, capsys):
    note_id = "cli_note_001"
    p1_env.add_db_note(note_id, status="COMPLETE")
    p1_env.create_note_artifacts(
        note_id,
        {
            "platform": "xhs",
            "note_id": note_id,
            "author": {"name": "CLI"},
            "content": {"title": "CLI Note", "text": "CLI"},
            "media": [],
        },
    )

    test_args = [
        "xhs-export",
        "--state-db",
        str(p1_env.db_path),
        "--data-dir",
        str(p1_env.data_dir),
        "--vault-dir",
        str(p1_env.vault_dir),
    ]
    monkeypatch.setattr("sys.argv", test_args)

    exit_code = cli_main()
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "=== Obsidian Vault Export Summary ===" in captured.out
    assert "Exported:             1" in captured.out
    assert "Remote Coverage:      UNPROVEN" in captured.out


def test_cli_main_db_not_found(tmp_path, monkeypatch, capsys):
    missing_db = tmp_path / "non_existent.db"
    test_args = [
        "xhs-export",
        "--state-db",
        str(missing_db),
    ]
    monkeypatch.setattr("sys.argv", test_args)

    exit_code = cli_main()
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "[ERROR] Export failed" in captured.err


def test_cli_main_boundary_violation_returns_error(p1_env, monkeypatch, capsys):
    test_args = [
        "xhs-export",
        "--state-db",
        str(p1_env.db_path),
        "--data-dir",
        str(p1_env.data_dir),
        "--vault-dir",
        str(p1_env.data_dir),  # invalid boundary!
    ]
    monkeypatch.setattr("sys.argv", test_args)

    exit_code = cli_main()
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "[ERROR] Export failed" in captured.err
    assert "cannot be inside or equal to data_dir" in captured.err


# ---------------------------------------------------------------------------
# 12. Security: Strip Sensitive Tracking Tokens (xsec_token, xsec_source)
# ---------------------------------------------------------------------------


def test_sanitize_note_url():
    # 1. URL with tracking query parameters and note_id
    raw_url = "https://www.xiaohongshu.com/explore/63fee6390000000027012206?xsec_token=ABQpoaZv3aXzxjOMf1hPtSD6q-QHnlVf_hD_DSfhJAhoM=&xsec_source=pc_fav"
    sanitized = sanitize_note_url(raw_url, "63fee6390000000027012206")
    assert sanitized == "https://www.xiaohongshu.com/explore/63fee6390000000027012206"
    assert "xsec_token" not in sanitized
    assert "xsec_source" not in sanitized

    # 2. URL with no note_id provided
    sanitized_no_id = sanitize_note_url(raw_url)
    assert sanitized_no_id == "https://www.xiaohongshu.com/explore/63fee6390000000027012206"
    assert "xsec_token" not in sanitized_no_id

    # 3. Already clean URL
    clean_url = "https://www.xiaohongshu.com/explore/63fee6390000000027012206"
    assert sanitize_note_url(clean_url, "63fee6390000000027012206") == clean_url

    # 4. Empty URL
    assert sanitize_note_url("", "") == ""
    assert sanitize_note_url("", "abc123") == "https://www.xiaohongshu.com/explore/abc123"

    # 5. Non-XHS URL with other query params preserved
    other_url = "https://example.com/page?foo=bar&xsec_token=secret123&baz=qux"
    assert sanitize_note_url(other_url) == "https://example.com/page?foo=bar&baz=qux"


def test_export_strips_xsec_token_from_markdown(p1_env):
    note_id = "6523f445000000001e028d64"
    raw_source = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=ABk40hyaZ61qSlK6YzqZzoNm8RkrxndZxbJhVO8zKXWd8=&xsec_source=pc_fav"

    p1_env.add_db_note(note_id=note_id, status="COMPLETE")
    p1_env.create_note_artifacts(
        note_id=note_id,
        canonical_data={
            "platform": "xhs",
            "note_id": note_id,
            "source_url": raw_source,
            "author": {"id": "auth1", "name": "作者A"},
            "content": {"title": "笔记标题", "text": "笔记正文内容"},
            "media": [],
            "stats": {"liked_count": 10},
        },
    )

    exporter = VaultExporter(
        state_db=p1_env.db_path,
        data_dir=p1_env.data_dir,
        vault_dir=p1_env.vault_dir,
    )
    res = exporter.export()
    assert res.exported_count == 1

    md_path = p1_env.vault_dir / "notes" / f"{note_id}.md"
    assert md_path.exists()
    content = md_path.read_text(encoding="utf-8")

    # Verify no tracking token leaks in frontmatter or body
    assert "xsec_token" not in content
    assert "xsec_source" not in content
    assert "ABk40hyaZ61qSlK6YzqZzoNm8RkrxndZxbJhVO8zKXWd8" not in content

    # Verify canonical bare URL is present
    bare_url = f"https://www.xiaohongshu.com/explore/{note_id}"
    assert f"source_url: \"{bare_url}\"" in content
    assert f"- **Source**: [{bare_url}]({bare_url})" in content

