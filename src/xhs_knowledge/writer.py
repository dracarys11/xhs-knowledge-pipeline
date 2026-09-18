"""DigestWriter: Atomic serialization and physical boundary isolation for Phase C.2 MVP.

Writes:
1. Markdown artifact: Vault/digests/<YYYY-MM-DD>_<digest_name>.md
2. Manifest artifact: Vault/digests/<YYYY-MM-DD>_<digest_name>.manifest.json

Enforces:
- Physical boundary containment strictly within Vault/digests/ (symlink escape prevention).
- Two-phase prepare-and-publish with atomic rollback: failure during publication cannot
  leave a mismatched valid artifact pair.
- Unknown file overwrite protection: refuses to overwrite or delete files that lack digest signatures.
- Stale Markdown cleanup: on zero-valid rerun, removes existing managed Markdown file and publishes
  only an INCOMPLETE manifest.
- Verbatim evidence preservation: persists exact un-stripped, un-normalized verbatim_quote in manifest.
- ValidationResult trust boundary: validates counts and re-verifies structural provenance of all
  validated excerpts against EvidenceBundle before publishing.
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
from xhs_knowledge.validator import ProvenanceValidator, ValidationResult


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

    def _assert_validation_trust_boundary(
        self, validation_result: ValidationResult, bundle: EvidenceBundle
    ) -> None:
        """Enforces trust boundary on ValidationResult before publishing.

        Checks:
        1. Correct type and internal count consistency.
        2. Status and count invariant: PASS requires validated_count > 0 and failed_count == 0.
        3. Structural provenance re-verification of every validated excerpt against EvidenceBundle.
        """
        if not isinstance(validation_result, ValidationResult):
            raise ValueError(
                f"validation_result must be a ValidationResult instance, got {type(validation_result).__name__}."
            )

        if validation_result.validated_count != len(validation_result.validated_excerpts):
            raise ValueError(
                f"ValidationResult internal inconsistency: validated_count ({validation_result.validated_count}) "
                f"!= len(validated_excerpts) ({len(validation_result.validated_excerpts)})."
            )

        if validation_result.failed_count != len(validation_result.omitted_excerpts):
            raise ValueError(
                f"ValidationResult internal inconsistency: failed_count ({validation_result.failed_count}) "
                f"!= len(omitted_excerpts) ({len(validation_result.omitted_excerpts)})."
            )

        if validation_result.status == "PASS" and (
            validation_result.failed_count > 0 or validation_result.validated_count == 0
        ):
            raise ValueError(
                f"ValidationResult invalid status invariant: status is PASS but validated_count="
                f"{validation_result.validated_count}, failed_count={validation_result.failed_count}."
            )

        # Structural provenance re-verification against bundle
        validator = ProvenanceValidator()
        for exc in validation_result.validated_excerpts:
            ok, err = validator.validate_excerpt(exc, bundle)
            if not ok:
                err_code = err.code if err else "PROVENANCE_RECHECK_FAILED"
                err_msg = err.message if err else "Provenance re-check failed"
                note_id = getattr(exc, "note_id", "unknown")
                raise ValueError(
                    f"ValidationResult trust boundary violation: excerpt for note '{note_id}' "
                    f"failed structural provenance re-verification ({err_code}: {err_msg})."
                )

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

        # Preserve verbatim_quote with 100% fidelity in manifest
        verified_excerpts = [
            {
                "note_id": exc.note_id,
                "source_file_sha256": exc.source_file_sha256,
                "verbatim_quote": exc.verbatim_quote,
            }
            for exc in validation_result.validated_excerpts
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
            "verified_excerpts": verified_excerpts,
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

        Fail-closed guarantees:
        - ValidationResult trust boundary re-checks all validated excerpts.
        - Zero validated excerpts: removes any stale same-name Markdown file and publishes
          only an INCOMPLETE manifest.
        - Two-phase prepare-and-publish: failures during commit roll back cleanly,
          preventing mismatched artifact pairs.
        - Preserves unknown-file protection on both .md and .manifest.json targets.
        """
        # 1. Parameter format & boundary validation
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

        # 2. Enforce trust boundary on ValidationResult
        self._assert_validation_trust_boundary(validation_result, bundle)

        if generated_at is None:
            generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        markdown_filename = f"{request.target_date}_{request.digest_name}.md"
        manifest_filename = f"{request.target_date}_{request.digest_name}.manifest.json"

        markdown_path = self.digests_dir / markdown_filename
        manifest_path = self.digests_dir / manifest_filename

        self._require_inside_digests(markdown_path)
        self._require_inside_digests(manifest_path)
        self.digests_dir.mkdir(parents=True, exist_ok=True)

        # =====================================================================
        # CASE 1: Zero Validated Excerpts (Fail-Closed, Remove Stale Markdown)
        # =====================================================================
        if validation_result.validated_count == 0:
            artifact_status = "INCOMPLETE"

            # Check unknown file protection before modifying anything
            if markdown_path.exists():
                if not overwrite:
                    raise FileExistsError(f"Target file '{markdown_path.name}' already exists and overwrite=False.")
                if not self._is_known_digest_artifact(markdown_path):
                    raise BoundaryViolationError(
                        f"Refusing to remove unknown/non-digest file at '{markdown_path}'."
                    )
            if manifest_path.exists():
                if not overwrite:
                    raise FileExistsError(f"Target file '{manifest_path.name}' already exists and overwrite=False.")
                if not self._is_known_digest_artifact(manifest_path):
                    raise BoundaryViolationError(
                        f"Refusing to overwrite unknown/non-digest file at '{manifest_path}'."
                    )

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
            manifest_bytes = manifest_json.encode("utf-8")
            manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

            # Staging temp file for manifest
            tmp_manifest = self.digests_dir / f".tmp_{manifest_filename}_{os.getpid()}_{uuid.uuid4().hex}"
            backup_md = self.digests_dir / f".bak_{markdown_filename}_{os.getpid()}_{uuid.uuid4().hex}" if markdown_path.exists() else None
            backup_manifest = self.digests_dir / f".bak_{manifest_filename}_{os.getpid()}_{uuid.uuid4().hex}" if manifest_path.exists() else None

            try:
                with open(tmp_manifest, "wb") as f:
                    f.write(manifest_bytes)
                    f.flush()
                    os.fsync(f.fileno())

                # Transactional commit: move existing files to backup
                if backup_md:
                    os.replace(markdown_path, backup_md)
                if backup_manifest:
                    os.replace(manifest_path, backup_manifest)

                # Publish manifest
                os.replace(tmp_manifest, manifest_path)

                # Clean up backups (stale markdown is safely removed!)
                if backup_md and backup_md.exists():
                    backup_md.unlink()
                if backup_manifest and backup_manifest.exists():
                    backup_manifest.unlink()

            except Exception:
                # Rollback on failure
                if backup_md and backup_md.exists():
                    os.replace(backup_md, markdown_path)
                if backup_manifest and backup_manifest.exists():
                    os.replace(backup_manifest, manifest_path)
                raise
            finally:
                if tmp_manifest.exists():
                    try:
                        tmp_manifest.unlink()
                    except OSError:
                        pass
                if backup_md and backup_md.exists():
                    try:
                        backup_md.unlink()
                    except OSError:
                        pass
                if backup_manifest and backup_manifest.exists():
                    try:
                        backup_manifest.unlink()
                    except OSError:
                        pass

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

        # =====================================================================
        # CASE 2: Validated Excerpts > 0 (Two-Phase Prepare-and-Publish)
        # =====================================================================
        artifact_status = "COMPLETE" if validation_result.is_valid else "INCOMPLETE"

        # Check unknown file protection before preparing or modifying
        for target in (markdown_path, manifest_path):
            if target.exists():
                if not overwrite:
                    raise FileExistsError(f"Target file '{target.name}' already exists and overwrite=False.")
                if not self._is_known_digest_artifact(target):
                    raise BoundaryViolationError(
                        f"Refusing to overwrite unknown/non-digest file at '{target}'."
                    )

        # Phase 1: Prepare contents & checksums
        markdown_content = self.format_markdown(
            request=request,
            bundle=bundle,
            validation_result=validation_result,
            generated_at=generated_at,
            artifact_status=artifact_status,
        )
        markdown_bytes = markdown_content.encode("utf-8")
        markdown_sha256 = hashlib.sha256(markdown_bytes).hexdigest()

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
        manifest_bytes = manifest_json.encode("utf-8")
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

        # Write both to temp staging files
        tmp_md = self.digests_dir / f".tmp_{markdown_filename}_{os.getpid()}_{uuid.uuid4().hex}"
        tmp_manifest = self.digests_dir / f".tmp_{manifest_filename}_{os.getpid()}_{uuid.uuid4().hex}"
        backup_md = self.digests_dir / f".bak_{markdown_filename}_{os.getpid()}_{uuid.uuid4().hex}" if markdown_path.exists() else None
        backup_manifest = self.digests_dir / f".bak_{manifest_filename}_{os.getpid()}_{uuid.uuid4().hex}" if manifest_path.exists() else None

        md_committed = False
        manifest_committed = False

        try:
            with open(tmp_md, "wb") as f_md:
                f_md.write(markdown_bytes)
                f_md.flush()
                os.fsync(f_md.fileno())

            with open(tmp_manifest, "wb") as f_mf:
                f_mf.write(manifest_bytes)
                f_mf.flush()
                os.fsync(f_mf.fileno())

            # Phase 2: Atomic Publication with Rollback
            # Backup existing
            if backup_md:
                os.replace(markdown_path, backup_md)
            if backup_manifest:
                os.replace(manifest_path, backup_manifest)

            # Publish markdown
            os.replace(tmp_md, markdown_path)
            md_committed = True

            # Publish manifest
            os.replace(tmp_manifest, manifest_path)
            manifest_committed = True

            # Publication complete: remove backups
            if backup_md and backup_md.exists():
                backup_md.unlink()
            if backup_manifest and backup_manifest.exists():
                backup_manifest.unlink()

        except Exception:
            # Transaction Rollback: restore previous state or remove orphaned published file
            if md_committed:
                if backup_md and backup_md.exists():
                    os.replace(backup_md, markdown_path)
                elif markdown_path.exists():
                    markdown_path.unlink()
            elif backup_md and backup_md.exists():
                os.replace(backup_md, markdown_path)

            if manifest_committed:
                if backup_manifest and backup_manifest.exists():
                    os.replace(backup_manifest, manifest_path)
                elif manifest_path.exists():
                    manifest_path.unlink()
            elif backup_manifest and backup_manifest.exists():
                os.replace(backup_manifest, manifest_path)

            raise
        finally:
            # Clean up temp and backup files
            for p in (tmp_md, tmp_manifest, backup_md, backup_manifest):
                if p and p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass

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
