# Phase C: Offline Knowledge Synthesis Contract v1.0
## (Vault Digest Pipeline v0.1)

## Status

**FROZEN DESIGN CONTRACT — implementation may begin only within the v0.1
scope and boundaries below.**

## 1. Purpose & Design Philosophy

This contract defines the engineering boundaries, type schemas, attribution constraints, and artifact verification rules for **Phase C (Offline Knowledge Synthesis)**.

### Core Principles:
1. **Constraining Autonomy over Marketing Hype**:
   - This layer is not an unconstrained "autonomous AI agent" running arbitrary loops. It is a **deterministic synthesis pipeline** that operates strictly over verified local artifacts.
2. **Backlink ≠ Provenance (Preventing "Citation Laundering")**:
   - A superficial markdown link like `[[note_id]]` does not prove provenance. A hallucinating model can invent a claim from whole cloth and arbitrarily append a real link to "look cited".
   - This contract requires an intermediate **Evidence-Bound Claim Model**: every factual assertion must be structurally linked to a verbatim quote or verified excerpt from the source note before rendering markdown.
3. **100% Offline Boundary**:
   - The pipeline operates exclusively on local files within `Vault/`.
   - Under no circumstances does this layer trigger network requests, invoke scrapers, launch browsers, or contact remote platforms.
   - The default pure-Python component is an **Evidence Extractor**. It selects
     exact candidate excerpts; it does not infer, generalize, or present
     candidates as facts.
   - An optional local-only LLM may propose structured claims from an already
     sealed bundle. It is never a retriever, a discovery mechanism, or a source
     of evidence.
4. **The Digest as an Evidence Artifact**:
   - A digest is not an ephemeral script output. Every digest is persisted alongside a cryptographic `manifest.json` recording the exact note IDs, file hashes, extraction timestamps, and validation proofs that produced it.

---

## 2. Pipeline Architecture

```
                  Vault/notes/  +  Vault/collections/
                                 │
                                 ▼
                        [ VaultRetriever ]
                                 │
                   (Deterministic Note Selection)
                                 ▼
                         [ SelectedNote ]
                                 │
                   (Bundle Assembly & Content Hash)
                                 ▼
                        [ EvidenceBundle ]
                                 │
                                 ▼
                     [ Evidence Extractor ]
                    └── DeterministicExtractor (v0.1 Pure Python)
                                 │
                         [ Evidence Bundle ]
                                 │
                     [ Optional Local Synthesizer ]
                                 │
                   (Structured Claims + Citations)
                                 ▼
                          [ DigestClaim ]
                                 │
                                 ▼
                        [ ClaimValidator ]
                                 │
                   (Verifies Verbatim Quotes & Scope)
                                 ▼
                         [ DigestWriter ]
                                 │
             ┌───────────────────┴───────────────────┐
             ▼                                       ▼
   Vault/digests/<date>_<name>.md          Vault/digests/<date>_<name>.manifest.json
   (Human-readable Markdown)               (Cryptographic Audit Trail)
```

---

## 3. Strict Physical Boundaries

1. **Input Boundary**: Reads strictly from `Vault/notes/`, `Vault/collections/`, and optionally `Vault/README.md`. Never reads from `.xhs-profile/`, `.xhs-state/`, or raw platform caches.
2. **Output Boundary**: Writes strictly to `Vault/digests/`.
   - `Vault/digests/` must reside directly inside `Vault/`.
   - Any path traversal (`../`, absolute paths, symlinks escaping `Vault/digests/`) immediately aborts with `BoundaryViolationError`.
3. **P1 Invariant**: Zero reads or writes to P1 storage directories (`data/`, `.xhs-state/`).

---

## 4. Contract Schemas

### 4.1. `DigestRequest` Schema
Defines the explicit query parameters selecting notes for synthesis:

