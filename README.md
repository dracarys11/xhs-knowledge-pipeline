# Evidence-First Personal Knowledge Pipeline

This project turns data from a user's own, authorized account into local,
traceable knowledge artifacts. It prioritizes correctness, provenance, and
user-controlled data portability over collection volume.

```text
User-owned account data
  -> evidence acquisition
  -> canonical local artifacts
  -> knowledge projection
  -> Obsidian Vault
```

It is not a public-data collection system or dataset-generation project. See
[Operational Safety](docs/OPERATIONAL_SAFETY.md) and the frozen
[Acquisition Contract](docs/ACQUISITION_CONTRACT_V1.md).

## Validated Milestone

- P1: stateful local acquisition, artifact integrity, recovery, and explicit
  failure boundaries.
- P2.1: read-only projection of verified local artifacts into Markdown and
  local media references.
- P2.2: collection relationship evidence can be projected into Obsidian
  collection indexes from sealed, sanitized fixtures.

One local validation run produced 137 complete note artifacts and 820 media
files. Those personal artifacts are deliberately not in this repository.
Remote account coverage remains **UNPROVEN**.

## Architecture

```text
Human-approved, fixed-scope observation
  -> redacted evidence fixture
  -> offline parser / canonical model
  -> Obsidian note and collection projection
  -> offline knowledge use
```

The fixture is the boundary between real-account activity and normal
development. Offline processing never opens a browser, triggers a sync, or
repairs source artifacts.

## Quick Demo

The repository includes synthetic, token-free fixtures under `demo/`. No
account, browser profile, or network access is required.

```bash
PYTHONPATH=src python -m xhs_knowledge \
  --evidence-dir demo/evidence/collections \
  --data-dir demo/data \
  --vault-dir /tmp/xhs-demo-vault
```

This creates a collection index under `/tmp/xhs-demo-vault/collections/`.
The expected combined P2.1 + P2.2 Vault shape is in `demo/expected_vault/`.

## Repository Boundaries

- `data/`, `.xhs-state/`, `.xhs-profile/`, `Vault/`, and `knowledge/` are
  local user data and ignored by Git.
- `demo/` contains synthetic fixtures only.
- Persistent knowledge artifacts must not include session material,
  token-bearing URLs, signed URLs, or private request parameters.
- Any real-account run requires a human-approved Purpose, Scope, Evidence,
  Stop Condition, Mutation Boundary, and Expansion Risk declaration.

## Development

```bash
pytest
```

The test suite uses local fixtures and does not require a real account.

## Status

The v0.2 milestone freezes the evidence-based acquisition, canonical-model,
and Obsidian-projection architecture for presentation and review. P2.2 Member
Import remains blocked on Gate 0 in `docs/ACQUISITION_CONTRACT_V1.md`.

## Key Documents

- [Architecture](docs/ARCHITECTURE.md)
- [Operational Safety](docs/OPERATIONAL_SAFETY.md)
- [Acquisition Contract v1](docs/ACQUISITION_CONTRACT_V1.md)
- [P2 Status](docs/P2_STATUS.md)
- [Decisions](docs/DECISIONS.md)
- [Changelog](docs/CHANGELOG.md)
