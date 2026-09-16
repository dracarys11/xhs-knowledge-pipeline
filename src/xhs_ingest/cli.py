"""Command-Line Interface (CLI) for Xiaohongshu acquisition system."""

import argparse
import json
import logging
import sys
from pathlib import Path

from .collector import DEFAULT_PROFILE_DIR, XhsPlaywrightCollector
from .errors import AcquisitionError, AcquisitionStatus
from .media import download_post_media
from .normalizer import normalize_note
from .renderer import render_post_markdown
from .state import DEFAULT_STATE_DB, StateStore
from .sync import run_sync


def cmd_login(args: argparse.Namespace) -> int:
    collector = XhsPlaywrightCollector(
        profile_dir=Path(args.profile_dir),
        headless=False,
    )
    success = collector.interactive_login(timeout_seconds=args.timeout)
    if success:
        print("[+] Login completed and session saved to profile directory.")
        return 0
    else:
        print("[-] Login was not completed within the timeout window.")
        return 1


def cmd_favorites(args: argparse.Namespace) -> int:
    collector = XhsPlaywrightCollector(
        profile_dir=Path(args.profile_dir),
        headless=not args.headed,
    )
    try:
        page_result = collector.list_favorites(limit=args.limit)
    except AcquisitionError as e:
        print(f"[ERROR] {e.status.value}: {e.message}", file=sys.stderr)
        if e.details:
            print(f"Details: {json.dumps(e.details, ensure_ascii=False, indent=2)}", file=sys.stderr)
        return 2

    if not page_result.items:
        print(f"[*] No favorites found. Completion proof: {page_result.completion_proof}")
        return 0

    print(f"\nFound {len(page_result.items)} favorite notes:")
    print("-" * 80)
    print(f"{'#':<3} | {'Note ID':<26} | {'Author':<16} | {'Title'}")
    print("-" * 80)
    for idx, item in enumerate(page_result.items, 1):
        title = (item.title or "").replace("\n", " ")[:36]
        author = (item.author_name or "Unknown")[:14]
        print(f"{idx:<3} | {item.note_id:<26} | {author:<16} | {title}")
    print("-" * 80)

    if args.ingest and page_result.items:
        first_ref = page_result.items[0]
        print(f"\n[*] Ingesting selected note: {first_ref.note_id} ...")
        return ingest_note(collector, first_ref, Path(args.output_dir))

    return 0


