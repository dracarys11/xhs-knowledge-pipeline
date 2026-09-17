"""ProvenanceValidator: Decidable structural provenance validator for Phase C.2 MVP.

Validates that EvidenceExcerpt instances:
1. Belong to an admitted SelectedNote in the EvidenceBundle (INVALID_NOTE_REFERENCE).
2. Match the admitted note's source file SHA256 (SOURCE_HASH_MISMATCH).
3. Exist verbatim as an exact substring in SelectedNote.content_text (QUOTE_NOT_FOUND).

Fails closed on:
- Empty evidence inputs (EMPTY_EVIDENCE)
- Malformed excerpt objects or invalid types (MALFORMED_EXCERPT)

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
    omitted_excerpts: list[Any]
    validated_count: int
    failed_count: int
    errors: list[ValidationError] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return self.status == "PASS" and self.failed_count == 0 and self.validated_count > 0

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
        self, excerpt: Any, bundle: EvidenceBundle
    ) -> tuple[bool, ValidationError | None]:
        """Validates a single candidate excerpt against the EvidenceBundle.

        Checks:
        0. Safe malformed input guards -> MALFORMED_EXCERPT
        1. note_id in bundle.notes -> INVALID_NOTE_REFERENCE
        2. source_file_sha256 == note.file_sha256 -> SOURCE_HASH_MISMATCH
        3. verbatim_quote in note.content_text -> QUOTE_NOT_FOUND
        """
        if not isinstance(bundle, EvidenceBundle):
            return False, ValidationError(
                code="MALFORMED_BUNDLE",
                note_id="",
                message=f"Provided bundle must be an EvidenceBundle, got {type(bundle).__name__}.",
                quote_preview="",
            )

        if excerpt is None:
            return False, ValidationError(
                code="MALFORMED_EXCERPT",
                note_id="",
                message="Excerpt is None.",
                quote_preview="",
            )

        if not isinstance(excerpt, EvidenceExcerpt):
            note_id = getattr(excerpt, "note_id", "")
            if not isinstance(note_id, str):
                note_id = ""
            return False, ValidationError(
                code="MALFORMED_EXCERPT",
                note_id=note_id,
                message=f"Excerpt must be an EvidenceExcerpt instance, got {type(excerpt).__name__}.",
                quote_preview="",
            )

        # Field type validation
        if (
            not isinstance(excerpt.note_id, str)
            or not isinstance(excerpt.source_file_sha256, str)
            or not isinstance(excerpt.verbatim_quote, str)
        ):
            note_id_str = excerpt.note_id if isinstance(excerpt.note_id, str) else ""
            quote_str = excerpt.verbatim_quote if isinstance(excerpt.verbatim_quote, str) else ""
            return False, ValidationError(
                code="MALFORMED_EXCERPT",
                note_id=note_id_str,
                message="EvidenceExcerpt fields (note_id, source_file_sha256, verbatim_quote) must all be str.",
                quote_preview=quote_str[:50] if quote_str else "",
            )

        # Non-empty content validation
        if not excerpt.note_id.strip():
            return False, ValidationError(
                code="MALFORMED_EXCERPT",
                note_id="",
                message="EvidenceExcerpt note_id cannot be empty or pure whitespace.",
                quote_preview=excerpt.verbatim_quote[:50],
            )

        if not excerpt.source_file_sha256.strip():
            return False, ValidationError(
                code="MALFORMED_EXCERPT",
                note_id=excerpt.note_id,
                message="EvidenceExcerpt source_file_sha256 cannot be empty or pure whitespace.",
                quote_preview=excerpt.verbatim_quote[:50],
            )

        if not excerpt.verbatim_quote or not excerpt.verbatim_quote.strip():
            return False, ValidationError(
                code="MALFORMED_EXCERPT",
                note_id=excerpt.note_id,
                message="EvidenceExcerpt verbatim_quote cannot be empty or pure whitespace.",
                quote_preview=excerpt.verbatim_quote[:50],
            )

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
        self, excerpts: list[EvidenceExcerpt] | Any, bundle: EvidenceBundle
    ) -> ValidationResult:
        """Validates a list of excerpts deterministically against an EvidenceBundle.

        Fail-closed guarantees:
        - None or non-iterable input -> FAILED with EMPTY_EVIDENCE or MALFORMED_EXCERPT.
        - Empty list -> FAILED with EMPTY_EVIDENCE (zero evidence cannot pass).
        - Malformed/None items -> Omitted with MALFORMED_EXCERPT audit error without crashing.
        - Preserves ordering and returns complete error diagnostics.
        """
        if excerpts is None:
            return ValidationResult(
                status="FAILED",
                validated_excerpts=[],
                omitted_excerpts=[],
                validated_count=0,
                failed_count=0,
                errors=[
                    ValidationError(
                        code="EMPTY_EVIDENCE",
                        note_id="",
                        message="No evidence excerpts provided for validation (input is None).",
                        quote_preview="",
                    )
                ],
            )

        try:
            excerpt_list = list(excerpts)
        except TypeError:
            return ValidationResult(
                status="FAILED",
                validated_excerpts=[],
                omitted_excerpts=[],
                validated_count=0,
                failed_count=0,
                errors=[
                    ValidationError(
                        code="MALFORMED_EXCERPT",
                        note_id="",
                        message=f"Excerpts collection must be iterable, got {type(excerpts).__name__}.",
                        quote_preview="",
                    )
                ],
            )

        if len(excerpt_list) == 0:
            return ValidationResult(
                status="FAILED",
                validated_excerpts=[],
                omitted_excerpts=[],
                validated_count=0,
                failed_count=0,
                errors=[
                    ValidationError(
                        code="EMPTY_EVIDENCE",
                        note_id="",
                        message="Evidence excerpt list is empty; zero evidence cannot be validated as PASS.",
                        quote_preview="",
                    )
                ],
            )

        validated: list[EvidenceExcerpt] = []
        omitted: list[Any] = []
        errors: list[ValidationError] = []

        for excerpt in excerpt_list:
            try:
                ok, err = self.validate_excerpt(excerpt, bundle)
            except Exception as exc:  # Defend against any unexpected exception
                ok = False
                note_id = getattr(excerpt, "note_id", "")
                if not isinstance(note_id, str):
                    note_id = ""
                err = ValidationError(
                    code="MALFORMED_EXCERPT",
                    note_id=note_id,
                    message=f"Unexpected error validating excerpt: {type(exc).__name__}: {exc}",
                    quote_preview="",
                )

            if ok and isinstance(excerpt, EvidenceExcerpt):
                validated.append(excerpt)
            else:
                omitted.append(excerpt)
                if err is not None:
                    errors.append(err)

        status = "PASS" if (len(errors) == 0 and len(validated) > 0) else "FAILED"

        return ValidationResult(
            status=status,
            validated_excerpts=validated,
            omitted_excerpts=omitted,
            validated_count=len(validated),
            failed_count=len(omitted),
            errors=errors,
        )
