# Phase C: Claim Boundary & Provenance Validation Specification v2.0

## Status

**DEFERRED PENDING MISSION — Structural provenance design beyond the Phase C
MVP.**

The Phase C MVP scope is defined by `PHASE_C_MVP_SCOPE_REVIEW.md`. This
document is retained for a later, explicitly approved semantic-provenance
phase and does not authorize implementation.

---

## 1. Core Epistemic Distinction: Structural Provenance vs. Semantic Truth

The fundamental vulnerability identified in Claim Model Design v1.0 was **Validator Overpromising**:
claiming that an automated component could verify "factual truth", "semantic intensity", or "attribution correctness".

### Foundational Principles:
1. **Quotes Prove Utterance, Not Reality**:
   - A quote proves that an author *wrote* a particular character sequence in a note. It does **not** prove that the statement is an externally verified fact about the physical world.
   - A source-reported statement labeled `FACT` means: *"The source note asserts this as a fact."* It does **not** mean: *"The system has confirmed this to be true."*
2. **Structural Validation vs. Human Semantic Review**:
   - **`ProvenanceValidator` (Automated)**: Verifies strictly decidable mathematical and textual invariants: byte spans, string identity, SHA256 integrity, bundle membership, and schema conformity.
   - **Human Reviewer (Manual)**: Evaluates semantic faithfuless, context preservation, missing negations/qualifiers, topic classification, and tone.
   - **An automated validator must never certify semantic truth.**
3. **No Automatic Claim Generation in v0.1**:
   - The pure-Python extractor is an **`EvidenceExtractor`**, producing solely **`EvidenceExcerpt`** objects (exact verbatim slices with byte/char spans and SHA256 bindings).
   - The system **never** automatically classifies excerpts into `FACT / OPINION / RECOMMENDATION` or generates synthetic `topic` labels.

```
                    [ EvidenceBundle ] (Sealed input from C.1)
                            │
                            ▼
                  [ EvidenceExtractor ]
                            │
              (Exact Slicing, Zero Interpretation)
                            │
                            ▼
                   [ EvidenceExcerpt ]
                            │
        ┌───────────────────┴───────────────────┐
        ▼                                       ▼
  (v0.1 Default Path)                  (Future Proposer / Human)
  Literal Markdown Excerpts            Draft Synthesis Proposer
  (Direct Quoted Presentation)                  │
                                                ▼
                                         [ DigestClaim ]
                                        (review_state: DRAFT)
                                                │
                                                ▼
                                     [ ProvenanceValidator ]
                                 (Strict Structural Verification)
                                                │
                                                ▼
                                     [ Human Review Gate ]
                               (Semantic Audit & Promotion to REVIEWED)
```

---

## 2. Taxonomy of Objects

### 2.1. `EvidenceExcerpt` (Automated Component Output)
The sole output of automated extraction. Contains zero inference:
- `note_id`: Source note ID.
- `source_file_sha256`: SHA256 hash of the source markdown file.
- `quote_start`: 0-indexed start character offset in `content_text`.
- `quote_end`: 0-indexed end character offset in `content_text`.
- `verbatim_quote`: Exact character sequence in `content_text[quote_start:quote_end]`.
- `location_hint`: Structural section name (e.g. `"content_paragraph_1"`).

### 2.2. `EvidenceReference` (Claim Provenance Link)
Mandatory span binding linking a claim to source evidence:
- `note_id`: Must match a note present in the current `EvidenceBundle`.
- `source_file_sha256`: Must match `SelectedNote.file_sha256`.
- `quote_start`: Exact integer start offset.
- `quote_end`: Exact integer end offset.
- `verbatim_quote`: Must match `SelectedNote.content_text[quote_start:quote_end]`.

### 2.3. `DigestClaim` (Interpretive Synthesis Candidate)
Created only by human author or an explicit external proposer. Starts as `DRAFT`:
- `claim_id`: Unique identifier (e.g. `"c_001"`).
- `note_id`: Single source note ID. **Cross-note synthesis is strictly forbidden in v0.1.**
- `claim_type`: Mandatory enum (`FACT`, `OPINION`, `RECOMMENDATION`, `SUMMARY`).
- `statement`: Candidate sentence or assertion.
- `evidence`: Non-empty list of `EvidenceReference` objects (all pointing to `note_id`).
- `topic`: Optional thematic tag (human-supplied; automated extractor cannot invent topics).
- `review_state`: Mandatory enum: `DRAFT`, `REVIEWED`, `PUBLISHED`. Automated runs emit `DRAFT` only.

