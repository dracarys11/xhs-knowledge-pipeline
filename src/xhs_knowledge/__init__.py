"""P2.2b Knowledge Projection Layer: Projects collection relations and notes into Obsidian Vault views."""

from .collection_indexer import CollectionIndexer, IndexResult, sanitize_url
from .contracts import (
    BoundaryViolationError,
    ClaimType,
    DigestClaim,
    DigestError,
    DigestRequest,
    EmptySelectionError,
    EvidenceBundle,
    EvidenceExcerpt,
    EvidenceReference,
    InvalidNoteContentError,
    SelectedNote,
    SelectionConfig,
    SelectionPositionMissingError,
    SourceConfig,
    SynthesizerConfig,
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
    "EvidenceBundle",
    "DigestClaim",
    "ClaimType",
    "EvidenceExcerpt",
    "EvidenceReference",
    "DigestError",
    "BoundaryViolationError",
    "EmptySelectionError",
    "SelectionPositionMissingError",
    "InvalidNoteContentError",
]
