# Phase C.2 Implementation Plan v2.0
## (Narrow Local Digest MVP: EvidenceExtractor, ProvenanceValidator, DigestWriter)

## Status

**FROZEN IMPLEMENTATION PLAN FOR CODEX AUDIT — Implementation prohibited until approved.**
Governed by: `docs/PHASE_C_MVP_SCOPE_REVIEW.md` and `docs/PENDING_MISSIONS.md`.

---

## 1. Architectural Guardrails & Frozen Scope

The sole purpose of the Phase C.2 MVP is to establish an auditable, mechanical pipeline from local Vault notes to a source-attributed Markdown digest artifact.

```
Vault
  │
  ▼
[ VaultRetriever ] (Phase C.1 - Verified)
  │
  ▼
[ EvidenceBundle ] (Sealed In-Memory Input)
  │
  ├─► [ EvidenceExtractor ] ──► list[EvidenceExcerpt]
  │                                    │
  └─► [ ProvenanceValidator ] ◄────────┘
            │
            ▼
     [ DigestWriter ]
            │
            ▼
    Vault/digests/<date>_<name>.md  +  manifest.json
```

### Strictly Forbidden in Phase C.2 MVP:
- ❌ **No LLM / Agent**: Zero Ollama, zero OpenAI/Claude/Gemini API calls, zero agent loops.
- ❌ **No Vector DBs / Embeddings**: Zero semantic embeddings, zero similarity search.
- ❌ **No NLP / Semantic Analysis**: Zero sentiment analysis, zero importance ranking, zero keyword classification, zero topic extraction.
- ❌ **No Generative Text**: Zero free-form summarization, zero recommendation synthesis.
- ❌ **No Network or Ingestion Imports**: Zero imports from `xhs_ingest`, `collector`, `sync`, `browser`, `requests`. Zero external network calls.
- ❌ **Deferred Missions**: Offsets/span coordinates, semantic entailment checks, intensity lints, and claim workflows are archived in `docs/PENDING_MISSIONS.md`.

---

## 2. Four Architecture Invariants (Auditable Answers)

### 2.1. What is the Input?
- **Strict Input**: A sealed `EvidenceBundle` produced by `VaultRetriever` (from Phase C.1).
- **Physical Boundary**: Components in C.2 never scan the filesystem, never query `Vault/` directly, and never traverse directories. They accept solely the immutable in-memory `EvidenceBundle`.
- **Single-Collection Invariant (P0-3)**:
  - Each `DigestRequest` must target **exactly one collection**: `len(request.source.collections) == 1`.
  - Enforced at request validation:
    ```python
    if len(self.source.collections) != 1:
        raise ValueError(
            f"Phase C.2 MVP strictly requires exactly one collection, got {len(self.source.collections)}"
        )
    ```

---

### 2.2. What does `EvidenceExtractor` Output? (P0-1 & P0-2)
- **Sole Output**: `list[EvidenceExcerpt]`.
- **Minimal Reference Schema (P0-1 Unified)**:
  ```python
  @dataclass
  class EvidenceExcerpt:
      note_id: str
      source_file_sha256: str
      verbatim_quote: str
  ```
  *(Character spans `quote_start` / `quote_end` are deliberately deferred to prevent schema drift).*

- **Purely Mechanical Extraction Algorithm (P0-2 Anti-Heuristic)**:
  - Takes `SelectedNote.content_text`.
  - Splits text strictly by natural paragraph delimiters: `content_text.split("\n\n")`.
  - Fallback to single newline `content_text.split("\n")` only if no `\n\n` exists.
  - Strips whitespace; discards empty paragraphs.
  - Slices the first `N` non-empty paragraphs (`max_excerpts_per_note`, default: 3).
  - Binds `note_id` and the note's immutable `file_sha256`.
  - **No `min_length` threshold**: Avoids implicit editorial judgment of what is "important".
  - **No ranking, scoring, or keywords**: Purely deterministic mechanical extraction.

