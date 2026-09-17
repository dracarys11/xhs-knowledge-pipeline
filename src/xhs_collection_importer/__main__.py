"""CLI entry point for xhs_collection_importer: python -m xhs_collection_importer."""

import argparse
import logging
import sys
from pathlib import Path

from .discovery import (
    DEFAULT_KNOWLEDGE_DIR,
    DEFAULT_PROFILE_DIR,
    AuthRequiredError,
    CollectionDiscovery,
    DiscoveryError,
    InconsistentStateError,
    SchemaMismatchError,
)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(
        prog="xhs-collection-importer",
        description="XHS Collection Relationship Discovery & Importer (P2.2)",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # Discovery subcommand
    discover_parser = subparsers.add_parser("discover", help="Discover user collection boards/albums")
    discover_parser.add_argument(
        "--profile-dir",
        default=str(DEFAULT_PROFILE_DIR),
        help=f"Path to browser profile directory (default: {DEFAULT_PROFILE_DIR})",
    )
    discover_parser.add_argument(
        "--knowledge-dir",
        default=str(DEFAULT_KNOWLEDGE_DIR),
        help=f"Path to knowledge collections directory (default: {DEFAULT_KNOWLEDGE_DIR})",
    )
    discover_parser.add_argument(
        "--headed",
        action="store_true",
        help="Run browser in headed mode (default: headless)",
    )

    args = parser.parse_args()

    if args.command in ("discover", None):
        # Default action is discovery
        profile_dir = Path(getattr(args, "profile_dir", DEFAULT_PROFILE_DIR))
        knowledge_dir = Path(getattr(args, "knowledge_dir", DEFAULT_KNOWLEDGE_DIR))
        headless = not getattr(args, "headed", False)

        try:
            discovery = CollectionDiscovery(
                profile_dir=profile_dir,
                knowledge_dir=knowledge_dir,
                headless=headless,
            )
            snapshot = discovery.discover()
        except AuthRequiredError as exc:
            print(f"[ERROR] Authentication required: {exc}", file=sys.stderr)
            return 2
        except (InconsistentStateError, SchemaMismatchError, DiscoveryError) as exc:
            print(f"[ERROR] Collection discovery failed: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"[FATAL] Unexpected error during collection discovery: {type(exc).__name__}", file=sys.stderr)
            return 3

        print("\n=== Collection Discovery Snapshot Summary ===")
        print(f"  Snapshot ID:        {snapshot.snapshot_id}")
        print(f"  Captured At:        {snapshot.captured_at}")
        print(f"  User ID:            {snapshot.source_user_id}")
        print(f"  Board List Status:   {snapshot.board_listing_status}")
        print(f"  Member Status:       {snapshot.member_relationship_status}")
        print(f"  Publication Status:  {snapshot.publication_status}")
        print(f"  Reported Total:     {snapshot.total_reported}")
        print(f"  Discovered Boards:  {snapshot.boards_count}")
        print(f"  Knowledge Dir:      {knowledge_dir}")

        if snapshot.collections:
            print("\nDiscovered Collections:")
            for b in snapshot.collections:
                priv = " (Private)" if b.privacy == 1 else ""
                print(f"  - [{b.collection_id}] {b.name}{priv} (Reported Notes: {b.reported_notes_count})")

        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
