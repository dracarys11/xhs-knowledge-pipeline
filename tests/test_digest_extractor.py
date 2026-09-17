"""Unit tests for Phase C.2 EvidenceExtractor (Module 1 - Mechanical Chunking).

Verifies:
1. Pure mechanical extraction: split("\\n\\n") -> filter empty -> top N raw paragraphs.
2. Raw paragraph whitespace preservation (verbatim fidelity).
3. Newline fallback behavior when no double newlines exist.
4. Input immutability of EvidenceBundle and SelectedNote.
5. Exact substring invariant: verbatim_quote in note.content_text.
6. Unicode & Emoji preservation: e.g. "✨🍜🍣".
7. Deterministic reproducibility across runs.
8. Real VaultRetriever bundle extraction.
"""

import copy
from pathlib import Path
import pytest

from xhs_knowledge.contracts import (
    CollectionMembership,
    DigestRequest,
    EvidenceBundle,
    EvidenceExcerpt,
    SelectedNote,
    SelectionConfig,
    SourceConfig,
)
from xhs_knowledge.extractor import EvidenceExtractor
from xhs_knowledge.retriever import VaultRetriever


@pytest.fixture
def sample_note() -> SelectedNote:
    content = (
        "第一段：关于大型语言模型的评测方法与实践总结。\n\n"
        "第二段：分布式系统的核心在于在不可靠的网络上构建可靠的状态机。\n\n"
        "短文本✨🍜\n\n"
        "第四段：命令行工具在开发者的本地工作流中能提供极高的确定性。"
    )
    return SelectedNote(
        note_id="note_test_001",
        title="测试笔记",
        author_name="测试作者",
        primary_collection="tech",
        vault_collection_position=1,
        memberships=[
            CollectionMembership(
                collection_id="col_001",
                collection_name="tech",
                vault_collection_position=1,
            )
        ],
        content_text=content,
        file_path="notes/note_test_001.md",
        file_sha256="aabbccddeeff11223344556677889900aabbccddeeff11223344556677889900",
    )


def test_extractor_mechanical_chunking_and_substring_invariant(sample_note: SelectedNote):
    """Extractor extracts paragraphs in mechanical order without heuristic filtering."""
    extractor = EvidenceExtractor(max_excerpts_per_note=3)
    excerpts = extractor.extract_note_excerpts(sample_note)

    assert len(excerpts) == 3

    # Paragraph 1
    assert excerpts[0].note_id == sample_note.note_id
    assert excerpts[0].source_file_sha256 == sample_note.file_sha256
    assert excerpts[0].verbatim_quote == "第一段：关于大型语言模型的评测方法与实践总结。"
    assert excerpts[0].verbatim_quote in sample_note.content_text

    # Paragraph 2
    assert excerpts[1].verbatim_quote == "第二段：分布式系统的核心在于在不可靠的网络上构建可靠的状态机。"
    assert excerpts[1].verbatim_quote in sample_note.content_text

    # Paragraph 3: Short paragraph with Emoji preserved
    assert excerpts[2].verbatim_quote == "短文本✨🍜"
    assert excerpts[2].verbatim_quote in sample_note.content_text


def test_extractor_whitespace_fidelity():
    """Verbatim quote must preserve internal and surrounding whitespace of raw paragraphs exactly."""
    note_with_ws = SelectedNote(
        note_id="ws_001",
        title="空白测试",
        author_name="作者",
        primary_collection="c",
        vault_collection_position=1,
        memberships=[],
        content_text="  第一段保留前导与后置空格  \n\n\t第二段带Tab缩进\t\n\n   \n\n第三段",
        file_path="notes/ws.md",
        file_sha256="sha_ws_123",
    )
    extractor = EvidenceExtractor(max_excerpts_per_note=3)
    excerpts = extractor.extract_note_excerpts(note_with_ws)

    assert len(excerpts) == 3
    # Exactly verbatim raw paragraph strings:
    assert excerpts[0].verbatim_quote == "  第一段保留前导与后置空格  "
    assert excerpts[1].verbatim_quote == "\t第二段带Tab缩进\t"
    # Third non-empty paragraph (empty whitespace block skipped)
    assert excerpts[2].verbatim_quote == "第三段"

    for e in excerpts:
        assert e.verbatim_quote in note_with_ws.content_text


