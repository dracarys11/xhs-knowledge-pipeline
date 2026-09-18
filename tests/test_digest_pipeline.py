"""End-to-End Acceptance Tests for Phase C MVP Digest Pipeline.

Verifies the complete closed-loop pipeline across all Phase C modules:
    DigestRequest
    → VaultRetriever.retrieve()
    → retriever.assemble_bundle()
    → EvidenceExtractor.extract()
    → ProvenanceValidator.validate()
    → DigestWriter.write()
    → current.json + immutable generations/

Acceptance criteria:
1. test_phase_c_mvp_e2e_happy_path:
   Full end-to-end execution on a hermetic synthetic Vault fixture.
   Asserts sorting invariant, verbatim evidence preservation, trust boundary,
   and exact file publication schema.
2. test_phase_c_mvp_e2e_reproducibility:
   Deterministic double-run with identical inputs produces byte-identical
   digest.md and manifest.json, identical generation IDs, and zero duplicate directories.
3. test_phase_c_mvp_e2e_does_not_mutate_p1_storage:
   Verifies zero mutation during this run over real workspace P1 storage (.xhs-state/sync.db and data/ tree)
   when running against the real Vault collection.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
import pytest

from xhs_knowledge.contracts import (
    DigestRequest,
    EvidenceBundle,
    EvidenceExcerpt,
    SelectionConfig,
    SourceConfig,
)
from xhs_knowledge.extractor import EvidenceExtractor
from xhs_knowledge.retriever import VaultRetriever
from xhs_knowledge.validator import ProvenanceValidator, ValidationResult
from xhs_knowledge.writer import (
    CURRENT_POINTER_FILENAME,
    GENERATION_MANIFEST_FILENAME,
    GENERATION_MARKDOWN_FILENAME,
    GENERATIONS_DIRNAME,
    DigestWriter,
    DigestWriterResult,
)


def compute_file_sha256(path: Path) -> str:
    """Computes SHA256 hex digest of a single file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def compute_dir_tree_sha256(directory: Path) -> str:
    """Computes a deterministic hash over all files in a directory tree."""
    if not directory.exists():
        return ""
    hashes: list[str] = []
    for root, dirs, files in os.walk(directory):
        dirs.sort()
        for f in sorted(files):
            p = Path(root) / f
            hashes.append(f"{p.relative_to(directory)}:{compute_file_sha256(p)}")
    return hashlib.sha256("\n".join(hashes).encode("utf-8")).hexdigest()


@pytest.fixture
def hermetic_vault(tmp_path: Path) -> Path:
    """Creates an isolated synthetic Vault fixture with valid notes and a collection."""
    vault = tmp_path / "Vault"
    notes_dir = vault / "notes"
    colls_dir = vault / "collections"
    notes_dir.mkdir(parents=True)
    colls_dir.mkdir(parents=True)

    # Note 1: Architecture note with multiple paragraphs
    (notes_dir / "note_001.md").write_text(
        "---\n"
        'note_id: "note_001"\n'
        'title: "系统架构与一致性"\n'
        'author_name: "张工"\n'
        "---\n\n"
        "# 系统架构与一致性\n\n"
        "## Content\n\n"
        "分布式系统的核心是在不可靠网络上保证数据一致性与可用性。\n\n"
        "CAP定理与BASE理论构成了现代系统设计的基石。\n",
        encoding="utf-8",
    )

    # Note 2: Evaluation note with multiple paragraphs
    (notes_dir / "note_002.md").write_text(
        "---\n"
        'note_id: "note_002"\n'
        'title: "评测集工程实践"\n'
        'author_name: "李工"\n'
        "---\n\n"
        "# 评测集工程实践\n\n"
        "## Content\n\n"
        "确定性与可复现性是评估AI合成质量的第一原则。\n\n"
        "所有断言必须基于物理事实而非概率预期。\n",
        encoding="utf-8",
    )

    # Collection: engineering (referencing note_001 at pos 1, note_002 at pos 2)
    (colls_dir / "engineering.md").write_text(
        "---\n"
        'collection_id: "col_eng_001"\n'
        'name: "engineering"\n'
        "total_notes: 2\n"
        "---\n\n"
        "# engineering\n\n"
        "## 收藏笔记\n\n"
        "- [[note_001|系统架构与一致性]] — *张工*\n"
        "- [[note_002|评测集工程实践]] — *李工*\n",
        encoding="utf-8",
    )

    return vault


