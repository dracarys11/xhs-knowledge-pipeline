# Personal Knowledge Export Pipeline

> An evidence-first data pipeline that turns saved content from dynamic platforms into local, traceable, and AI-ready personal knowledge vaults.

---

## 1. Problem

Modern platforms store valuable personal knowledge, but saved content is fragmented, locked behind dynamic proprietary interfaces, and difficult to reuse.

This project explores a fundamental question in personal data infrastructure:

**How can personal platform data become:**
- **Portable**: Exported into open Markdown and locally stored assets for tools like Obsidian.
- **Traceable**: Grounded in immutable cryptographic proof of remote server responses.
- **Locally Searchable**: Structured by collections, permanent primary keys, and human-readable aliases.
- **Safe to Process with AI**: Sanitized of tracking tokens and session credentials before offline LLM consumption.

---

## 2. Architecture

```
                       User-owned Data
                              │
                              ▼
                     Evidence Acquisition
                    (Stateful, Fail-Closed)
                              │
                              ▼
                        Evidence Store
                    (Immutable Fixtures)
                              │
                              ▼
                       Canonical Model
                   (Clean, Platform-Agnostic)
                              │
                              ▼
                     Knowledge Projection
                    (Hygiene Gate & Indexes)
                              │
                              ▼
                         Local Vault
                     (Obsidian Markdown)
                              │
                              ▼
                   AI-assisted Consumption
                    (Offline Reasoning & RAG)
```

> **Design Boundary**: The acquisition layer and consumption layer are strictly decoupled. The AI Agent never browses or scrapes remote platforms directly; it operates 100% offline over validated local vaults.

---

## 3. Reliability Design: Evidence-First Architecture

Most data exporters only prove that a script ran. This pipeline focuses on proving what data was acquired, how it was transformed, and whether the resulting knowledge base is trustworthy.

### Case Study: Refusing to Fabricate Facts

During real-world collection verification against Xiaohongshu:

```text
Reported Count: 43 notes
Observed Count: 41 notes
Delta:          2 notes
Decision:       UNKNOWN
```

**Why this matters:**
- Naive scrapers often report `"43 imported"` (ignoring missing items) or fabricate explanations like `"2 notes were deleted by authors"`.
- In our system: `missing != deleted`, `missing != private`, `missing != platform_filtered`.
- The pipeline acts as a **data quality gate**: it explicitly records the discrepancy as `UNKNOWN` rather than inventing unevidenced facts.

### Core Guarantees

1. **Evidence Hygiene Gate**: All URLs entering the Obsidian Vault are stripped of platform security signatures (`xsec_token`, `xsec_source`), referral tags (`utm_*`, `spm`), and session parameters, normalizing to bare canonical URLs (`https://www.xiaohongshu.com/explore/<note_id>`).
2. **Zero-Mutation Isolation Guard**: The projection layer treats underlying P1 storage (`data/` and `.xhs-state/sync.db`) as strictly read-only. Pre/post SHA256 checksums verify **0 bytes modified** during indexing.
3. **Database-Style Naming (Primary Key + Alias)**:
   - Notes are stored by immutable ID: `notes/<note_id>.md`.
   - Titles change, contain emojis, or collide; `note_id` never does.
   - Obsidian frontmatter injects `aliases: ["<Title>"]` and collection documents use `[[<note_id>|<Title>]]`, delivering a 100% human-readable reading experience with zero file-rename churn.

---

## 4. Synthetic Demo (Offline & Reproducible)

The repository provides synthetic, token-free fixtures under `demo/`. Anyone can inspect and run the complete pipeline offline without an account, browser session, or network connection:

### Running the Demo

```bash
# 1. Run test suite
pytest

# 2. Run the offline collection indexer over synthetic fixtures
PYTHONPATH=src python -m xhs_knowledge \
  --evidence-dir demo/evidence/collections \
  --data-dir demo/data \
  --vault-dir demo/Vault
```