- **Forbidden in Output**:
  - ❌ No `summary`
  - ❌ No `topic`
  - ❌ No `claim_type`
  - ❌ No `importance` or `score`
  - ❌ No character modifications or paraphrasing.

---

### 2.3. What does `ProvenanceValidator` Do?
- **Role**: Pure structural provenance verification of decidable facts.
- **Invariants Checked**:
  1. `excerpt.note_id` exists in the active `EvidenceBundle`.
  2. `excerpt.source_file_sha256` equals that selected note's `file_sha256`.
  3. `excerpt.verbatim_quote` is an exact, literal substring of that selected note's `content_text`:
     `assert excerpt.verbatim_quote in note.content_text`.
- **Failure Behavior**:
  - Any reference failing any check is omitted and recorded in `DigestManifest.errors`.
  - It must not invent a new source, retry retrieval, or substitute unverified text.
- **Explicit Non-Goals (What it NEVER checks)**:
  - ❌ Does **not** judge whether a statement is factually true in the external world.
  - ❌ Does **not** evaluate semantic entailment or completeness.
  - ❌ Does **not** evaluate recommendation intensity or tone.

---

### 2.4. What does `DigestWriter` Do? (P0-4)
- **Role**: Atomic serialization of verified excerpts and audit manifest into `Vault/digests/`.
- **Inputs**:
  - `request: DigestRequest`
  - `bundle: EvidenceBundle`
  - `verified_excerpts: list[EvidenceExcerpt]`
- **Outputs**:
  1. `Vault/digests/<YYYY-MM-DD>_<digest_name>.md`
  2. `Vault/digests/<YYYY-MM-DD>_<digest_name>.manifest.json`
- **Status Naming (P0-4 Clarification)**:
  - Frontmatter and Manifest explicitly distinguish between execution success and content mode:
    - `artifact_status: "COMPLETE"` (or `"INCOMPLETE"`)
    - `content_mode: "VERIFIED_SOURCE_EXCERPTS"`
  - Eliminates the ambiguity that a "COMPLETE" status implies an AI summary.
- **Fail-Closed on Empty Evidence**:
  - If 0 excerpts pass validation (e.g. all notes have empty content):
    - **Zero bytes written** to the `.md` file.
    - Manifest is written with `artifact_status: "INCOMPLETE"` and `verified_excerpts_count: 0`.
- **Boundary Isolation**:
  - Output paths must resolve strictly within `Vault/digests/`. Any escape raises `BoundaryViolationError`.
  - Atomic writing via temporary files (`.tmp`) before atomic replace.

---

## 3. Markdown Output Format Specification

Target: `Vault/digests/<YYYY-MM-DD>_<digest_name>.md`

```markdown
---
digest_name: "ai_daily"
target_date: "2026-09-17"
source_collection: "coding"
generated_at: "2026-09-18T00:10:00Z"
notes_referenced: 2
excerpts_count: 3
artifact_status: "COMPLETE"
content_mode: "VERIFIED_SOURCE_EXCERPTS"
---

# Knowledge Digest: ai_daily (2026-09-17)

- **Source Collection**: `coding`
- **Content Mode**: Verified Source Excerpts (Non-interpretive)
- **Generated At**: 2026-09-18T00:10:00Z

## 📌 Verified Source Excerpts

### [[6aa62ab4000000001001fc4f|一个月 20 刀的 Antigravity CLI，可能被很多人低估了]] — *艾康的AI自留地*
- > "一个月 20 刀的 Antigravity CLI，可能被很多人低估了"
- > "谷歌 Gemini CLI 6月18日停止个人账号支持，推荐Antigravity CLI替代..."

### [[6a97a054000000002802f761|Agent面试连环追问，你能坚持到第几关]] — *J同学qej*
- > "面试官连环追问核心在于评测集的代表性与可复现性"

---

## 📊 Digest Provenance & Audit
- **Referenced Notes**: 2
- **Evidence Verification**: 100% verified against local vault artifacts
- **Audit Manifest**: `digests/2026-09-17_ai_daily.manifest.json`
```