def test_phase_c_mvp_e2e_happy_path(hermetic_vault: Path) -> None:
    """True E2E test verifying all Phase C pipeline stages in sequence."""
    # 1. DigestRequest
    request = DigestRequest(
        digest_name="mvp_daily",
        target_date="2026-09-18",
        source=SourceConfig(collections=["engineering"]),
        selection=SelectionConfig(max_notes=10),
    )

    # 2. VaultRetriever
    retriever = VaultRetriever(vault_dir=hermetic_vault, allow_unisolated_vault=True)
    retrieved_notes = retriever.retrieve(request)

    assert len(retrieved_notes) >= 2
    assert len(set(n.note_id for n in retrieved_notes)) == len(retrieved_notes)
    assert len(retrieved_notes) <= request.selection.max_notes
    # Invariant: selection ordering preserved (vault_collection_position ASC, note_id ASC)
    actual_order = [(n.vault_collection_position, n.note_id) for n in retrieved_notes]
    assert actual_order == sorted(actual_order)

    # 3. assemble_bundle
    bundle = retriever.assemble_bundle(request, retrieved_notes)
    assert isinstance(bundle, EvidenceBundle)
    assert bundle.total_notes == len(retrieved_notes)
    assert len(bundle.notes) == len(retrieved_notes)
    assert bundle.bundle_content_hash != ""
    assert bundle.request_fingerprint == request.compute_fingerprint()
    assert bundle.bundle_content_hash == bundle.compute_content_hash()

    # 4. EvidenceExtractor
    extractor = EvidenceExtractor(max_excerpts_per_note=2)
    excerpts = extractor.extract(bundle)
    assert len(excerpts) > 0

    # Invariant: verbatim quotes must match exact raw content from source notes
    note_content_map = {n.note_id: n.content_text for n in bundle.notes}
    note_sha_map = {n.note_id: n.file_sha256 for n in bundle.notes}
    for exc in excerpts:
        assert isinstance(exc, EvidenceExcerpt)
        assert exc.note_id in note_content_map
        assert exc.verbatim_quote in note_content_map[exc.note_id]
        assert exc.source_file_sha256 == note_sha_map[exc.note_id]

    # 5. ProvenanceValidator
    validator = ProvenanceValidator()
    validation_result = validator.validate(excerpts, bundle)
    assert isinstance(validation_result, ValidationResult)
    assert validation_result.status == "PASS"
    assert validation_result.is_valid is True
    assert validation_result.failed_count == 0
    assert validation_result.validated_count == len(excerpts)
    assert len(validation_result.errors) == 0

    # 6. DigestWriter
    writer = DigestWriter(vault_dir=hermetic_vault)
    write_result = writer.write(request, bundle, validation_result)

    assert isinstance(write_result, DigestWriterResult)
    assert write_result.artifact_status == "COMPLETE"
    assert write_result.verified_excerpts_count == len(excerpts)
    assert write_result.omitted_excerpts_count == 0
    assert bool(write_result.generation_id)

    # Verify physical file publication layout
    artifact_key = f"{request.target_date}_{request.digest_name}"
    expected_artifact_dir = hermetic_vault / "digests" / artifact_key
    expected_pointer_path = expected_artifact_dir / CURRENT_POINTER_FILENAME
    expected_generation_dir = (
        expected_artifact_dir / GENERATIONS_DIRNAME / write_result.generation_id
    )

    assert expected_artifact_dir.is_dir()
    assert expected_pointer_path.is_file()
    assert not expected_pointer_path.is_symlink()
    assert expected_generation_dir.is_dir()
    assert not expected_generation_dir.is_symlink()

    # Verify current pointer schema
    pointer_data = json.loads(expected_pointer_path.read_text(encoding="utf-8"))
    assert pointer_data["pointer_version"] == "1.0"
    assert pointer_data["artifact_key"] == artifact_key
    assert pointer_data["generation_id"] == write_result.generation_id
    assert pointer_data["artifact_status"] == "COMPLETE"
    assert pointer_data["content_mode"] == "VERIFIED_SOURCE_EXCERPTS"
    assert (
        pointer_data["manifest"]
        == f"{GENERATIONS_DIRNAME}/{write_result.generation_id}/{GENERATION_MANIFEST_FILENAME}"
    )
    assert (
        pointer_data["markdown"]
        == f"{GENERATIONS_DIRNAME}/{write_result.generation_id}/{GENERATION_MARKDOWN_FILENAME}"
    )

    # Verify generation files exist
    assert write_result.markdown_path is not None
    assert write_result.markdown_path.is_file()
    assert write_result.manifest_path.is_file()
    assert write_result.markdown_path == expected_generation_dir / GENERATION_MARKDOWN_FILENAME
    assert write_result.manifest_path == expected_generation_dir / GENERATION_MANIFEST_FILENAME

    artifact_dir_resolved = expected_artifact_dir.resolve()
    assert write_result.manifest_path.resolve().is_relative_to(artifact_dir_resolved)
    assert write_result.markdown_path.resolve().is_relative_to(artifact_dir_resolved)
    assert not write_result.manifest_path.is_symlink()
    assert not write_result.markdown_path.is_symlink()

    # Verify manifest contents and exact string round-trip
    manifest_data = json.loads(write_result.manifest_path.read_text(encoding="utf-8"))
    assert manifest_data["manifest_version"] == "1.0"
    assert manifest_data["digest_name"] == request.digest_name
    assert manifest_data["target_date"] == request.target_date
    assert manifest_data["source_collection"] == "engineering"
    assert manifest_data["bundle_id"] == bundle.bundle_id
    assert manifest_data["request_fingerprint"] == bundle.request_fingerprint
    assert manifest_data["bundle_content_hash"] == bundle.bundle_content_hash
    assert manifest_data["artifact_status"] == "COMPLETE"
    assert manifest_data["verified_excerpts_count"] == len(excerpts)
    assert manifest_data["omitted_excerpts_count"] == 0
    assert len(manifest_data["verified_excerpts"]) == len(excerpts)

    for orig_exc, saved_exc in zip(excerpts, manifest_data["verified_excerpts"]):
        assert saved_exc["note_id"] == orig_exc.note_id
        assert saved_exc["source_file_sha256"] == orig_exc.source_file_sha256
        assert saved_exc["verbatim_quote"] == orig_exc.verbatim_quote

    # Verify markdown formatting and citations
    md_content = write_result.markdown_path.read_text(encoding="utf-8")
    assert f"# Knowledge Digest: {request.digest_name} ({request.target_date})" in md_content
    assert f"- **Source Collection**: `{request.source.collections[0]}`" in md_content
    assert "## 📌 Verified Source Excerpts" in md_content
    for note in retrieved_notes:
        assert f"[[{note.note_id}|{note.title}]]" in md_content
    for exc in excerpts:
        first_line = exc.verbatim_quote.splitlines()[0]
        assert first_line in md_content
    assert "## 📊 Digest Provenance & Audit" in md_content
    assert GENERATION_MANIFEST_FILENAME in md_content


