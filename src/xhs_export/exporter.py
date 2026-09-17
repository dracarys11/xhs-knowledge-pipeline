"""P2.1 Exporter: Downstream read-only projection of COMPLETE notes into an Obsidian Vault."""

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_STATE_DB = Path(".xhs-state/sync.db")
DEFAULT_DATA_DIR = Path("data")
DEFAULT_VAULT_DIR = Path("Vault")
OWNERSHIP_MANIFEST_NAME = ".exporter-manifest.json"


def get_current_iso_time() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_safe_filename(name: Any) -> bool:
    """Returns True only if name is a non-empty string representing a single, safe filename."""
    if not isinstance(name, str) or not name.strip():
        return False
    if "/" in name or "\\" in name or "\0" in name:
        return False
    if name in (".", ".."):
        return False
    if Path(name).name != name:
        return False
    return True


def validate_output_boundary(vault_dir: Path, data_dir: Path, state_db: Path) -> None:
    """Guarantees that vault_dir does not overwrite or intersect data_dir or state_db directory."""
    resolved_vault = vault_dir.resolve()
    resolved_data = data_dir.resolve()
    resolved_db = state_db.resolve()
    resolved_db_dir = resolved_db.parent

    # 1. vault_dir cannot be data_dir, or inside data_dir
    if resolved_vault == resolved_data or resolved_data in resolved_vault.parents:
        raise ValueError(
            f"Output vault_dir ({resolved_vault}) cannot be inside or equal to data_dir ({resolved_data})"
        )

    # 2. data_dir cannot be inside vault_dir
    if resolved_vault in resolved_data.parents:
        raise ValueError(
            f"data_dir ({resolved_data}) cannot be inside vault_dir ({resolved_vault})"
        )

    # 3. vault_dir cannot be state_db parent dir, or inside state_db parent dir
    if resolved_vault == resolved_db_dir or resolved_db_dir in resolved_vault.parents:
        raise ValueError(
            f"Output vault_dir ({resolved_vault}) cannot be inside or equal to state_db directory ({resolved_db_dir})"
        )

    # 4. state_db directory cannot be inside vault_dir
    if resolved_vault in resolved_db_dir.parents:
        raise ValueError(
            f"state_db directory ({resolved_db_dir}) cannot be inside vault_dir ({resolved_vault})"
        )

    # 5. vault_dir cannot be the db file itself
    if resolved_vault == resolved_db:
        raise ValueError(
            f"Output vault_dir ({resolved_vault}) cannot be the state_db file ({resolved_db})"
        )


def _require_path_inside_vault(path: Path, vault_dir: Path, *, label: str) -> None:
    """Reject a path that resolves outside the Vault or is an existing symlink."""
    if path.exists() and path.is_symlink():
        raise ValueError(f"{label} cannot be a symlink: {path}")
    try:
        path.resolve().relative_to(vault_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} resolves outside vault_dir: {path}") from exc


def _validate_vault_layout(vault_dir: Path) -> tuple[Path, Path]:
    """Create and validate only Exporter-managed top-level Vault directories."""
    if vault_dir.exists() and vault_dir.is_symlink():
        raise ValueError(f"vault_dir cannot be a symlink: {vault_dir}")
    vault_dir.mkdir(parents=True, exist_ok=True)
    notes_dir = vault_dir / "notes"
    assets_dir = vault_dir / "assets"
    for path, label in ((vault_dir, "vault_dir"), (notes_dir, "notes_dir"), (assets_dir, "assets_dir")):
        _require_path_inside_vault(path, vault_dir, label=label)
    notes_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)
    # Validate again after creation, including resolution through pre-existing parents.
    _require_path_inside_vault(notes_dir, vault_dir, label="notes_dir")
    _require_path_inside_vault(assets_dir, vault_dir, label="assets_dir")
    return notes_dir, assets_dir


def _validate_write_target(path: Path, vault_dir: Path) -> None:
    """Fail closed for final and temporary output targets before any write."""
    _require_path_inside_vault(path.parent, vault_dir, label="output parent")
    _require_path_inside_vault(path, vault_dir, label="output target")
    tmp_path = path.with_name(f".{path.name}.tmp")
    _require_path_inside_vault(tmp_path, vault_dir, label="output temporary target")


def _compute_sha256(path: Path, chunk_size: int = 65536) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


