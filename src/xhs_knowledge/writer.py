"""DigestWriter: Atomic serialization and physical boundary isolation for Phase C.2 MVP.

Publication layout (immutable-generation + atomic-pointer model):

    Vault/digests/<artifact-key>/
        generations/
            <generation-id>/
                digest.md          # absent for zero-evidence generations
                manifest.json
        current.json

Crash-safety design (honest scope): the complete immutable generation is prepared
and fsynced before publication; a single atomically replaced ``current.json`` file
acts as the publication commit point. This is NOT a multi-file filesystem
transaction — atomicity comes from having one pointer file as the commit point,
not from cross-file atomic replace. Guarantees are those of POSIX rename + fsync:

- Crash/failure before the current.json replacement: the previous current.json
  remains authoritative and the previous generation remains fully valid; any
  orphaned staging directory or unreferenced generation is never current.
- Crash after the current.json replacement: the new generation was already fully
  prepared, so the pointer references a complete generation.
- There is never an authoritative state mixing new Markdown with an old manifest
  (or vice versa): both live inside one immutable generation directory.

Generation identity is deterministic: the generation ID is derived from a SHA256
over the manifest bytes, which cover only stable inputs (request fingerprint,
bundle content hash, generated_at, validated excerpt structural contents, counts,
status/errors). Fixed inputs plus fixed generated_at therefore reproduce the same
generation ID and byte-identical artifacts. Published generations are immutable
and never mutated, overwritten, or deleted; no backup/rollback mechanism exists.

Also enforces:
- Physical boundary containment strictly within Vault/digests/ (symlink escape
  prevention); current.json is always a regular file, never a symlink.
- Unknown file protection: refuses to publish over foreign files or directories
  that lack digest publication signatures.
- Verbatim evidence preservation: persists exact un-stripped, un-normalized
  verbatim_quote in manifest.
- ValidationResult trust boundary: validates status/count/error consistency and
  re-verifies structural provenance of all validated excerpts against EvidenceBundle
  before publishing anything.
- Zero NLP, zero summarization, zero rewriting, zero editorial commentary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
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

GENERATIONS_DIRNAME = "generations"
CURRENT_POINTER_FILENAME = "current.json"
GENERATION_MARKDOWN_FILENAME = "digest.md"
GENERATION_MANIFEST_FILENAME = "manifest.json"
POINTER_VERSION = "1.0"

_VALID_STATUSES = ("PASS", "FAILED")
_VALID_ARTIFACT_STATUSES = ("COMPLETE", "INCOMPLETE")


@dataclass
class DigestWriterResult:
    """Structured result of DigestWriter execution."""
    artifact_key: str
    generation_id: str
    generation_dir: Path
    markdown_path: Path | None
    manifest_path: Path
    current_pointer_path: Path
    artifact_status: str  # "COMPLETE" | "INCOMPLETE"
    content_mode: str  # "VERIFIED_SOURCE_EXCERPTS"
    markdown_sha256: str
    manifest_sha256: str
    verified_excerpts_count: int
    omitted_excerpts_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_key": self.artifact_key,
            "generation_id": self.generation_id,
            "generation_dir": str(self.generation_dir),
            "markdown_path": str(self.markdown_path) if self.markdown_path else None,
            "manifest_path": str(self.manifest_path),
            "current_pointer_path": str(self.current_pointer_path),
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

    def _is_managed_artifact_dir(self, artifact_dir: Path) -> bool:
        """Checks whether an existing directory only contains digest-managed entries.

        Managed entries: the pointer file, the generations root, and hidden staging
        leftovers from interrupted publications. Any other visible entry marks the
        directory as foreign user data.
        """
        managed_names = {CURRENT_POINTER_FILENAME, GENERATIONS_DIRNAME}
        try:
            entries = list(artifact_dir.iterdir())
        except OSError:
            return False
        for child in entries:
            if child.name in managed_names or child.name.startswith("."):
                continue
            return False
        return True

    def _is_known_current_pointer(self, path: Path) -> bool:
        """Checks whether an existing file has the signature of a published current.json pointer."""
        if not path.is_file() or path.is_symlink():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        return (
            isinstance(data, dict)
            and data.get("pointer_version") == POINTER_VERSION
            and data.get("content_mode") == "VERIFIED_SOURCE_EXCERPTS"
            and isinstance(data.get("artifact_key"), str)
            and bool(data.get("artifact_key"))
            and isinstance(data.get("generation_id"), str)
            and bool(data.get("generation_id"))
            and data.get("artifact_status") in _VALID_ARTIFACT_STATUSES
            and isinstance(data.get("manifest"), str)
            and (data.get("markdown") is None or isinstance(data.get("markdown"), str))
        )

    def _assert_validation_trust_boundary(
        self, validation_result: ValidationResult, bundle: EvidenceBundle
    ) -> None:
        """Enforces trust boundary on ValidationResult before publishing anything.

        Checks:
        1. Correct type, known status, and internal count consistency.
        2. Status invariants:
           - PASS requires validated_count > 0, failed_count == 0, and errors == [].
           - FAILED requires errors != [].
        3. Structural provenance re-verification of every validated excerpt against EvidenceBundle.
        """
        if not isinstance(validation_result, ValidationResult):
            raise ValueError(
                f"validation_result must be a ValidationResult instance, got {type(validation_result).__name__}."
            )

        if validation_result.status not in _VALID_STATUSES:
            raise ValueError(
                f"ValidationResult invalid status: '{validation_result.status}'. "
                f"Expected one of {_VALID_STATUSES}."
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

        if validation_result.status == "PASS":
            if validation_result.validated_count <= 0 or validation_result.failed_count != 0:
                raise ValueError(
                    f"ValidationResult invalid status invariant: status is PASS but validated_count="
                    f"{validation_result.validated_count}, failed_count={validation_result.failed_count}."
                )
            if validation_result.errors:
                raise ValueError(
                    f"ValidationResult invalid status invariant: status is PASS but errors is non-empty "
                    f"({len(validation_result.errors)} error(s))."
                )
        else:  # FAILED
            if not validation_result.errors:
                raise ValueError(
                    "ValidationResult invalid status invariant: status is FAILED but errors is empty."
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

    @staticmethod
    def _write_file_fsynced(path: Path, data: bytes) -> None:
        """Writes bytes to a fresh file and fsyncs it so contents survive a crash."""
        with open(path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        """Best-effort fsync of a directory so rename/create operations are durable."""
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _verify_existing_generation(
        self,
        generation_dir: Path,
        markdown_bytes: bytes | None,
        manifest_bytes: bytes,
    ) -> None:
        """Fails closed unless an existing generation dir exactly matches the expected bytes.

        Deterministic generation IDs can collide with a previously published (or
        orphaned) generation. Identical content is safely reused; anything else
        (missing/extra files, byte differences, non-directory, symlinked directory,
        or symlinked/irregular generation members) is rejected without mutating it.
        """
        if generation_dir.is_symlink() or not generation_dir.is_dir():
            raise ValueError(
                f"Refusing to publish: deterministic generation path '{generation_dir}' "
                f"already exists and is not a managed generation directory."
            )
        expected_names = {GENERATION_MANIFEST_FILENAME}
        if markdown_bytes is not None:
            expected_names.add(GENERATION_MARKDOWN_FILENAME)
        try:
            actual_names = {child.name for child in generation_dir.iterdir()}
        except OSError as exc:
            raise ValueError(
                f"Refusing to publish: cannot inspect existing generation '{generation_dir}': {exc}."
            ) from exc
        if actual_names != expected_names:
            raise ValueError(
                f"Refusing to publish: existing generation '{generation_dir}' contains "
                f"unexpected content (found {sorted(actual_names)}, expected {sorted(expected_names)})."
            )
        # Members must be regular non-symlink files inside the generation directory;
        # a symlink child resolving to byte-identical content outside the Vault is
        # still a physical boundary violation and must never be made current.
        manifest_path = generation_dir / GENERATION_MANIFEST_FILENAME
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise BoundaryViolationError(
                f"Symlink boundary violation: generation member '{manifest_path}' is not a "
                f"regular non-symlink file."
            )
        if manifest_path.read_bytes() != manifest_bytes:
            raise ValueError(
                f"Refusing to publish: existing generation '{generation_dir}' manifest bytes "
                f"differ from the deterministic generation content."
            )
        if markdown_bytes is not None:
            markdown_path = generation_dir / GENERATION_MARKDOWN_FILENAME
            if markdown_path.is_symlink() or not markdown_path.is_file():
                raise BoundaryViolationError(
                    f"Symlink boundary violation: generation member '{markdown_path}' is not a "
                    f"regular non-symlink file."
                )
            if markdown_path.read_bytes() != markdown_bytes:
                raise ValueError(
                    f"Refusing to publish: existing generation '{generation_dir}' markdown bytes "
                    f"differ from the deterministic generation content."
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
            f"- **Audit Manifest**: `{GENERATION_MANIFEST_FILENAME}`",
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
        - ValidationResult trust boundary re-checks status/count/error consistency and
          all validated excerpts BEFORE any filesystem mutation.
        - All output bytes are built before publication begins (Phase 1: prepare).
        - Generation files are fully written and fsynced into a temporary directory,
          which is then atomically renamed to ``generations/<generation-id>/``
          (Phases 2-3: stage + finalize). Published generations are immutable.
        - ``current.json`` is the ONLY commit point (Phase 4: commit): a temp pointer
          file is written, fsynced, and atomically replaced over the published pointer.
          Failure before that replacement leaves the previous generation authoritative
          and fully intact; there is no backup/rollback path.
        - Zero validated excerpts: publishes a manifest-only INCOMPLETE generation
          (no digest.md) and points current.json at it; older generations remain as
          immutable history and are never deleted.
        - Deterministic generation IDs: fixed inputs + fixed generated_at reproduce the
          same generation; an existing identical generation is verified and reused,
          never overwritten.
        - Unknown-file and symlink boundary protections apply to the whole
          publication tree.
        """
        # ===== Phase 1 (part): validate all inputs and trust invariants =====
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

        # Enforce trust boundary BEFORE any filesystem mutation
        self._assert_validation_trust_boundary(validation_result, bundle)

        if generated_at is None:
            generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        artifact_key = f"{request.target_date}_{request.digest_name}"
        artifact_dir = self.digests_dir / artifact_key
        generations_dir = artifact_dir / GENERATIONS_DIRNAME
        current_pointer_path = artifact_dir / CURRENT_POINTER_FILENAME

        # Physical boundary containment for the whole publication tree
        self._require_inside_digests(artifact_dir)
        self._require_inside_digests(generations_dir)
        self._require_inside_digests(current_pointer_path)

        self.digests_dir.mkdir(parents=True, exist_ok=True)

        # Unknown-file protection: refuse to touch foreign paths before mutating anything
        if artifact_dir.exists() or artifact_dir.is_symlink():
            if not artifact_dir.is_dir():
                raise BoundaryViolationError(
                    f"Refusing to publish digest artifact: path '{artifact_dir}' exists and is not a directory."
                )
            if not self._is_managed_artifact_dir(artifact_dir):
                raise BoundaryViolationError(
                    f"Refusing to publish digest artifact: directory '{artifact_dir}' contains "
                    f"unknown/non-digest files."
                )
        if generations_dir.exists() or generations_dir.is_symlink():
            if not generations_dir.is_dir():
                raise BoundaryViolationError(
                    f"Refusing to publish digest artifact: generations path '{generations_dir}' "
                    f"exists and is not a directory."
                )
        if current_pointer_path.exists():
            if not overwrite:
                raise FileExistsError(
                    f"A digest publication already exists at '{current_pointer_path}' and overwrite=False."
                )
            if not self._is_known_current_pointer(current_pointer_path):
                raise BoundaryViolationError(
                    f"Refusing to overwrite unknown/non-digest file at '{current_pointer_path}'."
                )

        # ===== Phase 1: prepare all output bytes and hashes in memory =====
        if validation_result.validated_count == 0:
            # Zero-evidence generation: manifest only, no digest.md
            artifact_status = "INCOMPLETE"
            markdown_bytes: bytes | None = None
            markdown_sha256 = ""
            markdown_rel_path = ""
        else:
            artifact_status = "COMPLETE" if validation_result.is_valid else "INCOMPLETE"
            markdown_content = self.format_markdown(
                request=request,
                bundle=bundle,
                validation_result=validation_result,
                generated_at=generated_at,
                artifact_status=artifact_status,
            )
            markdown_bytes = markdown_content.encode("utf-8")
            markdown_sha256 = hashlib.sha256(markdown_bytes).hexdigest()
            markdown_rel_path = GENERATION_MARKDOWN_FILENAME

        manifest_data = self.format_manifest(
            request=request,
            bundle=bundle,
            validation_result=validation_result,
            markdown_rel_path=markdown_rel_path,
            markdown_sha256=markdown_sha256,
            generated_at=generated_at,
            artifact_status=artifact_status,
        )
        manifest_bytes = (
            json.dumps(manifest_data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

        # Deterministic generation identity: SHA256 over the manifest bytes, which cover
        # request fingerprint, bundle content hash, generated_at, excerpt structural
        # contents, counts, and status/errors. No filesystem state, no circular hashing.
        generation_id = f"gen_{hashlib.sha256(manifest_bytes).hexdigest()}"
        final_generation_dir = generations_dir / generation_id

        pointer_data = {
            "pointer_version": POINTER_VERSION,
            "artifact_key": artifact_key,
            "generation_id": generation_id,
            "artifact_status": artifact_status,
            "content_mode": "VERIFIED_SOURCE_EXCERPTS",
            "manifest": f"{GENERATIONS_DIRNAME}/{generation_id}/{GENERATION_MANIFEST_FILENAME}",
            "markdown": (
                f"{GENERATIONS_DIRNAME}/{generation_id}/{GENERATION_MARKDOWN_FILENAME}"
                if markdown_bytes is not None
                else None
            ),
        }
        pointer_bytes = (
            json.dumps(pointer_data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")

        # ===== Phases 2-3: stage the generation, then finalize by atomic rename =====
        generations_dir.mkdir(parents=True, exist_ok=True)
        tmp_pointer_path: Path | None = None
        if final_generation_dir.exists():
            # Same deterministic generation already published (or orphaned by a crash
            # after finalize): verify byte-identical content and reuse it immutably.
            self._verify_existing_generation(final_generation_dir, markdown_bytes, manifest_bytes)
        else:
            staging_dir = artifact_dir / f".staging_{os.getpid()}_{uuid.uuid4().hex}"
            try:
                staging_dir.mkdir()
                if markdown_bytes is not None:
                    self._write_file_fsynced(staging_dir / GENERATION_MARKDOWN_FILENAME, markdown_bytes)
                self._write_file_fsynced(staging_dir / GENERATION_MANIFEST_FILENAME, manifest_bytes)
                self._fsync_dir(staging_dir)

                os.rename(staging_dir, final_generation_dir)
                self._fsync_dir(generations_dir)
                self._fsync_dir(artifact_dir)
            except BaseException:
                # Best-effort cleanup of OUR unpublished staging directory only.
                # Published generations and the current pointer are never touched.
                shutil.rmtree(staging_dir, ignore_errors=True)
                raise

        # ===== Phase 4: commit — atomically replace current.json (ONLY commit point) =====
        try:
            tmp_pointer_path = artifact_dir / f".{CURRENT_POINTER_FILENAME}.{uuid.uuid4().hex}.tmp"
            self._write_file_fsynced(tmp_pointer_path, pointer_bytes)
            os.replace(tmp_pointer_path, current_pointer_path)
            tmp_pointer_path = None
            self._fsync_dir(artifact_dir)
        except BaseException:
            # The previous pointer (if any) remains authoritative; never touch generations.
            if tmp_pointer_path is not None:
                try:
                    tmp_pointer_path.unlink()
                except OSError:
                    pass
            raise

        return DigestWriterResult(
            artifact_key=artifact_key,
            generation_id=generation_id,
            generation_dir=final_generation_dir,
            markdown_path=(
                final_generation_dir / GENERATION_MARKDOWN_FILENAME
                if markdown_bytes is not None
                else None
            ),
            manifest_path=final_generation_dir / GENERATION_MANIFEST_FILENAME,
            current_pointer_path=current_pointer_path,
            artifact_status=artifact_status,
            content_mode="VERIFIED_SOURCE_EXCERPTS",
            markdown_sha256=markdown_sha256,
            manifest_sha256=manifest_sha256,
            verified_excerpts_count=validation_result.validated_count,
            omitted_excerpts_count=validation_result.failed_count,
        )