---

## 4. Manifest Specification (`<date>_<name>.manifest.json`)

```json
{
  "manifest_version": "1.0",
  "digest_name": "ai_daily",
  "target_date": "2026-09-17",
  "source_collection": "coding",
  "generated_at": "2026-09-18T00:10:00Z",
  "bundle_id": "bundle_20260917_ai_daily",
  "request_fingerprint": "...",
  "bundle_content_hash": "...",
  "inputs": [
    {
      "note_id": "6aa62ab4000000001001fc4f",
      "title": "一个月 20 刀的 Antigravity CLI，可能被很多人低估了",
      "sha256": "b2e63dc739c8..."
    }
  ],
  "candidate_excerpts_count": 3,
  "verified_excerpts_count": 3,
  "omitted_excerpts_count": 0,
  "output_file": "digests/2026-09-17_ai_daily.md",
  "output_sha256": "...",
  "artifact_status": "COMPLETE",
  "content_mode": "VERIFIED_SOURCE_EXCERPTS",
  "errors": []
}
```

---

## 5. Test Matrix Specifications (P1)

1. **Single-Collection Invariant Test**:
   - `collections = []` ➔ raises `ValueError`.
   - `collections = ["coding", "吃"]` ➔ raises `ValueError`.
   - `collections = ["coding"]` ➔ succeeds.
2. **Mechanical Extractor Test**:
   - Verify splitting strictly on `\n\n`.
   - Verify no filtering of short sentences (no `min_length` discrimination).
   - Verify non-empty paragraphs up to `max_excerpts_per_note` are taken in literal order.
3. **Unicode & Emoji Integrity Test**:
   - Notes containing Chinese text, special punctuation, and emojis (e.g. `✨🍜🍣`) are parsed without splitting or garbling characters.
4. **Empty Evidence Fail-Closed Test**:
   - Notes with empty content produce 0 excerpts.
   - DigestWriter writes 0 bytes of markdown (file does not exist or is not created).
   - Manifest records `artifact_status: "INCOMPLETE"`, `verified_excerpts_count: 0`.
5. **Exact Substring Validation Test**:
   - Valid quote `assert quote in note.content_text` ➔ PASS.
   - Tampered quote (1 char modified) ➔ FAIL (`QuoteSubstringMismatchError`) and omitted from output.
   - Note ID not in bundle ➔ FAIL (`EvidenceNoteNotInBundleError`).
   - File SHA256 mismatch ➔ FAIL (`SourceHashMismatchError`).
6. **Reproducibility Test (P1 Option B)**:
   - Repeated runs compare `bundle_content_hash`, `verified_excerpts` hashes, and markdown content excluding `generated_at` line, proving 100% deterministic reproducibility.
7. **End-to-End Cryptographic Zero-Mutation Test**:
   - Compute `sync.db` SHA256 and `data/` tree hash before the complete pipeline:
     `VaultRetriever ➔ EvidenceExtractor ➔ ProvenanceValidator ➔ DigestWriter`.
   - Compute hashes after execution.
   - Assert `before_db == after_db` and `before_data == after_data`.

---

## 6. Implementation Sequence

Implementation must proceed module by module with independent verification:

```
contracts.py (align EvidenceExcerpt & single-collection check)
     │
     ▼  [Codex Review / Self-Check]
extractor.py (mechanical paragraph chunking)
     │
     ▼  [Codex Review / Self-Check]
validator.py (decidable structural checks)
     │
     ▼  [Codex Review / Self-Check]
writer.py (atomic serialization & boundary isolation)
     │
     ▼  [Codex Review / Self-Check]
test_digest_pipeline.py (full test matrix)
```
