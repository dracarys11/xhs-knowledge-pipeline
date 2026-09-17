# Phase C MVP Scope Review

## Decision

**Proceed with a narrow local digest MVP.** Its purpose is to demonstrate a
reviewable chain from a local Vault to a source-attributed Markdown digest. It
does not certify that generated prose is semantically true, complete, or
faithful in every respect.

```text
Vault
  -> VaultRetriever
  -> EvidenceBundle
  -> EvidenceExtractor
  -> optional DigestGenerator
  -> structural reference check
  -> digest.md
```

The MVP is offline-only: no browser, Collector, P1 storage, remote platform,
or external retrieval access.

## P0: Required for MVP Correctness

### Minimal reference contract

Every generated passage that claims a source must carry:

```yaml
EvidenceReference:
  note_id: string
  source_file_sha256: string
  verbatim_quote: string
```

Before rendering, the MVP validates only these decidable facts:

1. `note_id` exists in the active `EvidenceBundle`.
2. `source_file_sha256` equals that selected note's hash.
3. `verbatim_quote` is an exact substring of that selected note's content.

Failure omits the passage and records the reason. It must not invent a new
source, retry retrieval, or render an uncited replacement.

### Deliberate scope limits

- One digest request targets exactly one collection.
- The digest uses `source_collection`, equal to the selected collection name.
  It is a user-managed navigation/category entry, not an inferred semantic
  topic.
- A generator may summarize excerpts from that one collection, but the output
  is explicitly source-attributed and reviewable rather than semantically
  certified.

## P1: Useful, but Not a Gate

- Retain `FACT`, `OPINION`, `RECOMMENDATION`, and `SUMMARY` as
  **source-attributed presentation labels**. Do not claim automatic truth
  verification.
- Preserve a narrow synthesizer interface so a mock or local implementation can
  be substituted later; do not build a provider framework.
- Include the evidence bundle ID, selected note IDs, source hashes, and omitted
  reference count in the digest manifest.

## Deferred Pending Missions

The following are intentionally outside the MVP and are recorded in
`docs/PENDING_MISSIONS.md`:

- semantic entailment, qualifier, sentiment, and intensity validation;
- span offsets, context windows, and quote-level provenance graphs;
- automated claim-type assignment or topic inference;
- `DRAFT -> REVIEWED -> PUBLISHED` workflow and human-review product flow;
- cross-digest analysis, a claims database, knowledge graph, and agent layer.

## Acceptance Evidence

The MVP is accepted when a synthetic Vault can deterministically produce a
digest that contains only references whose note ID, source hash, and quoted
text are present in the selected bundle. A reviewer must be able to open each
referenced Vault note from the digest.

The minimal rendered grouping is therefore:

```yaml
source_collection: <selected collection name>
note_id: <selected source note>
title: <source note title>
quote: <verbatim excerpt>
```

No field named `topic` is produced by the MVP pipeline.

## Authority

This document is the Phase C MVP scope profile. Where it conflicts with the
broader semantic-validation ambitions in `CLAIM_MODEL_DESIGN.md` or
`PHASE_C_CLAIM_BOUNDARY_V2.md`, this MVP profile governs implementation scope.