def test_phase_c_mvp_e2e_reproducibility(hermetic_vault: Path) -> None:
    """Verifies that running the pipeline twice with identical inputs yields byte-identical artifacts."""
    request = DigestRequest(
        digest_name="reproducible_digest",
        target_date="2026-09-18",
        source=SourceConfig(collections=["engineering"]),
        selection=SelectionConfig(max_notes=5),
    )
    fixed_timestamp = "2026-09-18T00:00:00Z"

    # --- Run 1 ---
    retriever_1 = VaultRetriever(vault_dir=hermetic_vault, allow_unisolated_vault=True)
    notes_1 = retriever_1.retrieve(request)
    bundle_1 = retriever_1.assemble_bundle(request, notes_1, created_at=fixed_timestamp)
    extractor_1 = EvidenceExtractor(max_excerpts_per_note=2)
    excerpts_1 = extractor_1.extract(bundle_1)
    validator_1 = ProvenanceValidator()
    val_1 = validator_1.validate(excerpts_1, bundle_1)
    writer_1 = DigestWriter(vault_dir=hermetic_vault)
    write_res_1 = writer_1.write(request, bundle_1, val_1, generated_at=fixed_timestamp)

    # Immediately snapshot Run 1 outputs and state
    notes_order_1 = [(n.vault_collection_position, n.note_id) for n in notes_1]
    bundle_hash_1 = bundle_1.bundle_content_hash
    excerpts_1_snapshot = [
        (exc.note_id, exc.source_file_sha256, exc.verbatim_quote) for exc in excerpts_1
    ]
    val_1_snapshot = (
        val_1.status,
        val_1.validated_count,
        val_1.failed_count,
    )
    assert write_res_1.markdown_path is not None
    md_bytes_1 = write_res_1.markdown_path.read_bytes()
    manifest_bytes_1 = write_res_1.manifest_path.read_bytes()
    pointer_bytes_1 = write_res_1.current_pointer_path.read_bytes()

    # --- Run 2 ---
    retriever_2 = VaultRetriever(vault_dir=hermetic_vault, allow_unisolated_vault=True)
    notes_2 = retriever_2.retrieve(request)
    bundle_2 = retriever_2.assemble_bundle(request, notes_2, created_at=fixed_timestamp)
    extractor_2 = EvidenceExtractor(max_excerpts_per_note=2)
    excerpts_2 = extractor_2.extract(bundle_2)
    validator_2 = ProvenanceValidator()
    val_2 = validator_2.validate(excerpts_2, bundle_2)
    writer_2 = DigestWriter(vault_dir=hermetic_vault)
    write_res_2 = writer_2.write(request, bundle_2, val_2, generated_at=fixed_timestamp)

    # Read Run 2 outputs
    notes_order_2 = [(n.vault_collection_position, n.note_id) for n in notes_2]
    bundle_hash_2 = bundle_2.bundle_content_hash
    excerpts_2_snapshot = [
        (exc.note_id, exc.source_file_sha256, exc.verbatim_quote) for exc in excerpts_2
    ]
    val_2_snapshot = (
        val_2.status,
        val_2.validated_count,
        val_2.failed_count,
    )
    assert write_res_2.markdown_path is not None
    md_bytes_2 = write_res_2.markdown_path.read_bytes()
    manifest_bytes_2 = write_res_2.manifest_path.read_bytes()
    pointer_bytes_2 = write_res_2.current_pointer_path.read_bytes()

    # Assert intermediate pipeline identity across runs
    assert notes_order_1 == notes_order_2
    assert bundle_hash_1 == bundle_hash_2
    assert excerpts_1_snapshot == excerpts_2_snapshot
    assert val_1_snapshot == val_2_snapshot

    # Assertions on writer results
    assert write_res_1.generation_id == write_res_2.generation_id
    assert write_res_1.markdown_path == write_res_2.markdown_path
    assert write_res_1.manifest_path == write_res_2.manifest_path
    assert write_res_1.current_pointer_path == write_res_2.current_pointer_path
    assert write_res_1.markdown_sha256 == write_res_2.markdown_sha256
    assert write_res_1.manifest_sha256 == write_res_2.manifest_sha256

    # Generation ID formula contract lock
    expected_gen_id = f"gen_{hashlib.sha256(manifest_bytes_1).hexdigest()}"
    assert write_res_1.generation_id == expected_gen_id
    assert write_res_2.generation_id == expected_gen_id

    # Byte-for-byte identity of published files and pointer
    assert md_bytes_1 == md_bytes_2
    assert manifest_bytes_1 == manifest_bytes_2
    assert pointer_bytes_1 == pointer_bytes_2

    # File on disk matches memory bytes and returned hashes
    assert compute_file_sha256(write_res_1.markdown_path) == hashlib.sha256(md_bytes_1).hexdigest()
    assert compute_file_sha256(write_res_1.manifest_path) == hashlib.sha256(manifest_bytes_1).hexdigest()
    assert write_res_1.markdown_sha256 == hashlib.sha256(md_bytes_1).hexdigest()
    assert write_res_1.manifest_sha256 == hashlib.sha256(manifest_bytes_1).hexdigest()

    # Current pointer targets the identical generation ID
    pointer_data = json.loads(pointer_bytes_2.decode("utf-8"))
    assert pointer_data["generation_id"] == write_res_1.generation_id

    # Invariant: No duplicate or dangling generation directories created
    artifact_key = f"{request.target_date}_{request.digest_name}"
    generations_dir = hermetic_vault / "digests" / artifact_key / GENERATIONS_DIRNAME
    gen_subdirs = [p for p in generations_dir.iterdir() if p.is_dir()]
    assert len(gen_subdirs) == 1
    assert gen_subdirs[0].name == write_res_1.generation_id


