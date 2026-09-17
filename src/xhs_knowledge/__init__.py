"""P2.2b Knowledge Projection Layer & Phase C Knowledge Synthesis MVP."""

from .collection_indexer import CollectionIndexer, IndexResult, sanitize_url
from .contracts import (
    BoundaryViolationError,
    CollectionMembership,
    DigestError,
    DigestRequest,
    EmptyEvidenceError,
    EmptySelectionError,
    EvidenceBundle,
    EvidenceExcerpt,
    EvidenceNoteNotInBundleError,
    EvidenceReference,
    InvalidNoteContentError,
    QuoteSubstringMismatchError,
    SelectedNote,
    SelectionConfig,
    SelectionPositionMissingError,
    SourceConfig,
    SourceHashMismatchError,
    SynthesizerConfig,
)
from .future_contracts import (
    ClaimType,
    ClaimTypeInvalidError,
    CrossNoteSynthesisError,
    DigestClaim,
    HumanReviewState,
)
from .retriever import VaultRetriever

__all__ = [
    "CollectionIndexer",
    "IndexResult",
    "sanitize_url",
    "VaultRetriever",
    "DigestRequest",
    "SourceConfig",
    "SelectionConfig",
    "SynthesizerConfig",
    "SelectedNote",
    "CollectionMembership",
    "EvidenceBundle",
    "EvidenceExcerpt",
    "EvidenceReference",
    "DigestError",
    "BoundaryViolationError",
    "EmptySelectionError",
    "SelectionPositionMissingError",
    "InvalidNoteContentError",
    "EmptyEvidenceError",
    "EvidenceNoteNotInBundleError",
    "SourceHashMismatchError",
    "QuoteSubstringMismatchError",
    # Future contract exports
    "DigestClaim",
    "ClaimType",
    "HumanReviewState",
    "CrossNoteSynthesisError",
    "ClaimTypeInvalidError",
]