def ingest_note(
    collector: XhsPlaywrightCollector,
    ref_or_id: str,
    output_base_dir: Path,
) -> int:
    try:
        # Step 1: Fetch Note Detail
        raw_note = collector.fetch_note(ref_or_id)

        # Step 2: Normalize
        post = normalize_note(raw_note)

        # Target directory: data/<note_id>/
        note_dir = output_base_dir / post.note_id
        assets_dir = note_dir / "assets"
        note_dir.mkdir(parents=True, exist_ok=True)
        assets_dir.mkdir(parents=True, exist_ok=True)

        # Step 3: Download Media Assets (Fail closed!)
        print(f"[*] Downloading {len(post.media)} media asset(s)...")
        download_success = download_post_media(post, assets_dir, raise_on_failure=False)

        # Step 4: Write raw.json
        raw_path = note_dir / "raw.json"
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(post.raw, f, ensure_ascii=False, indent=2)

        # Step 5: Write canonical.json
        canonical_path = note_dir / "canonical.json"
        with open(canonical_path, "w", encoding="utf-8") as f:
            json.dump(post.to_dict(include_raw=False), f, ensure_ascii=False, indent=2)

        # Step 6: Render and write post.md
        post_md_path = note_dir / "post.md"
        markdown_content = render_post_markdown(post)
        with open(post_md_path, "w", encoding="utf-8") as f:
            f.write(markdown_content)

        if not download_success:
            print(
                f"[WARNING] Note details saved to {note_dir}, but one or more media assets failed to download. "
                "Failing closed: ingestion not marked complete.",
                file=sys.stderr,
            )
            return 3

        print(f"[+] Note {post.note_id} successfully ingested to: {note_dir}")
        print(f"    - raw:       {raw_path}")
        print(f"    - canonical: {canonical_path}")
        print(f"    - post.md:   {post_md_path}")
        print(f"    - assets:    {len(post.media)} downloaded")
        return 0

    except AcquisitionError as e:
        print(f"[ERROR] {e.status.value}: {e.message}", file=sys.stderr)
        if e.details:
            print(f"Details: {json.dumps(e.details, ensure_ascii=False, indent=2)}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"[ERROR] UNKNOWN_ACQUISITION_FAILURE: {e}", file=sys.stderr)
        return 4


def cmd_ingest(args: argparse.Namespace) -> int:
    collector = XhsPlaywrightCollector(
        profile_dir=Path(args.profile_dir),
        headless=not args.headed,
    )
    return ingest_note(collector, args.target, Path(args.output_dir))


def _parse_max_notes(value: str) -> int | None:
    """--max-notes type: an integer slice, or the explicit 'all' opt-in.

    Required argument: a full sync must be a deliberate choice, never the
    default behavior of forgetting the flag.
    """
    if value.lower() == "all":
        return None
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer or 'all', got {value!r}")
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def cmd_sync(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    collector = XhsPlaywrightCollector(
        profile_dir=Path(args.profile_dir),
        headless=not args.headed,
    )
    store = StateStore(Path(args.state_db))
    try:
        if args.retry_failed:
            reset = store.retry_failed()
            print(f"[*] Reset {reset} FINAL_FAILED note(s) to PENDING.")
        return run_sync(collector, store, Path(args.output_dir), max_notes=args.max_notes)
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="xhs-ingest",
        description="Xiaohongshu Personal Favorites Knowledge Acquisition CLI",
    )
    parser.add_argument(
        "--profile-dir",
        default=str(DEFAULT_PROFILE_DIR),
        help=f"Directory for persistent browser profile (default: {DEFAULT_PROFILE_DIR})",
    )
    parser.add_argument(
        "--output-dir",
        default="data",
        help="Base directory for ingested note outputs (default: data)",
    )
    parser.add_argument(
        "--state-db",
        default=str(DEFAULT_STATE_DB),
        help=f"SQLite state database path (default: {DEFAULT_STATE_DB})",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run browser in visible (headed) mode for debugging",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # login command
    login_parser = subparsers.add_parser("login", help="Interactive human QR code / SMS login")
    login_parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Login timeout in seconds (default: 180)",
    )

    # favorites command
    fav_parser = subparsers.add_parser("favorites", help="List personal favorite notes")
    fav_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of favorites to list (default: 20)",
    )
    fav_parser.add_argument(
        "--ingest",
        action="store_true",
        help="Immediately ingest the first listed favorite note",
    )

    # ingest command
    ingest_parser = subparsers.add_parser("ingest", help="Ingest a single note by ID or URL")
    ingest_parser.add_argument(
        "target",
        help="Note ID or full note URL to ingest",
    )

    # sync command
    sync_parser = subparsers.add_parser("sync", help="Incremental favorites sync")
    sync_parser.add_argument(
        "--max-notes",
        type=_parse_max_notes,
        required=True,
        metavar="N|all",
        help="Process at most N notes this run (slice, exits 3 when work remains); "
        "'all' runs a full sync and must be chosen explicitly",
    )
    sync_parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Reset FINAL_FAILED notes to PENDING (clears attempt budget) before syncing",
    )

    args = parser.parse_args()

    if args.command == "login":
        sys.exit(cmd_login(args))
    elif args.command == "favorites":
        sys.exit(cmd_favorites(args))
    elif args.command == "ingest":
        sys.exit(cmd_ingest(args))
    elif args.command == "sync":
        sys.exit(cmd_sync(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
