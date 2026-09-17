# Phase C.2 Implementation Plan v1.0
## (Narrow Local Digest MVP: EvidenceExtractor, ProvenanceValidator, DigestWriter)

## Status

**FROZEN IMPLEMENTATION PLAN FOR CODEX AUDIT — Implementation prohibited until approved.**
Governed by: `docs/PHASE_C_MVP_SCOPE_REVIEW.md` and `docs/PENDING_MISSIONS.md`.

---

## 1. Architectural Guardrails & Frozen Scope

The purpose of the Phase C.2 MVP is to demonstrate a reviewable, traceable chain from local Vault notes to a source-attributed Markdown digest. It does not certify that generated prose is semantically true or complete.

```
Vault
  │
  ▼
[ VaultRetriever ] (Phase C.1 - Verified)
  │
  ▼
[ EvidenceBundle ] (Sealed Input to Phase C.2)
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
- ❌ **No LLM / Agent**: Zero Ollama, zero OpenAI/Claude/Gemini APIs, zero agent loops.
- ❌ **No Vector DBs / Embeddings**: Zero embeddings, zero semantic similarity search.
- ❌ **No NLP / Semantic Analysis**: Zero sentiment analysis, zero importance ranking, zero keyword classification, zero topic extraction.
- ❌ **No Network or Ingestion Imports**: Zero imports from `xhs_ingest`, `collector`, `sync`, `browser`, `requests`. Zero external network calls.
- ❌ **Deferred Missions**: Spans/offsets, semantic entailment checks, intensity lints, and claim workflows are archived in `docs/PENDING_MISSIONS.md`.

---

## 2. Answers to Core Architecture Questions

### 2.1. What is the Input?
- **Input**: A sealed `EvidenceBundle` containing `SelectedNote[]` produced by `VaultRetriever`.
- **Physical Boundary**: Components in C.2 never scan the filesystem, never query `Vault/` directly, and never traverse directories. They accept solely the immutable in-memory `EvidenceBundle`.
- **Target Collection Rule**: In accordance with `docs/PHASE_C_MVP_SCOPE_REVIEW.md`, each digest request targets exactly one collection (`len(collections) == 1`).

---

### 2.2. What does `EvidenceExtractor` Output?
- **Sole Output**: `list[EvidenceExcerpt]`.
- **Data Structure**:
  ```python
  @dataclass
  class EvidenceExcerpt:
      note_id: str
      source_file_sha256: str
      verbatim_quote: str
  ```
- **Deterministic Extraction Logic (Zero NLP)**:
  - Takes `SelectedNote.content_text`.
  - Splits text strictly by structural newline boundaries (`\n\n` or `\n`).
  - Slices non-empty paragraphs/sentences using literal string operations.
  - Binds `note_id` and the immutable `SelectedNote.file_sha256`.
- **Forbidden in Output**:
  - ❌ No `summary`
  - ❌ No `topic`
  - ❌ No `claim_type`
  - ❌ No `importance` or `score`
  - ❌ No rephrasing or modification of text characters.

---

### 2.3. What does `ProvenanceValidator` Do?
- **Role**: Pure structural provenance verification of decidable facts.
- **Invariants Checked**:
  1. `note_id` exists in the active `EvidenceBundle`.
  2. `source_file_sha256` equals that selected note's `file_sha256`.
  3. `verbatim_quote` is an exact substring of that selected note's `content_text`.
- **Failure Behavior**:
  - Failure omits the reference and logs the omission in the manifest.
  - It must not invent a new source, retry retrieval, or render an uncited replacement.
- **Explicit Non-Goals (What it NEVER checks)**:
  - ❌ Does **not** judge whether a statement is factually true in the external world.
  - ❌ Does **not** evaluate semantic entailment or completeness.
  - ❌ Does **not** evaluate recommendation intensity or tone.

---

### 2.4. What does `DigestWriter` Do?
- **Role**: Atomic serialization of verified excerpts and audit manifest into `Vault/digests/`.
- **Inputs**:
  - `request: DigestRequest`
  - `bundle: EvidenceBundle`
  - `verified_excerpts: list[EvidenceExcerpt]`
- **Outputs**:
  1. `Vault/digests/<YYYY-MM-DD>_<digest_name>.md`
  2. `Vault/digests/<YYYY-MM-DD>_<digest_name>.manifest.json`
- **Output Markdown Structure**:
  ```markdown
  ---
  digest_name: "ai_daily"
  target_date: "2026-09-17"
  source_collection: "coding"
  generated_at: "2026-09-17T15:30:00Z"
  notes_referenced: 2
  excerpts_count: 3
  status: "COMPLETE"
  ---

  # Knowledge Digest: ai_daily (2026-09-17)

  - **Source Collection**: `coding`
  - **Generated At**: 2026-09-17T15:30:00Z

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
- **Boundary & Safety Rules**:
  - Writes strictly inside `Vault/digests/`; rejects path traversal or symlinks with `BoundaryViolationError`.
  - Atomic writing via `.tmp` files.
  - If 0 excerpts pass validation: writes 0 bytes of markdown, records `INCOMPLETE` in manifest.

---

## 3. Manifest Specification

`Vault/digests/<YYYY-MM-DD>_<digest_name>.manifest.json`:
```json
{
  "manifest_version": "1.0",
  "digest_name": "ai_daily",
  "target_date": "2026-09-17",
  "source_collection": "coding",
  "generated_at": "2026-09-17T15:30:00Z",
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
  "status": "COMPLETE",
  "errors": []
}
```

---

## 4. Implementation Steps & Code Order

When approved:
1. **`src/xhs_knowledge/extractor.py`**:
   - `EvidenceExtractor(max_excerpts_per_note: int = 3, min_length: int = 10)`.
   - Literal paragraph extraction; zero NLP.
2. **`src/xhs_knowledge/validator.py`**:
   - `ProvenanceValidator`.
   - Checks `note_id in bundle`, `sha256 == note.file_sha256`, and `verbatim_quote in note.content_text`.
3. **`src/xhs_knowledge/writer.py`**:
   - `DigestWriter(vault_dir: Path)`.
   - Writes atomic `.md` and `.manifest.json` under `Vault/digests/`.
4. **`tests/test_digest_pipeline.py`**:
   - Exact quote substring test (PASS).
   - Tampered quote test (FAIL / omitted).
   - Foreign note test (FAIL / omitted).
   - Hash mismatch test (FAIL / omitted).
   - Boundary guard test (prevent write outside `Vault/digests/`).
   - End-to-end integration test:
     `Vault` ➔ `VaultRetriever` ➔ `EvidenceBundle` ➔ `EvidenceExtractor` ➔ `ProvenanceValidator` ➔ `DigestWriter` ➔ Verify Markdown, manifest, and byte-reproducibility across 10 runs.
