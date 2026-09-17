"""Future Contracts: Deferred Semantic and Claim-Level Schemas beyond the Phase C MVP.

Ref: docs/PENDING_MISSIONS.md and docs/PHASE_C_CLAIM_BOUNDARY_V2.md
These schemas are explicitly deferred from the v0.1 local digest MVP and do not authorize implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from xhs_knowledge.contracts import (
    DigestError,
    EmptyEvidenceError,
    EvidenceReference,
)


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
        # Enforce single-note boundary
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