@dataclass
class ExportResult:
    export_time: str
    remote_coverage_proof: str
    source_snapshot: dict[str, Any]
    exported_count: int
    skipped_count: int
    failed_count: int
    stale_cleaned_count: int = 0
    cleanup_status: str = "NOT_RUN"
    skipped_notes: list[dict[str, str]] = field(default_factory=list)
    failed_notes: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _atomic_write_text(path: Path, content: str, vault_dir: Path) -> None:
    _validate_write_target(path, vault_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def _atomic_copy_file(src: Path, dst: Path, vault_dir: Path) -> None:
    _validate_write_target(dst, vault_dir)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Check if target already exists with identical size and sha256
    if dst.exists() and dst.is_file():
        src_stat = src.stat()
        dst_stat = dst.stat()
        if src_stat.st_size == dst_stat.st_size and src_stat.st_size > 0:
            if _compute_sha256(src) == _compute_sha256(dst):
                return
    tmp_dst = dst.with_name(f".{dst.name}.tmp")
    shutil.copy2(src, tmp_dst)
    tmp_dst.replace(dst)


def _load_ownership_manifest(path: Path) -> tuple[set[str], set[str], str]:
    """Return managed paths, refusing cleanup when prior ownership is unavailable."""
    if not path.exists():
        return set(), set(), "SKIPPED_NO_OWNERSHIP"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        notes = data["managed_notes"]
        assets = data["managed_assets"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return set(), set(), "SKIPPED_INVALID_OWNERSHIP"
    if not isinstance(notes, list) or not isinstance(assets, list):
        return set(), set(), "SKIPPED_INVALID_OWNERSHIP"
    if not all(is_safe_filename(item) for item in [*notes, *assets]):
        return set(), set(), "SKIPPED_INVALID_OWNERSHIP"
    return set(notes), set(assets), "READY"


def _write_ownership_manifest(path: Path, vault_dir: Path, notes: set[str], assets: set[str]) -> None:
    _atomic_write_text(
        path,
        json.dumps(
            {"managed_notes": sorted(notes), "managed_assets": sorted(assets)},
            ensure_ascii=False,
            indent=2,
        ),
        vault_dir,
    )


def sanitize_note_url(source_url: str, note_id: str = "") -> str:
    """Strips sensitive tracking parameters (e.g. xsec_token, xsec_source) from note URLs.

    Produces a clean, bare canonical note URL: https://www.xiaohongshu.com/explore/<note_id>
    """
    if not source_url and not note_id:
        return ""
    if not source_url and note_id:
        return f"https://www.xiaohongshu.com/explore/{note_id}"

    try:
        parts = urllib.parse.urlsplit(source_url)
        # If it's a xiaohongshu explore URL and note_id is known, produce bare canonical explore URL
        if note_id and ("/explore/" in parts.path or parts.netloc.endswith("xiaohongshu.com")):
            return f"https://www.xiaohongshu.com/explore/{note_id}"

        # Otherwise strip tracking parameters (xsec_token, xsec_source) from query string
        query_pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        clean_pairs = [
            (k, v) for k, v in query_pairs
            if k not in ("xsec_token", "xsec_source")
        ]
        clean_query = urllib.parse.urlencode(clean_pairs)
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, clean_query, parts.fragment))
    except Exception:
        if note_id:
            return f"https://www.xiaohongshu.com/explore/{note_id}"
        return source_url