```yaml
DigestRequest:
  digest_name: string          # Identifier (e.g., "ai_daily", "weekly_food")
  target_date: string          # Artifact label only; never an implicit data-time filter
  source:
    collections: list[string]  # Target collection names (e.g. ["coding"], ["吃"]), or empty for all
  selection:
    max_notes: integer         # Hard ceiling (default: 10, maximum: 50)
    order_by: list[string]     # v0.1: ["collection_position ASC", "note_id ASC"] only
  synthesizer:
    type: string               # "extractor" (default) or explicitly configured local_llm
    model_tag: string          # e.g., "deterministic_extractor_v1", "local-model-tag"
  strict_provenance: boolean   # If true, reject any claim lacking verified source quotes (default: true)
```

---

### 4.2. `SelectedNote` Schema
Represents a single admitted source note with cryptographic integrity:

```json
{
  "note_id": "6aa174a8000000002901b985",
  "title": "东京必吃list✨5家平价米其林&顶美饭",
  "author_name": "西资卡",
  "primary_collection": "吃",
  "vault_collection_position": 4,
  "memberships": [
    {
      "collection_id": "6a9998ba000000002402ff99",
      "collection_name": "吃",
      "vault_collection_position": 4
    }
  ],
  "collections": ["吃"],
  "content_text": "来东京不知道吃什么？卡姐建议先把这5家存进收藏夹！...",
  "file_path": "notes/6aa174a8000000002901b985.md",
  "file_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
}
```

> [!IMPORTANT]
> **Collection Position Provenance Disclaimer**:
> `vault_collection_position` represents ordering inside the local Vault collection markdown projection (`Vault/collections/<name>.md`). It is **not** evidence of original platform ordering.

---

### 4.3. `EvidenceBundle` Schema
The complete, sealed input payload passed into the Synthesizer:

```json
{
  "bundle_id": "bundle_20260917_ai_daily",
  "request_fingerprint": "a591a6d40bf420404a011733cfb7b190d62c65bf0bcda32b57b277d9ad9f146e",
  "created_at": "2026-09-17T14:40:00Z",
  "total_notes": 3,
  "notes": [ /* SelectedNote objects */ ]
}
```

#### Selection-Time Semantics

`CanonicalPost.collected_at` records when P1 acquired a note. It is **not**
evidence of when the user saved, viewed, authored, or interacted with it, and
must not be used as a Phase C time filter or ranking signal in v0.1.

For v0.1, selection is bounded by explicitly named collections and their
observed relationship order: `primary_collection` priority (matching `DigestRequest.source.collections` order), then `vault_collection_position ASC`, then `note_id ASC`.
If evidence does not supply a stable membership position, the request must fail
closed rather than substitute `indexed_at`, filesystem mtime, or ingestion time.

---

### 4.4. `DigestClaim` & Provenance Schema
The intermediate representation enforced before any Markdown rendering. **Every
claim must be grounded in an explicit source note and verifiable quote**:

```json
{
  "claim_id": "c_001",
  "claim_type": "SUMMARY",
  "topic": "Agent Evaluation",
  "summary": "Agent 系统需要建立端到端的评测基准，避免单次测试的偶然性。",
  "evidence": [
    {
      "note_id": "6a97a054000000002802f761",
      "verbatim_quote": "面试官连环追问核心在于评测集的代表性与可复现性"
    }
  ]
}
```

`claim_type` is mandatory and has exactly these v0.1 values:

| Type | Required rendering semantics |
| --- | --- |
| `FACT` | A source-reported factual statement. It must not be rendered as independently verified external truth. |
| `OPINION` | Attribute the judgment to the source author (for example, “the author argues…”). |
| `RECOMMENDATION` | Attribute the recommendation to the source author; do not turn it into a universal ranking or instruction. |
| `SUMMARY` | A bounded paraphrase of the cited excerpt(s), with no new conclusion, comparison, or causal claim. |

Quotes demonstrate what a source said; they do not independently establish
truth about the world. The renderer must preserve the distinction above rather
than laundering an author’s opinion or recommendation into a system fact.

#### Validation Rules (`ClaimValidator`):
1. **Scope Check**: `note_id` must exist within the current `EvidenceBundle`.
2. **Verbatim Check**: `verbatim_quote` must be an exact substring of the corresponding `SelectedNote.content_text`.
3. **No Uncited Claims**: If a claim has `len(evidence) == 0`, it is rejected.
4. **Anti-Laundering Rule**: If `strict_provenance == true`, a claim failing
   verification is rejected from rendering and recorded as an omission in the
   manifest. It must never be repaired, rephrased, or rendered without evidence.
