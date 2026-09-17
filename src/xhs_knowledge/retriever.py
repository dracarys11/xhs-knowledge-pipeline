"""VaultRetriever: Deterministic, offline retrieval of Vault notes for Phase C Knowledge Synthesis.

Ref: docs/PHASE_C_DIGEST_CONTRACT_V1.md
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xhs_knowledge.contracts import (
    BoundaryViolationError,
    CollectionMembership,
    DigestRequest,
    EmptySelectionError,
    EvidenceBundle,
    InvalidNoteContentError,
    SelectedNote,
    SelectionPositionMissingError,
)

logger = logging.getLogger(__name__)

DEFAULT_VAULT_DIR = Path("Vault")
DEFAULT_DATA_DIR = Path("data")
DEFAULT_STATE_DIR = Path(".xhs-state")


def sanitize_filename(name: str) -> str:
    """Converts a collection name into a safe filesystem filename (without extension)."""
    cleaned = re.sub(r'[\/\\:*?"<>|]', "_", name).strip()
    return cleaned or "collection"


def validate_retriever_boundary(
    vault_dir: Path,
    data_dir: Path | None = None,
    state_dir: Path | None = None,
) -> None:
    """Ensures vault_dir is isolated and does not cross boundaries into data_dir or state_dir."""
    resolved_vault = vault_dir.resolve()

    if data_dir is not None:
        resolved_data = data_dir.resolve()
        if resolved_vault == resolved_data or resolved_data in resolved_vault.parents:
            raise BoundaryViolationError(
                f"Vault directory ({resolved_vault}) cannot be inside or equal to data directory ({resolved_data})"
            )
        if resolved_vault in resolved_data.parents:
            raise BoundaryViolationError(
                f"Data directory ({resolved_data}) cannot be inside vault directory ({resolved_vault})"
            )

    if state_dir is not None:
        resolved_state = state_dir.resolve()
        if resolved_vault == resolved_state or resolved_state in resolved_vault.parents:
            raise BoundaryViolationError(
                f"Vault directory ({resolved_vault}) cannot be inside or equal to state directory ({resolved_state})"
            )
        if resolved_vault in resolved_state.parents:
            raise BoundaryViolationError(
                f"State directory ({resolved_state}) cannot be inside vault directory ({resolved_vault})"
            )


def safe_validate_vault_path(path: Path, vault_root: Path, *, label: str = "path") -> Path:
    """Pre-read physical boundary validator.

    Must be called BEFORE any open, read, or metadata access.
    Strictly forbids symlinks and paths escaping vault_root.
    """
    # 1. Symlink check using os.path.islink (inspects link itself without dereferencing)
    if os.path.islink(path):
        raise BoundaryViolationError(
            f"Boundary violation: symlinks are strictly forbidden inside Vault ({path})"
        )

    # 2. Escape check
    resolved_vault = vault_root.resolve()
    resolved_path = path.resolve()

    try:
        resolved_path.relative_to(resolved_vault)
    except ValueError:
        raise BoundaryViolationError(
            f"Boundary violation: {label} ({resolved_path}) escapes Vault root ({resolved_vault})"
        )

    return path


def safe_iter_markdown_files(directory: Path, vault_root: Path) -> list[Path]:
    """Safely iterates markdown files inside a directory without following or reading unvalidated symlinks."""
    safe_validate_vault_path(directory, vault_root, label="directory")
    if not directory.exists() or not directory.is_dir():
        return []

    valid_files: list[Path] = []
    with os.scandir(directory) as it:
        entries = sorted(list(it), key=lambda e: e.name)
        for entry in entries:
            p = Path(entry.path)
            # Check symlink BEFORE is_file / reading
            if entry.is_symlink() or os.path.islink(p):
                raise BoundaryViolationError(
                    f"Boundary violation: symlink detected inside Vault directory: {p}"
                )
            if entry.is_file() and p.name.endswith(".md"):
                safe_validate_vault_path(p, vault_root, label="markdown file")
                valid_files.append(p)

    return valid_files


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Pure-Python YAML frontmatter parser for Vault markdown files."""
    if not text.startswith("---"):
        return {}, text

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text

    fm_raw = parts[1]
    body = parts[2]

    meta: dict[str, Any] = {}
    current_list_key: str | None = None

    for line in fm_raw.splitlines():
        line_str = line.rstrip()
        if not line_str or line_str.startswith("#"):
            continue

        list_match = re.match(r"^\s+-\s+(.*)$", line_str)
        if list_match and current_list_key:
            val = list_match.group(1).strip()
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                try:
                    val = json.loads(val)
                except Exception:
                    val = val[1:-1]
            meta[current_list_key].append(val)
            continue

        kv_match = re.match(r"^([a-zA-Z0-9_-]+):\s*(.*)$", line_str)
        if kv_match:
            k = kv_match.group(1)
            v = kv_match.group(2).strip()
            if not v:
                meta[k] = []
                current_list_key = k
            else:
                current_list_key = None
                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                    try:
                        v = json.loads(v)
                    except Exception:
                        v = v[1:-1]
                elif v.isdigit():
                    v = int(v)
                meta[k] = v

    return meta, body


