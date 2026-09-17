"""P2.2b Collection Indexer: Projects collection relationship evidence into Obsidian Vault indexes."""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_VAULT_DIR = Path("Vault")
DEFAULT_EVIDENCE_DIR = Path("knowledge/evidence/collections")
DEFAULT_DATA_DIR = Path("data")


def get_current_iso_time() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_url(url: str, note_id: str = "") -> str:
    """Evidence Hygiene Gate: Strips tracking, security, and referral tokens from URLs.

    Enforces bare canonical form for Xiaohongshu explore URLs:
    https://www.xiaohongshu.com/explore/<note_id>
    """
    if not url and not note_id:
        return ""
    if not url and note_id:
        return f"https://www.xiaohongshu.com/explore/{note_id}"

    try:
        parts = urllib.parse.urlsplit(url)
        # Canonical explore URL format
        if note_id and ("/explore/" in parts.path or parts.netloc.endswith("xiaohongshu.com")):
            return f"https://www.xiaohongshu.com/explore/{note_id}"

        # Strip sensitive tracking and referral parameters
        banned_prefixes = ("utm_", "spm", "from_", "xsec_")
        banned_exact = {"token", "auth_token", "session_id", "ref", "source", "x_trace_id"}

        query_pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        clean_pairs = [
            (k, v)
            for k, v in query_pairs
            if not any(k.startswith(p) for p in banned_prefixes) and k not in banned_exact
        ]
        clean_query = urllib.parse.urlencode(clean_pairs)
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, clean_query, parts.fragment))
    except Exception:
        if note_id:
            return f"https://www.xiaohongshu.com/explore/{note_id}"
        return url


def is_safe_collection_name(name: str) -> bool:
    if not isinstance(name, str) or not name.strip():
        return False
    if "/" in name or "\\" in name or "\0" in name:
        return False
    if name in (".", ".."):
        return False
    return True


def sanitize_filename(name: str) -> str:
    """Converts a collection name into a safe filesystem filename (without extension)."""
    # Replace slashes, backslashes, colons, and forbidden filename characters
    cleaned = re.sub(r'[\/\\:*?"<>|]', "_", name).strip()
    return cleaned or "collection"


def validate_vault_boundary(vault_dir: Path, data_dir: Path, state_dir: Path | None = None) -> None:
    """Ensures vault_dir is isolated and does not collide with data_dir or state_dir."""
    resolved_vault = vault_dir.resolve()
    resolved_data = data_dir.resolve()

    if resolved_vault == resolved_data or resolved_data in resolved_vault.parents:
        raise ValueError(f"Vault directory ({resolved_vault}) cannot be inside or equal to data directory ({resolved_data})")

    if state_dir:
        resolved_state = state_dir.resolve()
        if resolved_vault == resolved_state or resolved_state in resolved_vault.parents:
            raise ValueError(f"Vault directory ({resolved_vault}) cannot be inside or equal to state directory ({resolved_state})")


@dataclass
class NoteRef:
    note_id: str
    title: str
    author_name: str = ""
    is_exported: bool = False
    note_type: str = "normal"


