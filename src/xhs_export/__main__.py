"""CLI entry point for xhs_export: python -m xhs_export."""

import argparse
import sys
from pathlib import Path

from .exporter import DEFAULT_DATA_DIR, DEFAULT_STATE_DB, DEFAULT_VAULT_DIR, VaultExporter


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="xhs-export",
        description="Obsidian Vault Exporter for Xiaohongshu Acquired Knowledge Assets",
    )
    parser.add_argument(
        "--state-db",
        default=str(DEFAULT_STATE_DB),
        help=f"Path to SQLite state database (default: {DEFAULT_STATE_DB})",
    )
    parser.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA_DIR),
        help=f"Path to P1 data directory (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--vault-dir",
        default=str(DEFAULT_VAULT_DIR),
        help=f"Path to destination Obsidian Vault directory (default: {DEFAULT_VAULT_DIR})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of notes to export",
    )

    args = parser.parse_args()

    try:
        exporter = VaultExporter(
            state_db=Path(args.state_db),
            data_dir=Path(args.data_dir),
            vault_dir=Path(args.vault_dir),
        )
        result = exporter.export(limit=args.limit)
    except Exception as exc:
        print(f"[ERROR] Export failed: {exc}", file=sys.stderr)
        return 2

    print("\n=== Obsidian Vault Export Summary ===")
    print(f"  Target Vault:         {args.vault_dir}")
    print(f"  Tracked in DB:        {result.source_snapshot.get('tracked_count', 0)}")
    print(f"  Complete Candidates:  {result.source_snapshot.get('complete_candidates', 0)}")
    print(f"  Evaluated Candidates: {result.source_snapshot.get('evaluated_candidates', 0)}")
    print(f"  Exported:             {result.exported_count}")
    print(f"  Skipped:              {result.skipped_count}")
    print(f"  Failed:               {result.failed_count}")
    print(f"  Stale Cleaned:        {result.stale_cleaned_count}")
    print(f"  Remote Coverage:      {result.remote_coverage_proof}")

    if result.skipped_notes:
        print("\nSkipped Notes:")
        for s in result.skipped_notes[:10]:
            print(f"  - {s['note_id']}: {s['reason']}")
        if len(result.skipped_notes) > 10:
            print(f"  ... and {len(result.skipped_notes) - 10} more")

    if result.failed_notes:
        print("\nFailed Notes:")
        for f in result.failed_notes:
            print(f"  - {f['note_id']}: {f['error']}")

    return 0 if result.failed_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
