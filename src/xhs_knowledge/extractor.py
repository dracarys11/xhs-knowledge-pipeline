"""EvidenceExtractor: Mechanical, non-semantic excerpt extractor for Phase C.2 MVP.

Splits content_text strictly by paragraph boundaries, taking the first N non-empty paragraphs.
Zero NLP, zero semantic ranking, zero heuristic trimming.
"""

from __future__ import annotations

import logging
from typing import Any

from xhs_knowledge.contracts import (
    EvidenceBundle,
    EvidenceExcerpt,
    SelectedNote,
)

logger = logging.getLogger(__name__)


class EvidenceExtractor:
    """Extracts verbatim paragraph excerpts mechanically from an EvidenceBundle."""

    def __init__(self, max_excerpts_per_note: int = 3) -> None:
        if max_excerpts_per_note <= 0:
            raise ValueError("max_excerpts_per_note must be positive")
        self.max_excerpts_per_note = max_excerpts_per_note

    def extract_note_excerpts(self, note: SelectedNote) -> list[EvidenceExcerpt]:
        """Extracts the first N non-empty paragraphs mechanically from a note.

        Algorithm:
        content_text -> split('\\n\\n') -> filter whitespace-only -> take first N raw paragraphs.
        strip() is used solely for empty checking; emitted quote is the exact raw paragraph.
        """
        content = note.content_text
        if not content or not content.strip():
            return []

        # Split by double newline paragraphs
        raw_paragraphs = content.split("\n\n")
        # Fallback to single newline if no double newlines exist
        if len(raw_paragraphs) == 1 and "\n" in content:
            raw_paragraphs = content.split("\n")

        excerpts: list[EvidenceExcerpt] = []
        for raw_para in raw_paragraphs:
            if not raw_para.strip():
                continue

            excerpts.append(
                EvidenceExcerpt(
                    note_id=note.note_id,
                    source_file_sha256=note.file_sha256,
                    verbatim_quote=raw_para,
                )
            )

            if len(excerpts) >= self.max_excerpts_per_note:
                break

        return excerpts

    def extract(self, bundle: EvidenceBundle) -> list[EvidenceExcerpt]:
        """Extracts verbatim excerpts across all notes in the EvidenceBundle."""
        all_excerpts: list[EvidenceExcerpt] = []
        for note in bundle.notes:
            all_excerpts.extend(self.extract_note_excerpts(note))
        return all_excerpts
