#!/usr/bin/env python3
"""Phase C synthetic demo runner: thin entrypoint around the existing frozen APIs.

Executes the real pipeline against synthetic demo fixtures only:

    DigestRequest
    -> VaultRetriever.retrieve()
    -> VaultRetriever.assemble_bundle()
    -> EvidenceExtractor.extract()
    -> ProvenanceValidator.validate()
    -> DigestWriter.write()

Properties:
- Read-only towards demo/Vault fixtures: they are staged into a demo-local
  output directory, and every artifact (generation + current.json) is written
  only inside that output directory.
- Fail-closed output safety: the only deletable location is one dedicated run
  directory strictly inside demo/output/. Any --output value that resolves to
  the repository, demo/, demo/Vault/, the whole demo/output base itself, or any
  path outside the base (including symlink-based escapes) is rejected BEFORE
  any deletion happens.
- Zero network, zero browser, zero acquisition, zero LLM/NLP/embeddings.
- Deterministic: fixed target_date/created_at/generated_at reproduce the same
  generation identity and bytes.

Usage:
    python demo/run_phase_c_demo.py                # normal run
    python demo/run_phase_c_demo.py --self-check   # verify unsafe-path rejection
    python demo/run_phase_c_demo.py --output DIR   # DIR must resolve strictly
                                                  # inside demo/output/ and is
                                                  # used as the run directory
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from xhs_knowledge.contracts import DigestRequest, SelectionConfig, SourceConfig  # noqa: E402
from xhs_knowledge.extractor import EvidenceExtractor  # noqa: E402
from xhs_knowledge.retriever import VaultRetriever  # noqa: E402
from xhs_knowledge.validator import ProvenanceValidator  # noqa: E402
from xhs_knowledge.writer import DigestWriter  # noqa: E402

FIXTURE_VAULT = REPO_ROOT / "demo" / "Vault"
DEMO_OUTPUT_BASE = REPO_ROOT / "demo" / "output"
DEFAULT_RUN_DIR_NAME = "phase_c_demo"
COLLECTION = "AI Tools"
TARGET_DATE = "2026-09-18"
FIXED_CREATED_AT = "2026-09-18T00:00:00Z"
FIXED_GENERATED_AT = "2026-09-18T00:00:00Z"


class UnsafeOutputPathError(RuntimeError):
    """Raised when a requested output path is outside the dedicated demo base."""


def _resolve_run_dir(user_output: str | None) -> Path:
    """Resolves the disposable run directory, fail-closed.

    The run directory must resolve strictly inside REPO_ROOT/demo/output/ and
    must not be that base itself. Path.resolve() follows symlinks, so any
    symlink-based escape lands outside the base and is rejected by the same
    containment check. This must be called BEFORE any shutil.rmtree().
    """
    if os.path.islink(DEMO_OUTPUT_BASE):
        raise UnsafeOutputPathError(
            f"demo output base '{DEMO_OUTPUT_BASE}' must not be a symbolic link."
        )

    base = DEMO_OUTPUT_BASE.resolve()
    if not base.is_relative_to(REPO_ROOT):
        raise UnsafeOutputPathError(
            f"demo output base '{base}' escapes the repository root '{REPO_ROOT}'."
        )

    if user_output is None:
        run_dir = base / DEFAULT_RUN_DIR_NAME
    else:
        run_dir = Path(user_output).resolve()

    if run_dir == base:
        raise UnsafeOutputPathError(
            f"Refusing to delete the whole demo output base '{base}'; "
            f"--output must be a strict child such as '{base / DEFAULT_RUN_DIR_NAME}'."
        )
    if not run_dir.is_relative_to(base):
        raise UnsafeOutputPathError(
            f"--output must resolve strictly inside '{base}' (and inside the "
            f"repository); got '{run_dir}'."
        )
    return run_dir


def _self_check() -> int:
    """Demonstrates that dangerous --output values are rejected before deletion."""
    dangerous = [
        "/tmp",
        str(REPO_ROOT),
        str(REPO_ROOT / "demo"),
        str(REPO_ROOT / "demo" / "Vault"),
        str(DEMO_OUTPUT_BASE),  # the whole base itself
        str(REPO_ROOT.parent / "somewhere"),  # ../something
    ]
    failures = 0
    for candidate in dangerous:
        try:
            accepted = _resolve_run_dir(candidate)
        except UnsafeOutputPathError as exc:
            print(f"REJECTED  {candidate}\n          -> {exc}")
            continue
        print(f"ACCEPTED  {candidate} -> {accepted}   (MUST BE REJECTED!)")
        failures += 1

    # Symlink escape: a run-dir candidate literally inside the base whose
    # resolve() lands outside it (target is a private temp dir, never user data).
    import tempfile

    with tempfile.TemporaryDirectory() as outside_tmp:
        DEMO_OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
        escape_link = DEMO_OUTPUT_BASE / f".selfcheck_escape_{os.getpid()}"
        escape_link.unlink(missing_ok=True)
        escape_link.symlink_to(Path(outside_tmp).resolve())
        try:
            try:
                accepted = _resolve_run_dir(str(escape_link))
            except UnsafeOutputPathError as exc:
                print(f"REJECTED  {escape_link} (symlink escape)\n          -> {exc}")
            else:
                print(f"ACCEPTED  {escape_link} -> {accepted}   (MUST BE REJECTED!)")
                failures += 1
        finally:
            escape_link.unlink(missing_ok=True)

    default_dir = _resolve_run_dir(None)
    expected_default = DEMO_OUTPUT_BASE.resolve() / DEFAULT_RUN_DIR_NAME
    if default_dir != expected_default or not default_dir.is_relative_to(DEMO_OUTPUT_BASE.resolve()):
        print(f"BAD DEFAULT: {default_dir}")
        failures += 1
    else:
        print(f"OK        default run dir -> {default_dir}")

    if failures:
        print(f"SELF-CHECK FAILED: {failures} unsafe path(s) accepted.")
        return 1
    print("SELF-CHECK PASSED: all dangerous outputs rejected before deletion.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase C synthetic digest demo")
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "run directory; must resolve strictly inside "
            f"{DEMO_OUTPUT_BASE} (default: {DEMO_OUTPUT_BASE / DEFAULT_RUN_DIR_NAME})"
        ),
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="verify unsafe --output paths are rejected, then exit",
    )
    args = parser.parse_args()

    if args.self_check:
        return _self_check()

    # Fail-closed path resolution BEFORE any deletion.
    try:
        run_dir = _resolve_run_dir(args.output)
    except UnsafeOutputPathError as exc:
        print(f"REFUSING UNSAFE OUTPUT PATH: {exc}", file=sys.stderr)
        return 2

    # Stage a fresh copy of the synthetic fixtures so the demo never writes into
    # demo/Vault, and only the dedicated run directory is ever disposable.
    staged_vault = run_dir / "vault"
    data_dir = run_dir / "data"
    state_dir = run_dir / "state"
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(run_dir, ignore_errors=True)
    shutil.copytree(FIXTURE_VAULT, staged_vault)
    data_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    # 1. Request
    request = DigestRequest(
        digest_name="phase_c_demo",
        target_date=TARGET_DATE,
        source=SourceConfig(collections=[COLLECTION]),
        selection=SelectionConfig(max_notes=5),
    )

    # 2. Retrieval + bundle assembly (frozen APIs, offline, read-only on staged vault)
    retriever = VaultRetriever(vault_dir=staged_vault, data_dir=data_dir, state_dir=state_dir)
    notes = retriever.retrieve(request)
    bundle = retriever.assemble_bundle(request, notes, created_at=FIXED_CREATED_AT)

    # 3. Mechanical verbatim extraction (no semantics)
    excerpts = EvidenceExtractor(max_excerpts_per_note=2).extract(bundle)

    # 4. Structural provenance validation
    validation = ProvenanceValidator().validate(excerpts, bundle)

    # 5. Crash-safe publication (immutable generation + atomic current pointer)
    result = DigestWriter(vault_dir=staged_vault).write(
        request, bundle, validation, generated_at=FIXED_GENERATED_AT
    )

    pointer = json.loads(result.current_pointer_path.read_text(encoding="utf-8"))

    print("=== Phase C synthetic demo ===")
    print(f"collection:        {COLLECTION}")
    print(f"selected_notes:    {len(notes)}")
    print(f"excerpts:          {len(excerpts)}")
    print(f"validation_status: {validation.status}")
    print(f"artifact_status:   {result.artifact_status}")
    print(f"artifact_key:      {result.artifact_key}")
    print(f"generation_id:     {result.generation_id}")
    print(f"current.json:      {result.current_pointer_path}")
    print(f"digest.md:         {result.markdown_path}")
    print(f"manifest.json:     {result.manifest_path}")
    print(f"pointer_markdown:  {pointer['markdown']}")

    if result.artifact_status != "COMPLETE" or validation.status != "PASS":
        print("DEMO FAILED: expected COMPLETE artifact from PASS validation.")
        return 1
    print("DEMO OK: COMPLETE artifact published.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