def render_vault_markdown(canonical: dict[str, Any], note_id: str) -> str:
    """Renders canonical.json into an Obsidian Vault note with relative asset paths.

    Strictly avoids hallucinating tags or publish times: only fields present
    in the canonical source are emitted. Note source URLs are sanitized to bare URLs.
    """
    content_obj = canonical.get("content")
    if not isinstance(content_obj, dict):
        content_obj = {}
    title = (content_obj.get("title") or "").strip() or "无标题笔记"
    text = (content_obj.get("text") or "").strip()

    author_obj = canonical.get("author")
    if not isinstance(author_obj, dict):
        author_obj = {}
    author_name = author_obj.get("name") or "Unknown"
    author_id = author_obj.get("id") or ""

    source_url = canonical.get("source_url") or ""
    if source_url:
        source_url = sanitize_note_url(source_url, note_id)
    collected_at = canonical.get("collected_at") or ""
    collector = canonical.get("collector") or ""

    stats_obj = canonical.get("stats")
    if not isinstance(stats_obj, dict):
        stats_obj = {}

    # Frontmatter
    lines = [
        "---",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"aliases:\n  - {json.dumps(title, ensure_ascii=False)}",
        f"note_id: {json.dumps(note_id, ensure_ascii=False)}",
        f"author_name: {json.dumps(author_name, ensure_ascii=False)}",
    ]
    if author_id:
        lines.append(f"author_id: {json.dumps(author_id, ensure_ascii=False)}")
    if source_url:
        lines.append(f"source_url: {json.dumps(source_url, ensure_ascii=False)}")
    if collected_at:
        lines.append(f"collected_at: {json.dumps(collected_at, ensure_ascii=False)}")
    if collector:
        lines.append(f"collector: {json.dumps(collector, ensure_ascii=False)}")

    # Only include non-null numeric stats
    for key in ("liked_count", "collected_count", "comment_count", "share_count"):
        val = stats_obj.get(key)
        if isinstance(val, int):
            lines.append(f"{key}: {val}")

    lines.append("---")
    lines.append("")

    # Heading & Metadata
    lines.append(f"# {title}")
    lines.append("")
    if author_id:
        author_display = f"[{author_name}](https://www.xiaohongshu.com/user/profile/{author_id})"
    else:
        author_display = author_name
    lines.append(f"- **Author**: {author_display}")
    if source_url:
        lines.append(f"- **Source**: [{source_url}]({source_url})")
    if collected_at:
        lines.append(f"- **Collected At**: {collected_at}")

    stats_display = []
    if isinstance(stats_obj.get("liked_count"), int):
        stats_display.append(f"❤️ {stats_obj['liked_count']}")
    if isinstance(stats_obj.get("collected_count"), int):
        stats_display.append(f"⭐ {stats_obj['collected_count']}")
    if isinstance(stats_obj.get("comment_count"), int):
        stats_display.append(f"💬 {stats_obj['comment_count']}")
    if isinstance(stats_obj.get("share_count"), int):
        stats_display.append(f"🔄 {stats_obj['share_count']}")
    if stats_display:
        lines.append(f"- **Stats**: {' | '.join(stats_display)}")
    lines.append("")

    # Main Content
    lines.append("## Content")
    lines.append("")
    if text:
        lines.append(text)
    else:
        lines.append("*(无文字内容)*")
    lines.append("")

    # Media References (relative to Vault/notes/<note_id>.md -> ../assets/<note_id>/<filename>)
    media_list = canonical.get("media")
    if isinstance(media_list, list) and media_list:
        lines.append("## Media")
        lines.append("")
        for idx, m in enumerate(media_list, 1):
            if not isinstance(m, dict):
                continue
            filename = m.get("filename") or f"asset_{idx}"
            media_type = m.get("type", "image")
            rel_path = f"../assets/{note_id}/{filename}"
            if media_type == "video":
                lines.append(f'<video controls src="{rel_path}" width="100%"></video>')
                lines.append(f"*(Video: [{filename}]({rel_path}))*")
            else:
                lines.append(f"![Image {idx}]({rel_path})")
            lines.append("")

    return "\n".join(lines)


