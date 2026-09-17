"""P2.2b Knowledge Projection Layer: Projects collection relations and notes into Obsidian Vault views."""

from .collection_indexer import CollectionIndexer, IndexResult, sanitize_url

__all__ = ["CollectionIndexer", "IndexResult", "sanitize_url"]