5. **Type Rule**: A claim without a valid `claim_type`, or whose rendered wording
   violates that type's attribution requirement, is rejected. Missing
   attribution results in omission, never speculation.

---

### 4.5. `DigestManifest` Schema
Persisted alongside every generated digest under `Vault/digests/<date>_<name>.manifest.json`:

```json
{
  "manifest_version": "1.0",
  "vault_schema_version": "1.0",
  "retriever_version": "0.1",
  "digest_name": "ai_daily",
  "target_date": "2026-09-17",
  "generated_at": "2026-09-17T14:45:00Z",
  "synthesizer": {
    "type": "deterministic_extractor_v1",
    "model_tag": "pure_python_rule_engine"
  },
  "request": {
    "source_collections": ["coding"],
    "max_notes": 10,
    "order_by": ["collection_position ASC", "note_id ASC"]
  },
  "inputs": [
    {
      "note_id": "6aa62ab4000000001001fc4f",
      "title": "一个月 20 刀的 Antigravity CLI，可能被很多人低估了",
      "sha256": "4b825dc642cb6eb9a060e54bf8d69288fbee4904ce6243dabbc4e3a39e78ea94"
    }
  ],
  "candidate_excerpts_count": 3,
  "verified_excerpts_count": 3,
  "output_file": "digests/2026-09-17_ai_daily.md",
  "output_sha256": "8f481f6954203798544e398d363d6b0577be1315b4931a19ce556ebf7b15a953",
  "review_status": "DRAFT",
  "status": "COMPLETE",
  "errors": []
}
```

`vault_schema_version` identifies the Markdown/frontmatter schema consumed by
the retriever; `retriever_version` identifies the selection and parsing rules.
Neither version may be inferred from a file hash.

`review_status` is one of `DRAFT`, `REVIEWED`, or `PUBLISHED`. Automated runs
may create `DRAFT` only. A human must explicitly promote a digest to
`REVIEWED` or `PUBLISHED`; a synthesizer and validator cannot self-approve.

---

## 5. Output Markdown Specification

Target path: `Vault/digests/<YYYY-MM-DD>_<digest_name>.md`

### Structure:
```markdown
---
digest_name: "ai_daily"
target_date: "2026-09-17"
generated_at: "2026-09-17T14:45:00Z"
synthesizer: "deterministic_extractor_v1"
notes_referenced: 3
review_status: "DRAFT"
status: "COMPLETE"
---

# Knowledge Digest: ai_daily (2026-09-17)

## 📌 Topics & Key Observations

### Topic: Agent Systems & CLI
- **一个月 20 刀的 Antigravity CLI，可能被很多人低估了**
  - **Source Summary**: 强调本地 CLI 自动化工具的吞吐与稳定性优势。
  - **Source**: [[6aa62ab4000000001001fc4f|一个月 20 刀的 Antigravity CLI，可能被很多人低估了]] — *艾康的AI自留地*
  - > *"CLI 的响应速度和脚本化能力大幅超越网页端界面"*

---

## 📊 Digest Provenance & Audit
- **Referenced Notes**: 3
- **Evidence Verification**: 100% verified against local vault artifacts
- **Audit Manifest**: `digests/2026-09-17_ai_daily.manifest.json`
```

---

## 6. Failure States & Handling

| Failure Code | Condition | Behavior (Strict Fail-Closed) |
|---|---|---|
| `EMPTY_SELECTION` | 0 notes matched the request criteria | Aborts without writing markdown. Records failed attempt. |
| `NOTE_CONTENT_EMPTY` | Selected note exists but has zero body text | Excluded from bundle with warning. |
| `EVIDENCE_INVALID_NOTE_ID` | A proposed claim cites a note_id not present in the input bundle | Reject and omit the claim; do not expand retrieval or search for another source. |
| `EVIDENCE_UNVERIFIABLE_QUOTE` | Cited quote does not appear verbatim in note source | Reject and omit the claim in every mode; record the omission in the manifest. |
| `OUTPUT_BOUNDARY_VIOLATION` | Output target attempts to write outside `Vault/digests/` | Immediate fatal exception; 0 bytes written. |
| `PARTIAL_SYNTHESIS` | Some claims succeeded, but others failed validation | Invalid claims are omitted; the remaining output stays `DRAFT` and the manifest records each omission. |
| `CLAIM_TYPE_INVALID` | Claim type is absent or attribution wording does not match its type | Omit the claim; never silently coerce it to `FACT`. |
| `SELECTION_POSITION_MISSING` | A selected collection member has no stable observed position | Abort before bundle creation; do not substitute ingestion time or mtime. |