def test_phase_c_mvp_e2e_does_not_mutate_p1_storage(tmp_path: Path) -> None:
    """Verifies that running the pipeline against real Vault notes guarantees zero mutation to P1 storage."""
    required = [
        Path(".xhs-state/sync.db"),
        Path("data"),
        Path("Vault/collections/coding.md"),
        Path("Vault/notes"),
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        pytest.skip(f"Real local P1 workspace unavailable (missing: {', '.join(missing)})")

    real_state_db = Path(".xhs-state/sync.db")
    real_data_dir = Path("data")

    # Pre-execution cryptographic snapshot
    pre_db_sha256 = compute_file_sha256(real_state_db)
    pre_data_sha256 = compute_dir_tree_sha256(real_data_dir)

    # Isolated target vault for writer output to guarantee zero pollution of working Vault
    tmp_output_vault = tmp_path / "isolated_output_vault"
    tmp_output_vault.mkdir(parents=True)

    request = DigestRequest(
        digest_name="p1_safety_acceptance",
        target_date="2026-09-18",
        source=SourceConfig(collections=["coding"]),
        selection=SelectionConfig(max_notes=5),
    )

    # VaultRetriever operates against real Vault, bound by data_dir and state_dir
    retriever = VaultRetriever(
        vault_dir=Path("Vault"),
        data_dir=Path("data"),
        state_dir=Path(".xhs-state"),
    )
    retrieved_notes = retriever.retrieve(request)
    assert len(retrieved_notes) > 0

    bundle = retriever.assemble_bundle(request, retrieved_notes)
    extractor = EvidenceExtractor(max_excerpts_per_note=2)
    excerpts = extractor.extract(bundle)
    assert len(excerpts) > 0

    validator = ProvenanceValidator()
    validation_result = validator.validate(excerpts, bundle)
    assert validation_result.status == "PASS"
    assert validation_result.is_valid is True

    # Output writer strictly bound to isolated temporary vault
    writer = DigestWriter(vault_dir=tmp_output_vault)
    write_result = writer.write(request, bundle, validation_result)

    assert write_result.artifact_status == "COMPLETE"
    assert write_result.verified_excerpts_count > 0

    # Post-execution cryptographic comparison
    post_db_sha256 = compute_file_sha256(real_state_db)
    post_data_sha256 = compute_dir_tree_sha256(real_data_dir)

    assert post_db_sha256 == pre_db_sha256, (
        f".xhs-state/sync.db was mutated! Before: {pre_db_sha256}, After: {post_db_sha256}"
    )
    assert post_data_sha256 == pre_data_sha256, (
        f"data/ directory tree was mutated! Before: {pre_data_sha256}, After: {post_data_sha256}"
    )

    # Verify git status to ensure working tree was untouched
    git_res = subprocess.run(
        ["git", "status", "--porcelain", "data", ".xhs-state"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert git_res.stdout.strip() == "", f"Git status detected changes in P1 paths: {git_res.stdout}"