---

## 3. HumanReviewState Lifecycle & Promotion Boundary

```
[ Automated Pipeline / Extractor / Proposer ]
                     │
                     ▼
             review_state: DRAFT
                     │
            (ProvenanceValidator)
                     │
               Pass Structural Check?
              ├── NO  ──► OMIT & Log to Manifest Errors
              └── YES ──► Persist as DRAFT in Digest
                               │
                               ▼
                    [ Human Review Gate ]
                               │
                  Human verifies semantics,
                  qualifiers, and tone
                               │
                               ▼
                    review_state: REVIEWED
                               │
                    (Optional user publish)
                               ▼
                    review_state: PUBLISHED
```

- **Inviolable Invariant**: Automated code cannot self-approve or transition a claim from `DRAFT` to `REVIEWED` or `PUBLISHED`.
- A structurally valid `DRAFT` is safe to persist for inspection, but must never be consumed downstream as authoritative knowledge without human approval.

---

## 4. `ProvenanceValidator` Invariants (Pure Structural Gate)

The validator enforces six deterministic, non-semantic invariant rules:

| Rule ID | Rule Name | Enforced Condition | Failure Code |
|---|---|---|---|
| **R-1** | **Bundle Membership** | `claim.note_id` exists in current `EvidenceBundle.notes` | `EVIDENCE_NOTE_NOT_IN_BUNDLE` |
| **R-2** | **Cryptographic Binding** | `ref.source_file_sha256 == target_note.file_sha256` | `SOURCE_HASH_MISMATCH` |
| **R-3** | **Non-Empty Evidence** | `len(claim.evidence) >= 1` | `EMPTY_EVIDENCE` |
| **R-4** | **Span Boundary Check** | `0 <= ref.quote_start < ref.quote_end <= len(target_note.content_text)` | `SPAN_OUT_OF_BOUNDS` |
| **R-5** | **Exact Substring Match** | `target_note.content_text[ref.quote_start:ref.quote_end] == ref.verbatim_quote` | `QUOTE_SPAN_MISMATCH` |
| **R-6** | **Single Note Scope** | All `ref.note_id` in `claim.evidence` equal `claim.note_id` | `CROSS_NOTE_SYNTHESIS_FORBIDDEN` |

### Explicit Exclusions (What `ProvenanceValidator` Does NOT Do):
- ❌ Does **not** judge whether a claim is factual in the physical world.
- ❌ Does **not** perform NLP sentiment analysis or intensity checking.
- ❌ Does **not** verify whether paraphrasing omitted qualifiers or conditions.
- ❌ Does **not** auto-correct or repair invalid spans.

Any failure of Rules R-1 through R-6 causes the claim to be omitted from rendering and logged with its exact error code in `DigestManifest.errors`.

---

## 5. Automated Pipeline Scope Boundaries (v0.1 Frozen Rules)

1. **`EvidenceExtractor`**:
   - May only extract literal, contiguous character spans from `SelectedNote.content_text`.
   - Must calculate 0-indexed character offsets (`quote_start`, `quote_end`).
   - Must bind the exact `file_sha256`.
   - Must not output `DigestClaim`, `topic`, `claim_type`, or synthesized text.
2. **Digest Rendering for v0.1**:
   - In pure rule mode, the digest presents verified `EvidenceExcerpt` objects directly under their note citations:
     ```markdown
     ### [[note_id|Note Title]] — *Author*
     > "Exact extracted quote from note body"
     *(Offset: 120-185 | SHA256: 4b825dc...)*
     ```
   - If draft claims are supplied by a human or proposer, they are rendered under a prominent `## ⚠️ DRAFT Synthesized Claims (Pending Review)` section with full span audit backlinks.
3. **Incomplete Digest Fail-Closed**:
   - If 0 candidate claims/excerpts pass validation, the digest is marked `status: INCOMPLETE`, and no markdown digest is published.