def test_extractor_newline_fallback():
    """When no double newlines exist, falls back to splitting by single newline."""
    single_nl_note = SelectedNote(
        note_id="nl_001",
        title="单换行笔记",
        author_name="作者",
        primary_collection="c",
        vault_collection_position=1,
        memberships=[],
        content_text="第一行内容\n第二行内容\n\n第三行内容",  # Has \n\n so splits by \n\n
        file_path="notes/nl.md",
        file_sha256="sha_nl_123",
    )
    pure_single_nl_note = SelectedNote(
        note_id="pure_nl_001",
        title="纯单换行笔记",
        author_name="作者",
        primary_collection="c",
        vault_collection_position=1,
        memberships=[],
        content_text="第一行内容\n第二行内容\n第三行内容",  # No \n\n, pure single \n
        file_path="notes/pure_nl.md",
        file_sha256="sha_pnl_123",
    )

    extractor = EvidenceExtractor(max_excerpts_per_note=3)
    excerpts = extractor.extract_note_excerpts(pure_single_nl_note)
    assert len(excerpts) == 3
    assert excerpts[0].verbatim_quote == "第一行内容"
    assert excerpts[1].verbatim_quote == "第二行内容"
    assert excerpts[2].verbatim_quote == "第三行内容"


def test_extractor_input_immutability(sample_note: SelectedNote):
    """Calling extract must not mutate EvidenceBundle or its SelectedNotes in place."""
    bundle = EvidenceBundle(
        bundle_id="bundle_immut_01",
        request_fingerprint="fp_immut_01",
        created_at="2026-09-18T00:00:00Z",
        total_notes=1,
        notes=[sample_note],
    )

    before_dict = copy.deepcopy(bundle.to_dict())
    extractor = EvidenceExtractor(max_excerpts_per_note=3)
    excerpts = extractor.extract(bundle)
    assert len(excerpts) > 0

    after_dict = bundle.to_dict()
    assert before_dict == after_dict, "EvidenceBundle or SelectedNote was mutated during extract()!"


def test_extractor_unicode_and_emoji_handling():
    """Unicode emojis and multilingual characters are preserved without corruption."""
    emoji_note = SelectedNote(
        note_id="emoji_001",
        title="东京美食✨",
        author_name="食客",
        primary_collection="food",
        vault_collection_position=1,
        memberships=[],
        content_text="新宿必吃拉面🍜🍣！\n\n汤底非常浓郁✨，建议早点去排队！\n\n好吃😋",
        file_path="notes/emoji.md",
        file_sha256="hash123",
    )
    extractor = EvidenceExtractor(max_excerpts_per_note=3)
    excerpts = extractor.extract_note_excerpts(emoji_note)
    assert len(excerpts) == 3
    assert excerpts[0].verbatim_quote == "新宿必吃拉面🍜🍣！"
    assert excerpts[1].verbatim_quote == "汤底非常浓郁✨，建议早点去排队！"
    assert excerpts[2].verbatim_quote == "好吃😋"


def test_extractor_empty_content():
    """Empty or whitespace content returns empty list without error."""
    empty_note = SelectedNote(
        note_id="empty_001",
        title="空",
        author_name="",
        primary_collection="c",
        vault_collection_position=1,
        memberships=[],
        content_text="   \n\n   ",
        file_path="notes/empty.md",
        file_sha256="123456",
    )
    extractor = EvidenceExtractor()
    excerpts = extractor.extract_note_excerpts(empty_note)
    assert excerpts == []


def test_extractor_bundle_extraction(sample_note: SelectedNote):
    """Bundle extraction iterates all admitted notes deterministically."""
    bundle = EvidenceBundle(
        bundle_id="bundle_test_01",
        request_fingerprint="fp_001",
        created_at="2026-09-18T00:00:00Z",
        total_notes=1,
        notes=[sample_note],
    )
    extractor = EvidenceExtractor(max_excerpts_per_note=3)
    excerpts = extractor.extract(bundle)
    assert len(excerpts) == 3

    # Multi-run deterministic check
    for _ in range(5):
        rerun_excerpts = extractor.extract(bundle)
        assert len(rerun_excerpts) == len(excerpts)
        for e1, e2 in zip(excerpts, rerun_excerpts):
            assert e1.note_id == e2.note_id
            assert e1.source_file_sha256 == e2.source_file_sha256
            assert e1.verbatim_quote == e2.verbatim_quote


def test_extractor_real_vault_integration():
    """Extracts excerpts from real repository Vault bundle and verifies substring invariant."""
    vault_path = Path("Vault")
    if not vault_path.exists():
        pytest.skip("Vault directory not present")

    retriever = VaultRetriever(vault_dir=vault_path, allow_unisolated_vault=False)
    req = DigestRequest(
        digest_name="real_extract_test",
        target_date="2026-09-17",
        source=SourceConfig(collections=["coding"]),
        selection=SelectionConfig(max_notes=3),
    )
    notes = retriever.retrieve(req)
    bundle = retriever.assemble_bundle(req, notes)

    extractor = EvidenceExtractor(max_excerpts_per_note=2)
    excerpts = extractor.extract(bundle)

    assert len(excerpts) > 0
    note_map = {n.note_id: n for n in bundle.notes}

    for excerpt in excerpts:
        note = note_map[excerpt.note_id]
        assert excerpt.source_file_sha256 == note.file_sha256
        assert excerpt.verbatim_quote in note.content_text
