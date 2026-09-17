"""DigestWriter: Atomic serialization and physical boundary isolation for Phase C.2 MVP.

Writes:
1. Markdown artifact: Vault/digests/<YYYY-MM-DD>_<digest_name>.md
2. Manifest artifact: Vault/digests/<YYYY-MM-DD>_<digest_name>.manifest.json

Enforces:
- Physical boundary containment strictly within Vault/digests/ (symlink escape prevention).
- Atomic writing via temporary files (.tmp) and POSIX atomic replacement.
- Unknown file overwrite protection: refuses to overwrite files that lack digest signatures.
- Fail-closed semantics: 0 validated excerpts -> 0 bytes markdown written (file omitted),
  manifest written with artifact_status: "INCOMPLETE" and verified_excerpts_count: 0.
- Zero NLP, zero summarization, zero rewriting, zero editorial commentary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xhs_knowledge.contracts import (
    BoundaryViolationError,
    DigestRequest,
    EvidenceBundle,
    EvidenceExcerpt,
)
from xhs_knowledge.validator import ValidationResult


@dataclass
class DigestWriterResult:
    """Structured result of DigestWriter execution."""
    markdown_path: Path | None
    manifest_path: Path
    artifact_status: str  # "COMPLETE" | "INCOMPLETE"
    content_mode: str  # "VERIFIED_SOURCE_EXCERPTS"
    markdown_sha256: str
    manifest_sha256: str
    verified_excerpts_count: int
    omitted_excerpts_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "markdown_path": str(self.markdown_path) if self.markdown_path else None,
            "manifest_path": str(self.manifest_path),
            "artifact_status": self.artifact_status,
            "content_mode": self.content_mode,
            "markdown_sha256": self.markdown_sha256,
            "manifest_sha256": self.manifest_sha256,
            "verified_excerpts_count": self.verified_excerpts_count,
            "omitted_excerpts_count": self.omitted_excerpts_count,
        }


class DigestWriter:
    """Serializes validated evidence excerpts to Obsidian Markdown and JSON manifest."""

    def __init__(self, vault_dir: Path | str) -> None:
        self.vault_dir = Path(vault_dir).resolve()
        self.digests_dir = (self.vault_dir / "digests").resolve()

    def _validate_safe_name(self, name: str, field_name: str) -> None:
        """Validates that a filename component contains no traversal or illegal characters."""
        if not name or not name.strip():
            raise BoundaryViolationError(f"{field_name} cannot be empty.")
        if "/" in name or "\\" in name or ".." in name:
            raise BoundaryViolationError(
                f"{field_name} '{name}' contains illegal path characters or traversal sequences."
            )

    def _require_inside_digests(self, path: Path) -> Path:
        """Enforces that target path resolves strictly inside Vault/digests/ without symlink escape."""
        resolved_digests = self.digests_dir.resolve()
        try:
            resolved_digests.relative_to(self.vault_dir.resolve())
        except ValueError:
            raise BoundaryViolationError(
                f"Digests directory '{resolved_digests}' escapes vault boundary '{self.vault_dir}'."
            )

        # Check if path or any existing ancestor is a symlink
        cur = path
        while True:
            if os.path.islink(cur):
                raise BoundaryViolationError(
                    f"Symlink boundary violation: path component '{cur}' is a symbolic link."
                )
            parent = cur.parent
            if parent == cur:
                break
            cur = parent

        # Check resolved path is strictly inside digests_dir
        resolved = path.resolve()
        try:
            resolved.relative_to(resolved_digests)
        except ValueError:
            raise BoundaryViolationError(
                f"Target path '{resolved}' escapes physical boundary '{resolved_digests}'."
            )

        return resolved

    def _is_known_digest_artifact(self, path: Path) -> bool:
        """Checks whether an existing file has the signature of a previously generated digest artifact."""
        if not path.is_file():
            return False
        try:
            if path.suffix == ".md":
                with open(path, "r", encoding="utf-8") as f:
                    header = f.read(1024)
                return (
                    header.startswith("---\n")
                    and 'content_mode: "VERIFIED_SOURCE_EXCERPTS"' in header
                    and "digest_name:" in header
                )
            elif path.suffix == ".json":
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return (
                    isinstance(data, dict)
                    and data.get("manifest_version") == "1.0"
                    and data.get("content_mode") == "VERIFIED_SOURCE_EXCERPTS"
                )
        except Exception:
            return False
        return False

    def _atomic_write_text(self, target_path: Path, content: str, overwrite: bool = True) -> str:
        """Atomically writes UTF-8 text to target_path via a temp file. Returns SHA256."""
        self._require_inside_digests(target_path)

        if target_path.exists():
            if not overwrite:
                raise FileExistsError(f"Target file '{target_path.name}' already exists and overwrite=False.")
            if not self._is_known_digest_artifact(target_path):
                raise BoundaryViolationError(
                    f"Refusing to overwrite unknown/non-digest file at '{target_path}'."
                )

        self.digests_dir.mkdir(parents=True, exist_ok=True)

        tmp_path = self.digests_dir / f".tmp_{target_path.name}_{os.getpid()}_{uuid.uuid4().hex}"
        try:
            data = content.encode("utf-8")
            content_sha256 = hashlib.sha256(data).hexdigest()
            with open(tmp_path, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_path, target_path)
            return content_sha256
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def format_markdown(
        self,
        request: DigestRequest,
        bundle: EvidenceBundle,
        validation_result: ValidationResult,
        generated_at: str,
        artifact_status: str = "COMPLETE",
    ) -> str:
        """Formats verified excerpts into Obsidian Markdown specification."""
        excerpts_by_note: dict[str, list[EvidenceExcerpt]] = {}
        for exc in validation_result.validated_excerpts:
            excerpts_by_note.setdefault(exc.note_id, []).append(exc)

        referenced_notes = [n for n in bundle.notes if n.note_id in excerpts_by_note]
        source_collection = request.source.collections[0]
        manifest_filename = f"{request.target_date}_{request.digest_name}.manifest.json"

        lines = [
            "---",
            f'digest_name: "{request.digest_name}"',
            f'target_date: "{request.target_date}"',
            f'source_collection: "{source_collection}"',
            f'generated_at: "{generated_at}"',
            f"notes_referenced: {len(referenced_notes)}",
            f"excerpts_count: {len(validation_result.validated_excerpts)}",
            f'artifact_status: "{artifact_status}"',
            'content_mode: "VERIFIED_SOURCE_EXCERPTS"',
            "---",
            "",
            f"# Knowledge Digest: {request.digest_name} ({request.target_date})",
            "",
            f"- **Source Collection**: `{source_collection}`",
            "- **Content Mode**: Verified Source Excerpts (Non-interpretive)",
            f"- **Generated At**: {generated_at}",
            "",
            "## 📌 Verified Source Excerpts",
            "",
        ]

        for note in referenced_notes:
            if note.author_name:
                lines.append(f"### [[{note.note_id}|{note.title}]] — *{note.author_name}*")
            else:
                lines.append(f"### [[{note.note_id}|{note.title}]]")

            for exc in excerpts_by_note[note.note_id]:
                quote_lines = exc.verbatim_quote.splitlines()
                if len(quote_lines) <= 1:
                    lines.append(f'- > "{exc.verbatim_quote}"')
                else:
                    first = f'- > "{quote_lines[0]}'
                    rest = [f"  > {ql}" for ql in quote_lines[1:-1]]
                    last = f'  > {quote_lines[-1]}"'
                    lines.extend([first] + rest + [last])

            lines.append("")

        lines.extend([
            "---",
            "",
            "## 📊 Digest Provenance & Audit",
            f"- **Referenced Notes**: {len(referenced_notes)}",
            "- **Evidence Verification**: 100% verified against local vault artifacts",
            f"- **Audit Manifest**: `digests/{manifest_filename}`",
            "",
        ])

        return "\n".join(lines)

    def format_manifest(
        self,
        request: DigestRequest,
        bundle: EvidenceBundle,
        validation_result: ValidationResult,
        markdown_rel_path: str,
        markdown_sha256: str,
        generated_at: str,
        artifact_status: str,
    ) -> dict[str, Any]:
        """Formats execution provenance into JSON manifest specification."""
        inputs = [
            {
                "note_id": n.note_id,
                "title": n.title,
                "sha256": n.file_sha256,
            }
            for n in bundle.notes
        ]

        total_candidates = validation_result.validated_count + validation_result.failed_count

        manifest = {
            "manifest_version": "1.0",
            "digest_name": request.digest_name,
            "target_date": request.target_date,
            "source_collection": request.source.collections[0],
            "generated_at": generated_at,
            "bundle_id": bundle.bundle_id,
            "request_fingerprint": bundle.request_fingerprint,
            "bundle_content_hash": bundle.bundle_content_hash,
            "inputs": inputs,
            "candidate_excerpts_count": total_candidates,
            "verified_excerpts_count": validation_result.validated_count,
            "omitted_excerpts_count": validation_result.failed_count,
            "output_file": markdown_rel_path,
            "output_sha256": markdown_sha256,
            "artifact_status": artifact_status,
            "content_mode": "VERIFIED_SOURCE_EXCERPTS",
            "errors": [e.to_dict() for e in validation_result.errors],
        }
        return manifest

    def write(
        self,
        request: DigestRequest,
        bundle: EvidenceBundle,
        validation_result: ValidationResult,
        generated_at: str | None = None,
        overwrite: bool = True,
    ) -> DigestWriterResult:
        """Serializes the digest and manifest according to validated provenance.

        Fail-closed:
        - If validated_count == 0: writes zero bytes to .md (omitted), writes INCOMPLETE manifest.
        - If validated_count > 0 and is_valid: writes COMPLETE markdown and manifest.
        - If validated_count > 0 and not is_valid: writes INCOMPLETE markdown and manifest.
        """
        # Validate request parameters for boundary safety
        self._validate_safe_name(request.digest_name, "digest_name")
        self._validate_safe_name(request.target_date, "target_date")

        if not re.match(r"^\d{4}-\d{2}-\d{2}$", request.target_date):
            raise BoundaryViolationError(
                f"Invalid target_date '{request.target_date}'. Expected YYYY-MM-DD format."
            )
        if not re.match(r"^[a-zA-Z0-9_\-]+$", request.digest_name):
            raise BoundaryViolationError(
                f"Invalid digest_name '{request.digest_name}'. Only letters, numbers, hyphens, and underscores allowed."
            )
        if len(request.source.collections) != 1:
            raise ValueError(
                f"DigestRequest must contain exactly 1 collection, got {len(request.source.collections)}."
            )

        if generated_at is None:
            generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        markdown_filename = f"{request.target_date}_{request.digest_name}.md"
        manifest_filename = f"{request.target_date}_{request.digest_name}.manifest.json"

        markdown_path = self.digests_dir / markdown_filename
        manifest_path = self.digests_dir / manifest_filename

        # Fail closed on zero validated excerpts
        if validation_result.validated_count == 0:
            artifact_status = "INCOMPLETE"
            manifest_data = self.format_manifest(
                request=request,
                bundle=bundle,
                validation_result=validation_result,
                markdown_rel_path="",
                markdown_sha256="",
                generated_at=generated_at,
                artifact_status=artifact_status,
            )
            manifest_json = json.dumps(manifest_data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
            manifest_sha256 = self._atomic_write_text(manifest_path, manifest_json, overwrite=overwrite)

            return DigestWriterResult(
                markdown_path=None,
                manifest_path=manifest_path,
                artifact_status=artifact_status,
                content_mode="VERIFIED_SOURCE_EXCERPTS",
                markdown_sha256="",
                manifest_sha256=manifest_sha256,
                verified_excerpts_count=0,
                omitted_excerpts_count=validation_result.failed_count,
            )

        artifact_status = "COMPLETE" if validation_result.is_valid else "INCOMPLETE"

        markdown_content = self.format_markdown(
            request=request,
            bundle=bundle,
            validation_result=validation_result,
            generated_at=generated_at,
            artifact_status=artifact_status,
        )
        markdown_sha256 = self._atomic_write_text(markdown_path, markdown_content, overwrite=overwrite)

        manifest_data = self.format_manifest(
            request=request,
            bundle=bundle,
            validation_result=validation_result,
            markdown_rel_path=f"digests/{markdown_filename}",
            markdown_sha256=markdown_sha256,
            generated_at=generated_at,
            artifact_status=artifact_status,
        )
        manifest_json = json.dumps(manifest_data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        manifest_sha256 = self._atomic_write_text(manifest_path, manifest_json, overwrite=overwrite)

        return DigestWriterResult(
            markdown_path=markdown_path,
            manifest_path=manifest_path,
            artifact_status=artifact_status,
            content_mode="VERIFIED_SOURCE_EXCERPTS",
            markdown_sha256=markdown_sha256,
            manifest_sha256=manifest_sha256,
            verified_excerpts_count=validation_result.validated_count,
            omitted_excerpts_count=validation_result.failed_count,
        )