def extract_note_content_text(body: str) -> str:
    """Extracts raw textual body under ## Content, excluding header and media."""
    match = re.search(r"##\s+Content\s*\n(.*?)(?=\n##|\Z)", body, re.DOTALL)
    if match:
        text = match.group(1).strip()
    else:
        # Fallback: strip leading markdown headers and metadata lines
        text = re.sub(r"^#+.*$", "", body, flags=re.MULTILINE).strip()

    if text == "*(无文字内容)*":
        return ""
    return text


class VaultRetriever:
    """Deterministic, read-only retriever operating exclusively on Vault artifacts."""

    def __init__(
        self,
        vault_dir: Path | str = DEFAULT_VAULT_DIR,
        data_dir: Path | str | None = DEFAULT_DATA_DIR,
        state_dir: Path | str | None = DEFAULT_STATE_DIR,
        *,
        allow_unisolated_vault: bool = False,
    ) -> None:
        self.vault_dir = Path(vault_dir)
        self.data_dir = Path(data_dir) if data_dir else None
        self.state_dir = Path(state_dir) if state_dir else None
        self.allow_unisolated_vault = allow_unisolated_vault

        if not self.allow_unisolated_vault:
            if self.data_dir is None or self.state_dir is None:
                raise BoundaryViolationError(
                    "data_dir and state_dir must be configured for boundary isolation. "
                    "Set allow_unisolated_vault=True explicitly if running in a hermetic test."
                )

        validate_retriever_boundary(self.vault_dir, self.data_dir, self.state_dir)

        self.notes_dir = self.vault_dir / "notes"
        self.collections_dir = self.vault_dir / "collections"

    def parse_note_file(self, note_path: Path) -> dict[str, Any]:
        """Parses a note markdown file in Vault/notes/ into constituent fields."""
        safe_validate_vault_path(note_path, self.vault_dir, label="note file")
        if not note_path.is_file():
            raise InvalidNoteContentError(f"Note file does not exist or is not a file: {note_path}")

        try:
            raw_bytes = note_path.read_bytes()
            raw_text = raw_bytes.decode("utf-8")
        except Exception as exc:
            raise InvalidNoteContentError(f"Failed to read note file {note_path}: {exc}") from exc

        meta, body = parse_frontmatter(raw_text)
        note_id = meta.get("note_id") or note_path.stem
        title = meta.get("title") or ""
        author_name = meta.get("author_name") or ""
        content_text = extract_note_content_text(body)
        file_sha256 = hashlib.sha256(raw_bytes).hexdigest()

        rel_path = str(note_path.relative_to(self.vault_dir))

        return {
            "note_id": note_id,
            "title": title,
            "author_name": author_name,
            "content_text": content_text,
            "file_path": rel_path,
            "file_sha256": file_sha256,
        }

    def parse_collection_file(
        self, collection_path: Path
    ) -> tuple[str, str, list[dict[str, Any]]]:
        """Parses a collection markdown file in Vault/collections/.

        Returns:
            (collection_id, collection_name, list_of_member_entries)
            where each entry is:
            {"note_id": str, "position": int, "title_alias": str, "author_alias": str, "is_pending": bool}
        """
        safe_validate_vault_path(collection_path, self.vault_dir, label="collection file")
        if not collection_path.is_file():
            raise SelectionPositionMissingError(f"Collection file not found: {collection_path}")

        raw_text = collection_path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(raw_text)
        cid = meta.get("collection_id") or collection_path.stem
        cname = meta.get("name") or collection_path.stem

        members: list[dict[str, Any]] = []
        in_notes_section = False
        pos = 1

        for line in raw_text.splitlines():
            line_stripped = line.strip()
            if line_stripped == "## 收藏笔记":
                in_notes_section = True
                continue
            if in_notes_section:
                if line_stripped.startswith("## "):
                    break
                m = re.match(
                    r"^\s*-\s+\[\[([^\|\]]+)(?:\|([^\]]*))?\]\](?:\s+\*\((待导出)\)\*)?(?:\s+—\s+\*([^*]+)\*)?",
                    line,
                )
                if m:
                    nid = m.group(1).strip()
                    title_alias = (m.group(2) or "").strip()
                    is_pending = bool(m.group(3))
                    author_alias = (m.group(4) or "").strip()

                    members.append(
                        {
                            "note_id": nid,
                            "position": pos,
                            "title_alias": title_alias,
                            "author_alias": author_alias,
                            "is_pending": is_pending,
                        }
                    )
                    pos += 1

        if not in_notes_section and not members:
            raise SelectionPositionMissingError(
                f"Collection file {collection_path.name} missing '## 收藏笔记' section with positions"
            )

        return cid, cname, members

    def _resolve_collection_paths(self, collection_names: list[str]) -> list[tuple[str, Path]]:
        """Resolves collection names to paths deterministically, validating paths BEFORE reading."""
        if not self.collections_dir.exists():
            return []

        # Safe iteration: checks symlinks before reading any content
        existing_files = safe_iter_markdown_files(self.collections_dir, self.vault_dir)

        # Build lookup table: name -> path and filename_stem -> path
        name_to_path: dict[str, Path] = {}
        for cp in existing_files:
            name_to_path[cp.stem] = cp
            try:
                meta, _ = parse_frontmatter(cp.read_text(encoding="utf-8"))
                if "name" in meta:
                    name_to_path[meta["name"]] = cp
            except Exception:
                continue

        resolved: list[tuple[str, Path]] = []
        if collection_names:
            for name in collection_names:
                if name in name_to_path:
                    resolved.append((name, name_to_path[name]))
                else:
                    safe_name = sanitize_filename(name)
                    if safe_name in name_to_path:
                        resolved.append((name, name_to_path[safe_name]))
                    else:
                        logger.warning("Requested collection '%s' not found in Vault/collections/", name)
        else:
            # All collections, sorted alphabetically by collection name
            for name in sorted(name_to_path.keys()):
                p = name_to_path[name]
                if (name, p) not in resolved:
                    resolved.append((name, p))

        return resolved

    def retrieve(self, request: DigestRequest) -> list[SelectedNote]:
        """Retrieves and selects notes deterministically according to DigestRequest."""
        target_collections = self._resolve_collection_paths(request.source.collections)
        if not target_collections:
            raise EmptySelectionError(
                f"No collections found matching request source {request.source.collections}"
            )

        # Build collection priority index
        collection_priority_index = {
            coll_name: idx for idx, (coll_name, _) in enumerate(target_collections)
        }

        # Map note_id -> SelectedNote
        selected_map: dict[str, SelectedNote] = {}

        for coll_name, coll_path in target_collections:
            cid, real_cname, members = self.parse_collection_file(coll_path)
            for item in members:
                note_id = item["note_id"]
                pos = item["position"]
                is_pending = item["is_pending"]

                if is_pending:
                    continue

                membership = CollectionMembership(
                    collection_id=cid,
                    collection_name=real_cname,
                    vault_collection_position=pos,
                )

                if note_id in selected_map:
                    # Multi-collection provenance: record membership without overwriting primary provenance
                    existing = selected_map[note_id]
                    if not any(m.collection_id == cid for m in existing.memberships):
                        existing.memberships.append(membership)
                    continue

                note_path = self.notes_dir / f"{note_id}.md"
                if not note_path.exists():
                    logger.debug(
                        "Note %s referenced in collection %s but not present in Vault/notes/",
                        note_id,
                        real_cname,
                    )
                    continue

                # Pre-read boundary check before parse
                safe_validate_vault_path(note_path, self.vault_dir, label="note file")

                try:
                    note_info = self.parse_note_file(note_path)
                except Exception as exc:
                    logger.warning("Failed parsing note %s: %s", note_id, exc)
                    continue

                content_text = note_info["content_text"]
                # Enforce NOTE_CONTENT_EMPTY
                if not content_text:
                    logger.warning("Excluding note %s from selection: NOTE_CONTENT_EMPTY", note_id)
                    continue

                # Title resolution
                resolved_title = note_info["title"]
                if not resolved_title or resolved_title == "无标题笔记":
                    if item["title_alias"]:
                        resolved_title = item["title_alias"]
                    else:
                        resolved_title = resolved_title or f"笔记 {note_id}"

                # Author resolution
                resolved_author = note_info["author_name"] or item["author_alias"]

                selected_map[note_id] = SelectedNote(
                    note_id=note_id,
                    title=resolved_title,
                    author_name=resolved_author,
                    primary_collection=real_cname,
                    vault_collection_position=pos,
                    memberships=[membership],
                    content_text=content_text,
                    file_path=note_info["file_path"],
                    file_sha256=note_info["file_sha256"],
                )

        if not selected_map:
            raise EmptySelectionError(
                f"0 notes matched selection criteria for digest '{request.digest_name}'"
            )

        # Deterministic multi-collection sorting:
        # 1. Collection priority index (preserves request.source.collections priority)
        # 2. vault_collection_position ASC
        # 3. note_id ASC
        notes_list = sorted(
            selected_map.values(),
            key=lambda n: (
                collection_priority_index.get(n.primary_collection, 999999),
                n.vault_collection_position,
                n.note_id,
            ),
        )

        # Enforce max_notes ceiling
        selected_notes = notes_list[: request.selection.max_notes]
        return selected_notes

    def assemble_bundle(
        self,
        request: DigestRequest,
        notes: list[SelectedNote],
        created_at: str | None = None,
    ) -> EvidenceBundle:
        """Assembles a sealed, reproducible EvidenceBundle from retrieved notes."""
        if created_at is None:
            created_at = datetime.now(timezone.utc).isoformat()

        bundle_id = f"bundle_{request.target_date.replace('-', '')}_{request.digest_name}"
        request_fingerprint = request.compute_fingerprint()

        bundle = EvidenceBundle(
            bundle_id=bundle_id,
            request_fingerprint=request_fingerprint,
            created_at=created_at,
            total_notes=len(notes),
            notes=notes,
        )
        return bundle