### Execution Output

```text
=== Obsidian Collection Indexer Summary ===
  Target Collections Dir:   demo/Vault/collections
  Collections Indexed:      2
  Total Notes Referenced:   5
  Notes in Vault:           5
  Notes Pending Export:     0
  - Output: demo/Vault/collections/AI Tools.md
  - Output: demo/Vault/collections/Restaurants.md
  - Output: demo/Vault/README.md
```

### Obsidian Vault Structure

```text
My Knowledge Base (demo/Vault/)
├── README.md                      # Knowledge Base Dashboard & Stats
├── collections/                   # Topic Index Documents
│   ├── AI Tools.md
│   │   ├── [[demo_note_001|Building Agent Systems]] — *demo_user*
│   │   ├── [[demo_note_002|Reliable LLM Evaluation]] — *demo_user*
│   │   └── [[demo_note_003|Retrieval Design Notes]] — *demo_user*
│   └── Restaurants.md
│       ├── [[demo_note_004|Neighborhood Ramen]] — *demo_user*
│       └── [[demo_note_005|Local Coffee Guide]] — *demo_user*
└── notes/                         # Note Content Layer
    ├── demo_note_001.md
    └── ... (5 notes with frontmatter & aliases)
```

---

## 5. Testing & Verification

```bash
pytest
```

```text
============================= 154 passed in 22.96s =============================
```

Rather than raw test counts, the test suite emphasizes verification across critical failure boundaries:
- **Schema Validation**: Missing fields, corrupted media metadata, and type invariants.
- **Boundary Checks**: Path traversal prevention and strict directory containment.
- **URL Sanitization**: Ensuring tracking tokens and auth cookies never reach exported notes.
- **Projection Determinism**: Idempotent generation, stable wikilinks, and sorted manifests.
- **Offline Fixture Execution**: Deterministic replayability across environments.

---

## 6. Engineering Decisions & Interview Notes

### Q1: Why not just crawl everything with a generic scraper?
> **Answer**: Because acquisition volume is not the bottleneck—trustworthiness is. Generic scrapers fail silently when rate-limited or challenged by dynamic anti-bot systems, leaving corrupted partial state. We architected the pipeline around explicit protocol evidence, atomic transitions, and fail-closed state machines so that every exported artifact is provably complete and reproducible.

### Q2: Why separate the Evidence Store from Canonical Data?
> **Answer**: Raw network observations contain unstable platform artifacts, session cookies, and dynamic schema shifts. The Evidence Store preserves verbatim proof of what the server returned, while Canonical Data provides a clean, platform-agnostic internal model. If normalization rules evolve, canonical data can be re-derived without re-querying the platform.

### Q3: Why not let an LLM agent control browser exploration directly?
> **Answer**: Because reasoning and data acquisition have entirely different failure modes. An autonomous agent can hallucinate decisions, loop unpredictably, or trigger platform anti-scraping risk controls. Deterministic protocols belong in a rigid, typed acquisition engine; LLMs belong downstream in the consumption layer where they synthesize and reason over sealed local data.

### Q4: Why separate personal data from the public repository?
> **Answer**: Production data contains personal favorites, private reading lists, and platform IDs that should never be published. We established a strict boundary: production data remains local-only (ignored by Git), while the public repository uses synthetic fixtures to demonstrate pipeline behavior deterministically.

---

## 7. Roadmap

- [x] **Phase A**: Synthetic demo fixtures & offline collection projection.
- [x] **Phase B**: Evidence-first documentation, demo story, and v0.3 public milestone.
- [ ] **Phase C**: Offline Agent prototype (daily knowledge synthesis and backlinks over local Vault).
- [ ] **Phase D**: Multi-source connector abstractions (GitHub Stars, Reddit Saved, RSS).

---

## License
MIT License. Strictly for personal knowledge archival and educational research.