@dataclass
class IndexResult:
    indexed_at: str
    collections_indexed: int
    total_notes_referenced: int
    exported_notes_count: int
    missing_notes_count: int
    collection_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CollectionIndexer:
    """Projects collection relationships into Obsidian Markdown index documents."""

    def __init__(
        self,
        vault_dir: Path = DEFAULT_VAULT_DIR,
        evidence_dir: Path = DEFAULT_EVIDENCE_DIR,
        data_dir: Path = DEFAULT_DATA_DIR,
    ) -> None:
        self.vault_dir = Path(vault_dir)
        self.evidence_dir = Path(evidence_dir)
        self.data_dir = Path(data_dir)

        validate_vault_boundary(self.vault_dir, self.data_dir)
        self.collections_dir = self.vault_dir / "collections"
        self.notes_dir = self.vault_dir / "notes"

    def _load_note_info(self, note_id: str, evidence_note: dict[str, Any] | None = None) -> NoteRef:
        """Resolves note title, author, and export status."""
        title = ""
        author_name = ""
        note_type = "normal"

        if evidence_note:
            title = (evidence_note.get("title") or "").strip()
            note_type = evidence_note.get("type") or "normal"

        # Check canonical source in data_dir
        canonical_path = self.data_dir / note_id / "canonical.json"
        if canonical_path.exists():
            try:
                canonical_data = json.loads(canonical_path.read_text(encoding="utf-8"))
                content = canonical_data.get("content") or {}
                canonical_title = (content.get("title") or "").strip()
                if canonical_title:
                    title = canonical_title
                author = canonical_data.get("author") or {}
                author_name = author.get("name") or ""
                if "type" in canonical_data:
                    note_type = canonical_data["type"]
            except Exception as exc:
                logger.warning("Failed to parse canonical for note %s: %s", note_id, exc)

        if not title:
            title = f"笔记 {note_id}"

        # Check if exported into Vault/notes/<note_id>.md
        vault_note_path = self.notes_dir / f"{note_id}.md"
        is_exported = vault_note_path.exists()

        return NoteRef(
            note_id=note_id,
            title=title,
            author_name=author_name,
            is_exported=is_exported,
            note_type=note_type,
        )

    def render_collection_markdown(
        self,
        board_id: str,
        collection_name: str,
        notes: list[NoteRef],
        manifest: dict[str, Any],
    ) -> str:
        """Renders an Obsidian-compatible Markdown document for a collection."""
        reported_total = manifest.get("reported_total", len(notes))
        observed_total = len(notes)
        exported_count = sum(1 for n in notes if n.is_exported)
        proof = manifest.get("completion_proof", {}).get("type", "unknown")
        now_iso = get_current_iso_time()

        lines = [
            "---",
            f"collection_id: {json.dumps(board_id, ensure_ascii=False)}",
            f"name: {json.dumps(collection_name, ensure_ascii=False)}",
            f"total_notes: {observed_total}",
            f"reported_notes: {reported_total}",
            f"exported_notes: {exported_count}",
            f"indexed_at: {json.dumps(now_iso, ensure_ascii=False)}",
            "---",
            "",
            f"# {collection_name}",
            "",
            f"- **Collection ID**: `{board_id}`",
            f"- **Observed Notes**: {observed_total} (Reported: {reported_total})",
            f"- **Vault Export Status**: {exported_count}/{observed_total} 篇已同步",
            f"- **Completion Proof**: `{proof}`",
            "",
            "## 收藏笔记",
            "",
        ]

        for note in notes:
            # Clean title to avoid markdown breakages
            clean_title = note.title.replace("[", "\\[").replace("]", "\\]")
            # Standard Obsidian wikilink with alias: [[note_id|display_title]]
            link = f"[[{note.note_id}|{clean_title}]]"
            suffix = ""
            if not note.is_exported:
                suffix = " *(待导出)*"
            elif note.author_name:
                suffix = f" — *{note.author_name}*"
            lines.append(f"- {link}{suffix}")

        lines.append("")
        return "\n".join(lines)

    def index(self) -> IndexResult:
        """Indexes all discovered collections under evidence_dir into Vault/collections/."""
        if not self.evidence_dir.exists():
            raise FileNotFoundError(f"Evidence collections directory does not exist: {self.evidence_dir}")

        self.collections_dir.mkdir(parents=True, exist_ok=True)

        collection_subdirs = sorted([d for d in self.evidence_dir.iterdir() if d.is_dir()])
        indexed_count = 0
        total_notes = 0
        total_exported = 0
        total_missing = 0
        created_files = []
        indexed_collection_names = []

        for cdir in collection_subdirs:
            manifest_file = cdir / "manifest.json"
            if not manifest_file.exists():
                logger.warning("Skipping directory without manifest: %s", cdir)
                continue

            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.error("Failed to parse manifest in %s: %s", cdir, exc)
                continue

            board_id = manifest.get("board_id") or cdir.name
            collection_name = manifest.get("name") or board_id

            # Collect notes from all page records
            page_files = sorted(cdir.glob("page_*.json"))
            evidence_notes: dict[str, dict[str, Any]] = {}
            ordered_note_ids: list[str] = []

            for pfile in page_files:
                try:
                    pdata = json.loads(pfile.read_text(encoding="utf-8"))
                    for n in pdata.get("notes", []):
                        nid = n.get("note_id")
                        if nid and nid not in evidence_notes:
                            ordered_note_ids.append(nid)
                            evidence_notes[nid] = n
                except Exception as exc:
                    logger.warning("Failed to parse page file %s: %s", pfile, exc)

            # Build NoteRef list
            notes: list[NoteRef] = [
                self._load_note_info(nid, evidence_notes.get(nid))
                for nid in ordered_note_ids
            ]

            total_notes += len(notes)
            exported_in_collection = sum(1 for n in notes if n.is_exported)
            total_exported += exported_in_collection
            total_missing += (len(notes) - exported_in_collection)

            # Render markdown content
            md_content = self.render_collection_markdown(board_id, collection_name, notes, manifest)

            # Safe destination filename
            filename = f"{sanitize_filename(collection_name)}.md"
            target_file = self.collections_dir / filename

            # Write atomically
            tmp_file = target_file.with_name(f".{target_file.name}.tmp")
            tmp_file.write_text(md_content, encoding="utf-8")
            tmp_file.replace(target_file)

            created_files.append(str(target_file))
            indexed_collection_names.append(collection_name)
            indexed_count += 1
            logger.info("Indexed collection '%s' (%s) -> %s", collection_name, board_id, target_file)

        # Generate root entrypoint: Vault/README.md
        readme_path = self._generate_readme(indexed_collection_names)
        created_files.append(str(readme_path))

        return IndexResult(
            indexed_at=get_current_iso_time(),
            collections_indexed=indexed_count,
            total_notes_referenced=total_notes,
            exported_notes_count=total_exported,
            missing_notes_count=total_missing,
            collection_files=created_files,
        )

    def _generate_readme(self, collection_names: list[str]) -> Path:
        """Generates Vault/README.md providing a clean navigation entrypoint."""
        total_vault_notes = len(list(self.notes_dir.glob("*.md"))) if self.notes_dir.exists() else 0

        lines = [
            "# My Knowledge Base",
            "",
            "## Collections",
            "",
        ]
        for name in sorted(collection_names):
            clean_name = sanitize_filename(name)
            lines.append(f"- [[collections/{clean_name}|{name}]]")

        lines.extend([
            "",
            "## Stats",
            "",
            f"- Notes: {total_vault_notes}",
            f"- Collections: {len(collection_names)}",
            "",
        ])

        readme_path = self.vault_dir / "README.md"
        tmp_file = readme_path.with_name(f".{readme_path.name}.tmp")
        tmp_file.write_text("\n".join(lines), encoding="utf-8")
        tmp_file.replace(readme_path)
        return readme_path


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="xhs-collection-indexer",
        description="Obsidian Collection Indexer for Xiaohongshu Knowledge Collections",
    )
    parser.add_argument(
        "--vault-dir",
        default=str(DEFAULT_VAULT_DIR),
        help=f"Path to Obsidian Vault directory (default: {DEFAULT_VAULT_DIR})",
    )
    parser.add_argument(
        "--evidence-dir",
        default=str(DEFAULT_EVIDENCE_DIR),
        help=f"Path to collection evidence directory (default: {DEFAULT_EVIDENCE_DIR})",
    )
    parser.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA_DIR),
        help=f"Path to P1 data directory (default: {DEFAULT_DATA_DIR})",
    )

    args = parser.parse_args()

    try:
        indexer = CollectionIndexer(
            vault_dir=Path(args.vault_dir),
            evidence_dir=Path(args.evidence_dir),
            data_dir=Path(args.data_dir),
        )
        res = indexer.index()
    except Exception as exc:
        print(f"[ERROR] Indexing failed: {exc}", file=sys.stderr)
        return 2

    print("\n=== Obsidian Collection Indexer Summary ===")
    print(f"  Target Collections Dir:   {indexer.collections_dir}")
    print(f"  Collections Indexed:      {res.collections_indexed}")
    print(f"  Total Notes Referenced:   {res.total_notes_referenced}")
    print(f"  Notes in Vault:           {res.exported_notes_count}")
    print(f"  Notes Pending Export:     {res.missing_notes_count}")
    for f in res.collection_files:
        print(f"  - Output: {f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