---

## 7. Extraction and Optional Synthesis Contracts

To ensure the architecture is model-agnostic and 100% testable offline:

```python
class EvidenceExtractor(ABC):
    @abstractmethod
    def extract(self, bundle: EvidenceBundle) -> list[EvidenceExcerpt]:
        """Returns exact candidate excerpts from a sealed EvidenceBundle.

        It must not infer claims, assign external truth, or write to disk.
        """
        pass


class LocalSynthesizer(ABC):
    @abstractmethod
    def synthesize(self, bundle: EvidenceBundle) -> list[DigestClaim]:
        """May propose structured, attributed claims from this bundle only.

        It must not retrieve, discover sources, perform network requests, or
        write to disk. Its output is always a DRAFT pending validation and
        human review.
        """
        pass
```

- **`DeterministicExtractor` (v0.1 Target)**:
  - May only read frontmatter, title, collection membership, paragraph
    boundaries, and exact quote locations.
  - May only emit title/paragraph **candidate excerpts** using deterministic
    slicing and exact text locations.
  - Must not perform topic inference, sentiment analysis, value ranking,
    recommendation inference, keyword-based classification, or any other
    semantic interpretation.
  - Does not label excerpts as facts, opinions, recommendations, or insights.
  - Runs in milliseconds, zero cost, 100% offline, and needs no API key.
- **`LocalSynthesizer` (future, optional)**:
  - Is local-only and receives the sealed `EvidenceBundle`, never Vault paths
    or a retrieval capability.
  - Must emit the same typed schema and pass through `ClaimValidator`.
  - Is not part of the v0.1 implementation acceptance gate.

---

## 8. Acceptance Criteria for Phase C

1. **`VaultRetriever`**:
   - Accurately queries notes by collection and limit.
   - Uses only observed collection position and `note_id` for v0.1 ordering;
     it does not treat `collected_at` as a user activity time.
   - Deterministic sorting guaranteed.
2. **`ClaimValidator`**:
   - Rejects hallucinated quotes and invalid note IDs with explicit error codes.
3. **`DeterministicExtractor`**:
   - Produces only exact, source-addressable candidate excerpts for test bundles
     without network calls.
4. **`DigestWriter`**:
   - Atomically writes `.md` and `.manifest.json` to `Vault/digests/`.
   - Protects boundaries against escaping `Vault/digests/`.
5. **Reproducibility**:
   - Identical input hashes, request fingerprint, schema versions, and extractor
     version produce the same selected evidence bundle and content fingerprint.
   - Runtime metadata such as `generated_at` may differ; it must not be used as
     evidence selection input or represented as byte-identical output proof.
6. **Human Review Boundary**:
   - Every generated digest starts as `DRAFT`; automated code cannot mark it
     `REVIEWED` or `PUBLISHED`.

---

## 9. Human Review and Manifest Boundary

The v0.1 lifecycle is intentionally one-way:

```text
Selected local evidence
        |
        v
Draft digest + manifest
        |
        v
Explicit human review
        |
        v
REVIEWED or PUBLISHED artifact
```

`DRAFT` is a safety boundary, not an implementation detail. A draft may be
read by a human, but it is not an approved knowledge assertion and must not be
silently promoted into a future knowledge graph or other durable derived layer.

The manifest is the durable provenance record for one digest run. Its stable
note IDs, hashes, selection parameters, versions, review state, and validation
outcomes are intentionally future-compatible with later analysis of how a
claim changed over time. v0.1 does **not** create a claims database,
`claims.json`, knowledge graph, or cross-digest query system; such work needs a
separate contract.
