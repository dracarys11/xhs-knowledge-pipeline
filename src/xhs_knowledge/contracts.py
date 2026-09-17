"""Phase C Contracts: Schemas, Data Models, and Exceptions for Offline Knowledge Synthesis.

Ref: docs/PHASE_C_DIGEST_CONTRACT_V1.md
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class DigestError(Exception):
    """Base exception for all Phase C digest pipeline failures."""
    pass


class BoundaryViolationError(DigestError):
    """Raised when an operation attempts to access or write outside physical boundary."""
    pass


class EmptySelectionError(DigestError):
    """Raised when 0 notes matched the request selection criteria."""
    pass


class SelectionPositionMissingError(DigestError):
    """Raised when a selected collection member has no stable observed position."""
    pass


class InvalidNoteContentError(DigestError):
    """Raised when a selected note markdown file is corrupted or unreadable."""
    pass


class EvidenceInvalidNoteIdError(DigestError):
    """Raised when a claim cites a note_id not present in the input bundle."""
    pass


class EvidenceUnverifiableQuoteError(DigestError):
    """Raised when a cited quote does not appear verbatim in the note content."""
    pass


class ClaimTypeInvalidError(DigestError):
    """Raised when a claim type is invalid or missing."""
    pass


class ClaimType(str, Enum):
    FACT = "FACT"
    OPINION = "OPINION"
    RECOMMENDATION = "RECOMMENDATION"
    SUMMARY = "SUMMARY"


@dataclass
class SourceConfig:
    collections: list[str] = field(default_factory=list)


@dataclass
class SelectionConfig:
    max_notes: int = 10
    order_by: list[str] = field(default_factory=lambda: ["collection_position ASC", "note_id ASC"])


@dataclass
class SynthesizerConfig:
    type: str = "extractor"
    model_tag: str = "deterministic_extractor_v1"


@dataclass
class DigestRequest:
    digest_name: str
    target_date: str
    source: SourceConfig = field(default_factory=SourceConfig)
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    synthesizer: SynthesizerConfig = field(default_factory=SynthesizerConfig)
    strict_provenance: bool = True

    def __post_init__(self) -> None:
        if not self.digest_name or not self.digest_name.strip():
            raise ValueError("digest_name must be a non-empty string")
        if not self.target_date or not self.target_date.strip():
            raise ValueError("target_date must be a non-empty string")
        if self.selection.max_notes <= 0:
            raise ValueError("selection.max_notes must be positive")
        if self.selection.max_notes > 50:
            raise ValueError("selection.max_notes cannot exceed hard ceiling of 50")

        # Validate order_by for v0.1: ["collection_position ASC", "note_id ASC"]
        allowed_orders = [["collection_position ASC", "note_id ASC"], ["collection_position ASC"]]
        if self.selection.order_by not in allowed_orders:
            raise ValueError(
                f"Unsupported order_by in v0.1: {self.selection.order_by}. "
                "Only ['collection_position ASC', 'note_id ASC'] is permitted."
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def compute_fingerprint(self) -> str:
        """Computes a deterministic SHA256 fingerprint of the request configuration."""
        canonical_json = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


@dataclass
class SelectedNote:
    note_id: str
    title: str
    author_name: str
    collections: list[str]
    content_text: str
    file_path: str
    file_sha256: str
    collection_position: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceBundle:
    bundle_id: str
    request_fingerprint: str
    created_at: str
    total_notes: int
    notes: list[SelectedNote]
    bundle_content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.bundle_content_hash and self.notes:
            self.bundle_content_hash = self.compute_content_hash()

    def compute_content_hash(self) -> str:
        """Computes a deterministic content hash over all selected notes.

        Independent of created_at or runtime metadata.
        """
        h = hashlib.sha256()
        for n in self.notes:
            line = f"{n.note_id}:{n.file_sha256}:{n.collection_position}\n"
            h.update(line.encode("utf-8"))
        return h.hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceReference:
    note_id: str
    verbatim_quote: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DigestClaim:
    claim_id: str
    claim_type: ClaimType
    topic: str
    summary: str
    evidence: list[EvidenceReference]

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "claim_type": self.claim_type.value if isinstance(self.claim_type, ClaimType) else str(self.claim_type),
            "topic": self.topic,
            "summary": self.summary,
            "evidence": [e.to_dict() for e in self.evidence],
        }


@dataclass
class EvidenceExcerpt:
    note_id: str
    text: str
    location: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
