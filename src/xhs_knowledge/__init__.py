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
from .extractor import EvidenceExtractor
from .retriever import VaultRetriever
from .validator import ProvenanceValidator, ValidationError, ValidationResult

__all__ = [
    "CollectionIndexer",
    "IndexResult",
    "sanitize_url",
    "VaultRetriever",
    "EvidenceExtractor",
    "ProvenanceValidator",
    "ValidationResult",
    "ValidationError",
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
]
