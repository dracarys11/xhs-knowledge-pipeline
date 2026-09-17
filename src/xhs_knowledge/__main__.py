"""CLI entry point for xhs_knowledge: python -m xhs_knowledge."""

from .collection_indexer import main

if __name__ == "__main__":
    import sys
    sys.exit(main())
