"""Phase C Contracts: Schemas, Data Models, and Exceptions for Offline Knowledge Synthesis.

Ref: docs/PHASE_C_CLAIM_BOUNDARY_V2.md and docs/PHASE_C_DIGEST_CONTRACT_V1.md
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
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


class EmptyEvidenceError(DigestError):
    """Raised when a claim contains zero evidence references."""
    pass


class EvidenceNoteNotInBundleError(DigestError):
    """Raised when a claim cites a note_id not present in the active EvidenceBundle."""
    pass


class SourceHashMismatchError(DigestError):
    """Raised when the cited source file SHA256 does not match the bundle note's SHA256."""
    pass


class SpanOutOfBoundsError(DigestError):
    """Raised when evidence quote span offsets fall outside note content_text bounds."""
    pass


class QuoteSpanMismatchError(DigestError):
    """Raised when the verbatim_quote does not exactly match content_text[quote_start:quote_end]."""
    pass


class CrossNoteSynthesisError(DigestError):
    """Raised when a claim in v0.1 attempts to aggregate evidence from multiple different notes."""
    pass


class ClaimTypeInvalidError(DigestError):
    """Raised when a claim type is invalid or missing."""
    pass


class ClaimType(str, Enum):
    FACT = "FACT"  # Source-reported factual assertion; not externally verified
    OPINION = "OPINION"  # Source author's personal sentiment, taste, or assessment
    RECOMMENDATION = "RECOMMENDATION"  # Source author's suggested action, tool, or avoidance
    SUMMARY = "SUMMARY"  # Bounded aggregation of constituent excerpts within a single note


class HumanReviewState(str, Enum):
    DRAFT = "DRAFT"  # Default automated state; pending human review
    REVIEWED = "REVIEWED"  # Formally audited and approved by human reviewer
    PUBLISHED = "PUBLISHED"  # Released for downstream knowledge consumption


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

        # Strict YYYY-MM-DD format validation
        try:
            datetime.strptime(self.target_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(
                f"target_date must follow strict 'YYYY-MM-DD' format, got {self.target_date!r}"
            ) from exc

        if self.selection.max_notes <= 0:
            raise ValueError("selection.max_notes must be positive")
        if self.selection.max_notes > 50:
            raise ValueError("selection.max_notes cannot exceed hard ceiling of 50")

        # Validate order_by for v0.1: ["collection_position ASC", "note_id ASC"]
        allowed_orders = [
            ["collection_position ASC", "note_id ASC"],
            ["vault_collection_position ASC", "note_id ASC"],
            ["collection_position ASC"],
            ["vault_collection_position ASC"],
        ]
        if self.selection.order_by not in allowed_orders:
            raise ValueError(
                f"Unsupported order_by in v0.1: {self.selection.order_by}. "
                "Only ['vault_collection_position ASC', 'note_id ASC'] (or legacy alias) is permitted."
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def compute_fingerprint(self) -> str:
        """Computes a deterministic SHA256 fingerprint of the request configuration."""
        canonical_json = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


@dataclass
class CollectionMembership:
    """Provenance record for a note's presence inside a specific collection markdown projection.

    Note: vault_collection_position represents ordering inside the local Vault collection projection.
    It is not evidence of original platform ordering.
    """
    collection_id: str
    collection_name: str
    vault_collection_position: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SelectedNote:
    """Admitted source note with cryptographic integrity and collection provenance.

    Note: vault_collection_position represents ordering inside the local Vault collection projection.
    It is not evidence of original platform ordering.
    """
    note_id: str
    title: str
    author_name: str
    primary_collection: str
    vault_collection_position: int
    memberships: list[CollectionMembership]
    content_text: str
    file_path: str
    file_sha256: str

    @property
    def collections(self) -> list[str]:
        """Convenience accessor for collection names."""
        if self.memberships:
            return [m.collection_name for m in self.memberships]
        return [self.primary_collection] if self.primary_collection else []

    @property
    def collection_position(self) -> int:
        """Backward-compatible alias for vault_collection_position."""
        return self.vault_collection_position

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["collections"] = self.collections
        d["collection_position"] = self.vault_collection_position
        return d


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
            line = f"{n.note_id}:{n.file_sha256}:{n.primary_collection}:{n.vault_collection_position}\n"
            h.update(line.encode("utf-8"))
        return h.hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceReference:
    """Exact, verifiable character span binding to a specific source note."""
    note_id: str
    source_file_sha256: str
    quote_start: int
    quote_end: int
    verbatim_quote: str

    def __post_init__(self) -> None:
        if not self.note_id:
            raise ValueError("note_id cannot be empty")
        if not self.source_file_sha256:
            raise ValueError("source_file_sha256 cannot be empty")
        if self.quote_start < 0:
            raise ValueError(f"quote_start must be non-negative, got {self.quote_start}")
        if self.quote_end <= self.quote_start:
            raise ValueError(f"quote_end ({self.quote_end}) must be greater than quote_start ({self.quote_start})")
        if not self.verbatim_quote:
            raise ValueError("verbatim_quote cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceExcerpt:
    """Non-interpretive raw excerpt produced by automated EvidenceExtractor."""
    note_id: str
    source_file_sha256: str
    quote_start: int
    quote_end: int
    verbatim_quote: str
    location_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DigestClaim:
    """Interpretive synthesis assertion. Starts as DRAFT pending human review.

    In v0.1, cross-note synthesis is strictly prohibited. Every claim is bound to a single note_id.
    """
    claim_id: str
    note_id: str
    claim_type: ClaimType
    statement: str
    evidence: list[EvidenceReference]
    topic: str = ""
    review_state: HumanReviewState = HumanReviewState.DRAFT

    def __post_init__(self) -> None:
        if not self.claim_id:
            raise ValueError("claim_id cannot be empty")
        if not self.note_id:
            raise ValueError("note_id cannot be empty")
        if not self.statement or not self.statement.strip():
            raise ValueError("statement cannot be empty")
        if not self.evidence:
            raise EmptyEvidenceError(f"Claim {self.claim_id} has empty evidence list")
        # Enforce v0.1 single-note boundary
        for ref in self.evidence:
            if ref.note_id != self.note_id:
                raise CrossNoteSynthesisError(
                    f"Claim {self.claim_id} cites foreign note {ref.note_id}; cross-note synthesis is forbidden in v0.1."
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "note_id": self.note_id,
            "claim_type": self.claim_type.value if isinstance(self.claim_type, ClaimType) else str(self.claim_type),
            "statement": self.statement,
            "topic": self.topic,
            "review_state": self.review_state.value if isinstance(self.review_state, HumanReviewState) else str(self.review_state),
            "evidence": [e.to_dict() for e in self.evidence],
        }
