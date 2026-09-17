# Pending Missions

This archive records deliberately deferred work. It is not an implementation
backlog and does not authorize code changes by itself.

## Phase C: Semantic Provenance and Review Workflow

**Status:** DEFERRED until the local digest MVP has been demonstrated with
synthetic fixtures and source-attributed Markdown output.

**Why deferred:** The MVP needs traceability and reviewability, not a
research-grade system for proving semantic entailment or factual truth.

**Deferred materials:**

- `CLAIM_MODEL_DESIGN.md`
- `PHASE_C_CLAIM_BOUNDARY_V2.md`

**Deferred scope:**

- quote spans, offsets, and context-window validation;
- semantic entailment, missing-qualifier, sentiment, and recommendation-
  intensity checks;
- automatic claim-type or topic classification;
- human review state transitions (`DRAFT`, `REVIEWED`, `PUBLISHED`);
- cross-note/cross-digest claim models, provenance graphs, and knowledge graph
  consumption.

**Re-open condition:** A completed MVP has evidence that the local
Vault-to-digest path is useful, and a concrete user need requires one of these
stronger guarantees.