class VaultExporter:
    """Read-only downstream exporter projecting P1 artifacts into an Obsidian Vault."""

    def __init__(
        self,
        state_db: Path = DEFAULT_STATE_DB,
        data_dir: Path = DEFAULT_DATA_DIR,
        vault_dir: Path = DEFAULT_VAULT_DIR,
    ) -> None:
        self.state_db = Path(state_db)
        self.data_dir = Path(data_dir)
        self.vault_dir = Path(vault_dir)
        validate_output_boundary(
            vault_dir=self.vault_dir,
            data_dir=self.data_dir,
            state_db=self.state_db,
        )

    def _open_readonly_db(self) -> sqlite3.Connection:
        if not self.state_db.exists():
            raise FileNotFoundError(f"State database not found: {self.state_db}")
        # Strictly read-only connection
        uri = f"file:{self.state_db.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def export(self, limit: int | None = None) -> ExportResult:
        """Executes the export projection into the target Vault."""
        # Double check output boundary
        validate_output_boundary(
            vault_dir=self.vault_dir,
            data_dir=self.data_dir,
            state_db=self.state_db,
        )

        notes_dir, assets_base_dir = _validate_vault_layout(self.vault_dir)
        ownership_path = self.vault_dir / OWNERSHIP_MANIFEST_NAME
        _require_path_inside_vault(ownership_path, self.vault_dir, label="ownership manifest")
        managed_notes, managed_assets, ownership_status = _load_ownership_manifest(ownership_path)

        with self._open_readonly_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM notes")
            tracked_count = cur.fetchone()[0]

            cur.execute("SELECT count(*) FROM notes WHERE status = 'COMPLETE'")
            complete_candidates = cur.fetchone()[0]

            query = "SELECT note_id, status, title FROM notes WHERE status = 'COMPLETE' ORDER BY rowid"
            if limit is not None:
                query += f" LIMIT {int(limit)}"
            cur.execute(query)
            complete_rows = cur.fetchall()

        evaluated_candidates = len(complete_rows)
        exported: list[str] = []
        skipped: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []

        for row in complete_rows:
            raw_note_id = row["note_id"]

            # 1. Path traversal check on note_id
            if not is_safe_filename(raw_note_id):
                skipped.append({"note_id": str(raw_note_id), "reason": f"INVALID_NOTE_ID_PATH: {raw_note_id!r}"})
                continue
            note_id = raw_note_id

            note_dir = self.data_dir / note_id
            canonical_path = note_dir / "canonical.json"

            # 2. Validate canonical existence & readability
            if not canonical_path.exists():
                skipped.append({"note_id": note_id, "reason": "MISSING_CANONICAL"})
                continue
            try:
                canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                skipped.append({"note_id": note_id, "reason": f"INVALID_CANONICAL_JSON: {exc}"})
                continue

            # 3. Canonical root must be dict
            if not isinstance(canonical, dict):
                skipped.append(
                    {
                        "note_id": note_id,
                        "reason": f"INVALID_CANONICAL_SCHEMA: expected JSON object, got {type(canonical).__name__}",
                    }
                )
                continue

            # 4. Validate note_id identity match
            if canonical.get("note_id") != note_id:
                skipped.append(
                    {
                        "note_id": note_id,
                        "reason": f"NOTE_ID_MISMATCH: canonical has {canonical.get('note_id')!r}",
                    }
                )
                continue

            # 5. Check content and author types if present
            if "content" in canonical and not isinstance(canonical["content"], dict):
                skipped.append(
                    {
                        "note_id": note_id,
                        "reason": f"INVALID_CANONICAL_SCHEMA: content must be dict, got {type(canonical['content']).__name__}",
                    }
                )
                continue

            if "author" in canonical and not isinstance(canonical["author"], dict):
                skipped.append(
                    {
                        "note_id": note_id,
                        "reason": f"INVALID_CANONICAL_SCHEMA: author must be dict, got {type(canonical['author']).__name__}",
                    }
                )
                continue

            # 6. Validate media list and items
            media_list = canonical.get("media")
            if media_list is not None and not isinstance(media_list, list):
                skipped.append(
                    {
                        "note_id": note_id,
                        "reason": f"INVALID_CANONICAL_SCHEMA: media must be list, got {type(media_list).__name__}",
                    }
                )
                continue

            media_items = media_list or []
            media_valid = True
            for m in media_items:
                if not isinstance(m, dict):
                    skipped.append(
                        {
                            "note_id": note_id,
                            "reason": f"INVALID_CANONICAL_SCHEMA: media item must be dict, got {type(m).__name__}",
                        }
                    )
                    media_valid = False
                    break

                filename = m.get("filename")
                if not filename or not is_safe_filename(filename):
                    skipped.append(
                        {
                            "note_id": note_id,
                            "reason": f"INVALID_MEDIA_FILENAME: {filename!r}",
                        }
                    )
                    media_valid = False
                    break

                expected_size = m.get("size_bytes")
                if expected_size is None or not isinstance(expected_size, int):
                    skipped.append(
                        {
                            "note_id": note_id,
                            "reason": f"MEDIA_MISSING_SIZE_BYTES: {filename}",
                        }
                    )
                    media_valid = False
                    break

                asset_src = note_dir / "assets" / filename
                if not asset_src.exists():
                    skipped.append({"note_id": note_id, "reason": f"MEDIA_FILE_MISSING: {filename}"})
                    media_valid = False
                    break

                stat_size = asset_src.stat().st_size
                if stat_size == 0:
                    skipped.append({"note_id": note_id, "reason": f"MEDIA_FILE_EMPTY: {filename}"})
                    media_valid = False
                    break

                if stat_size != expected_size:
                    skipped.append(
                        {
                            "note_id": note_id,
                            "reason": f"MEDIA_SIZE_MISMATCH: {filename} (expected {expected_size}, got {stat_size})",
                        }
                    )
                    media_valid = False
                    break

            if not media_valid:
                continue

            # 7. Perform Vault projection (Assets copy + Markdown render)
            try:
                # Copy media assets
                if media_items:
                    vault_note_assets = assets_base_dir / note_id
                    _require_path_inside_vault(vault_note_assets, self.vault_dir, label="note assets directory")
                    vault_note_assets.mkdir(parents=True, exist_ok=True)
                    _require_path_inside_vault(vault_note_assets, self.vault_dir, label="note assets directory")
                    for m in media_items:
                        fn = m["filename"]
                        src_file = note_dir / "assets" / fn
                        dst_file = vault_note_assets / fn
                        _atomic_copy_file(src_file, dst_file, self.vault_dir)

                # Render & write Markdown
                md_content = render_vault_markdown(canonical, note_id)
                md_path = notes_dir / f"{note_id}.md"
                _atomic_write_text(md_path, md_content, self.vault_dir)

                exported.append(note_id)
            except ValueError:
                # A boundary failure must abort rather than be downgraded to a note failure.
                raise
            except Exception as exc:
                logger.error("[exporter] failed projecting note %s: %s", note_id, exc)
                failed.append({"note_id": note_id, "error": str(exc)})

        # 8. Stale artifact reconciliation (Managed file cleanup)
        stale_cleaned_count = 0
        cleanup_status = "SKIPPED_LIMITED_RUN" if limit is not None else ownership_status
        if limit is None:
            # Full run: prune only artifacts explicitly owned by a prior exporter run.
            exported_set = set(exported)
            if ownership_status == "READY":
                cleanup_status = "COMPLETED"
                for note_id in managed_notes - exported_set:
                    md_file = notes_dir / f"{note_id}.md"
                    _validate_write_target(md_file, self.vault_dir)
                    if md_file.exists():
                        try:
                            md_file.unlink()
                            stale_cleaned_count += 1
                            logger.info("[exporter] pruned stale note: %s", md_file.name)
                        except OSError as exc:
                            raise RuntimeError(f"failed to prune managed stale note {md_file}: {exc}") from exc

                for note_id in managed_assets - exported_set:
                    asset_folder = assets_base_dir / note_id
                    _validate_write_target(asset_folder, self.vault_dir)
                    if asset_folder.exists():
                        try:
                            shutil.rmtree(asset_folder)
                            stale_cleaned_count += 1
                            logger.info("[exporter] pruned stale asset dir: %s", asset_folder.name)
                        except OSError as exc:
                            raise RuntimeError(f"failed to prune managed stale assets {asset_folder}: {exc}") from exc

        # Ownership is deliberately separate from this run's result manifest.  A limited
        # run retains prior ownership; a full run converges ownership to successful output.
        if limit is None and ownership_status == "READY":
            next_managed_notes = set(exported)
            next_managed_assets = set(exported)
        else:
            next_managed_notes = managed_notes | set(exported)
            next_managed_assets = managed_assets | set(exported)
        _write_ownership_manifest(
            ownership_path,
            self.vault_dir,
            next_managed_notes,
            next_managed_assets,
        )

        # 9. Atomic write export_manifest.json
        result = ExportResult(
            export_time=get_current_iso_time(),
            remote_coverage_proof="UNPROVEN",
            source_snapshot={
                "state_db": str(self.state_db),
                "data_dir": str(self.data_dir),
                "tracked_count": tracked_count,
                "complete_candidates": complete_candidates,
                "evaluated_candidates": evaluated_candidates,
            },
            exported_count=len(exported),
            skipped_count=len(skipped),
            failed_count=len(failed),
            stale_cleaned_count=stale_cleaned_count,
            cleanup_status=cleanup_status,
            skipped_notes=skipped,
            failed_notes=failed,
        )

        manifest_path = self.vault_dir / "export_manifest.json"
        _atomic_write_text(
            manifest_path,
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            self.vault_dir,
        )

        return result
