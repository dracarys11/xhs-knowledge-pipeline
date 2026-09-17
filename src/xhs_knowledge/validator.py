"""ProvenanceValidator: Decidable structural provenance validator for Phase C.2 MVP.

Validates that EvidenceExcerpt instances:
1. Belong to an admitted SelectedNote in the EvidenceBundle (INVALID_NOTE_REFERENCE).
2. Match the admitted note's source file SHA256 (SOURCE_HASH_MISMATCH).
3. Exist verbatim as an exact substring in SelectedNote.content_text (QUOTE_NOT_FOUND).

Zero NLP, zero fuzzy matching, zero embeddings, zero semantic judgment.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from xhs_knowledge.contracts import (
    EvidenceBundle,
    EvidenceExcerpt,
)


@dataclass
class ValidationError:
    """Audit record for an excerpt that failed structural provenance verification."""
    code: str
    note_id: str
    message: str
    quote_preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationResult:
    """Structured result of provenance validation over a set of excerpts."""
    status: str  # "PASS" | "FAILED"
    validated_excerpts: list[EvidenceExcerpt]
    omitted_excerpts: list[EvidenceExcerpt]
    validated_count: int
    failed_count: int
    errors: list[ValidationError] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return self.status == "PASS" and self.failed_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "validated_count": self.validated_count,
            "failed_count": self.failed_count,
            "errors": [e.to_dict() for e in self.errors],
        }


class ProvenanceValidator:
    """Validates structural provenance of EvidenceExcerpt objects against an EvidenceBundle."""

    def validate_excerpt(
        self, excerpt: EvidenceExcerpt, bundle: EvidenceBundle
    ) -> tuple[bool, ValidationError | None]:
        """Validates a single EvidenceExcerpt against the EvidenceBundle.

        Checks:
        1. note_id in bundle.notes -> INVALID_NOTE_REFERENCE
        2. source_file_sha256 == note.file_sha256 -> SOURCE_HASH_MISMATCH
        3. verbatim_quote in note.content_text -> QUOTE_NOT_FOUND
        """
        note_map = {n.note_id: n for n in bundle.notes}

        # 1. Note existence in bundle
        if excerpt.note_id not in note_map:
            return False, ValidationError(
                code="INVALID_NOTE_REFERENCE",
                note_id=excerpt.note_id,
                message=f"Excerpt cites note_id '{excerpt.note_id}' not present in current EvidenceBundle.",
                quote_preview=excerpt.verbatim_quote[:50],
            )

        note = note_map[excerpt.note_id]

        # 2. Cryptographic hash identity
        if excerpt.source_file_sha256 != note.file_sha256:
            return False, ValidationError(
                code="SOURCE_HASH_MISMATCH",
                note_id=excerpt.note_id,
                message=(
                    f"Excerpt source_file_sha256 ({excerpt.source_file_sha256[:12]}...) "
                    f"does not match bundle note file_sha256 ({note.file_sha256[:12]}...)."
                ),
                quote_preview=excerpt.verbatim_quote[:50],
            )

        # 3. Exact character-for-character substring verification
        if excerpt.verbatim_quote not in note.content_text:
            return False, ValidationError(
                code="QUOTE_NOT_FOUND",
                note_id=excerpt.note_id,
                message="verbatim_quote was not found as an exact substring in SelectedNote.content_text.",
                quote_preview=excerpt.verbatim_quote[:50],
            )

        return True, None

    def validate(
        self, excerpts: list[EvidenceExcerpt], bundle: EvidenceBundle
    ) -> ValidationResult:
        """Validates a list of excerpts deterministically against an EvidenceBundle."""
        validated: list[EvidenceExcerpt] = []
        omitted: list[EvidenceExcerpt] = []
        errors: list[ValidationError] = []

        for excerpt in excerpts:
            ok, err = self.validate_excerpt(excerpt, bundle)
            if ok:
                validated.append(excerpt)
            else:
                omitted.append(excerpt)
                if err is not None:
                    errors.append(err)

        status = "PASS" if len(errors) == 0 else "FAILED"

        return ValidationResult(
            status=status,
            validated_excerpts=validated,
            omitted_excerpts=omitted,
            validated_count=len(validated),
            failed_count=len(omitted),
            errors=errors,
        )
