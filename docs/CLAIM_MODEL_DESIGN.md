# Claim Model Design Specification v1.0
## Semantic Boundaries, Attribution Rules, and Claim Validation for Phase C.2

## Status

**DEFERRED PENDING MISSION — Not part of the Phase C MVP.**

The Phase C MVP scope is defined by `PHASE_C_MVP_SCOPE_REVIEW.md`. This
document remains design input for a later semantic-provenance phase and does
not authorize implementation.

---

## 1. Motivation: The Citation Laundering Problem

In LLM-based summarization pipelines, the most pervasive failure mode is **Citation Laundering**:
1. An LLM hallucinates an assertion or distorts the source nuance (e.g. transforming *"这家店排队很久"* into *"排队通常需要30分钟"*).
2. The pipeline naively appends a markdown backlink `[[note_id]]` or footnote `[1]` to the fabricated statement.
3. The reader or downstream system perceives a fully cited, authoritative fact.

**Backlink ≠ Provenance.** A backlink merely indicates that a file was looked at. It does not prove that the statement is faithful to the text.

To eliminate citation laundering in Phase C, we introduce an explicit, typed **Evidence-Bound Claim Model**. Before any Markdown is rendered:
- Every synthesized statement must be structured as a typed `DigestClaim`.
- Every `DigestClaim` must be physically anchored to one or more `verbatim_quote` substrings extracted from the admitted `SelectedNote`s in the `EvidenceBundle`.
- Every claim must pass the `ClaimValidator` gate, which enforces verbatim matching, attribution framing, and semantic intensity constraints.

```
EvidenceBundle
      │
      ▼
[ Proposer ] (v0.1 Pure Python DeterministicExtractor / Future Local LLM)
      │
      ▼
[ DigestClaim ] ──(Unvalidated Candidate)
      │
      ▼
[ ClaimValidator ] ◄── Validates Quote Verbatim, Attribution Framing, Intensity
      │
      ├── Pass ──► [ VerifiedClaim ] ──► DigestWriter (Render Markdown)
      └── Fail ──► [ Omission ]     ──► DigestManifest (Record Error Code)
```

---

## 2. Claim Type Taxonomy & Semantic Boundaries

Every candidate claim must declare a single, unambiguous `ClaimType`. We define four types with strict semantic boundaries:

| ClaimType | Definition | Allowed Transformations | Prohibited Transformations | Required Attribution Framing |
|---|---|---|---|---|
| **`FACT`** | Objective, verifiable assertion stated directly by the source note. | Exact textual extraction or lossless formatting normalization (e.g. `"11点到22点"` -> `"11:00-22:00"`). | Number fabrication, extrapolations, causal inferences not stated in text. | Neutral factual voice; must not imply external ground truth beyond source claim. |
| **`OPINION`** | Subjective assessment, aesthetic taste, personal sentiment, or user review by the note author. | Faithful summarization of the author's personal sentiment. | Presenting author sentiment as objective fact (`"xxx很好"` ❌). | **Mandatory Author Attribution**: `"作者认为..."`, `"笔记作者表示..."`, `"[Author] 评价..."`. |
| **`RECOMMENDATION`** | Explicit suggestion, purchase advice, avoidance warning (避雷), or tool recommendation by the author. | Accurate reflection of recommended actions or alternatives. | **Intensity Drift (强度漂移)**: Escalating `"值得一试"` to `"强烈推荐"` / `"必去"`; Generalizing personal tip to universal rule. | **Mandatory Subjective Action Framing**: `"作者建议/推荐..."`, `"作者提示避雷..."`. |
| **`SUMMARY`** | Bounded multi-sentence aggregation condensing multiple observations within the same note. | Compositional synthesis of constituent FACTs and OPINIONs. | Free-form creative summary; introducing external concepts or comparing with unreferenced notes. | Composite attribution reflecting source perspective. |

---

## 3. Detailed Boundary Rules

### 3.1. `FACT` Rules (Precision & Non-Extrapolation)
1. **Verbatim Grounding**: Every factual claim must have at least one `verbatim_quote` that contains the factual predicate and all quantities (numbers, dates, proper nouns).
2. **Numeric Fidelity**: If a claim contains a number, duration, price, or statistic, that exact value (or its trivial arithmetic equivalence, e.g. `10k` -> `10,000`) must appear in the verbatim quote.
   - *Violation Example*: Note says *"排队时间很长"*; Claim says *"排队约30分钟"* ➔ **REJECTED (`NUMERIC_FABRICATION`)**.
3. **Scope Boundedness**: The claim cannot assert universal truth; it only asserts what the source note reports.

### 3.2. `OPINION` Rules (Attribution Requirement)
1. **Linguistic Attribution Frame**: The claim statement must explicitly attribute the evaluation to the source author.
   - *Valid*: `"作者认为 Antigravity CLI 的脚本化能力大幅提升了工作效率。"`
   - *Invalid*: `"Antigravity CLI 是最好的本地工具。"` ➔ **REJECTED (`MISSING_OPINION_ATTRIBUTION`)**.
2. **Sentiment Preservation**: The emotional valence (positive, negative, ambivalent) must match the quote without hyperbole.

### 3.3. `RECOMMENDATION` Rules (Intensity Preservation Principle)
1. **Intensity Drift Prohibition**: Modifiers reflecting recommendation strength must not be amplified beyond the quote:
   - *"值得一试"* cannot become *"必须打卡"* or *"顶级推荐"*.
   - *"可以考虑"* cannot become *"强烈建议"*.
   - If a superlative modifier (`"最"`, `"必须"`, `"绝了"`, `"神级"`) appears in the claim, that exact superlative must be present in the `verbatim_quote`.
   - *Violation Example*: Note says *"可以试试这家拉面"*; Claim says *"强烈推荐东京必吃这家拉面"* ➔ **REJECTED (`INTENSITY_DRIFT`)**.
2. **Target Specificity**: Recommendations must retain conditionality (e.g. *"如果是苹果用户，可以考虑..."* must not be generalized to *"所有人推荐使用..."*).

### 3.4. `SUMMARY` Rules (Compositional Synthesis Constraint)
1. **Compositional Closure**: A `SUMMARY` claim is strictly a conjunction or condensation of constituent excerpts. It must not invent bridging concepts or causal narratives that link disparate quotes if the original text does not connect them.
2. **Multi-Quote Anchoring**: If a summary spans multiple topics, each topic must have its corresponding `verbatim_quote` in the `evidence` list.

---

## 4. Contract Data Structures

### 4.1. `EvidenceReference` Schema
```python
@dataclass
class EvidenceReference:
    note_id: str
    verbatim_quote: str
    char_offset: int | None = None  # Optional starting offset in content_text
```

### 4.2. `DigestClaim` Schema
```python
@dataclass
class DigestClaim:
    claim_id: str                          # Unique identifier within digest (e.g. "c_001")
    claim_type: ClaimType                  # FACT, OPINION, RECOMMENDATION, SUMMARY
    topic: str                             # Thematic cluster or subject
    statement: str                         # The concise claim text
    source_note_id: str                    # Primary source note ID
    evidence: list[EvidenceReference]      # Non-empty list of verbatim quotes
    attribution_author: str = ""           # Author name used in attribution frame
    intensity_tag: str | None = None       # Optional: "MILD", "MODERATE", "STRONG"
```

### 4.3. Validation Error Codes

| Error Code | Trigger Condition | Fail-Closed Action |
|---|---|---|
| `EVIDENCE_EMPTY` | `len(claim.evidence) == 0` | Omit claim; record in manifest. |
| `EVIDENCE_NOTE_NOT_IN_BUNDLE` | `claim.source_note_id` or any evidence `note_id` not in `EvidenceBundle` | Omit claim; fatal audit flag. |
| `QUOTE_NOT_VERBATIM` | `verbatim_quote` is not a literal substring of `content_text` | Omit claim; record diff in manifest. |
| `MISSING_ATTRIBUTION` | `claim_type` is `OPINION` or `RECOMMENDATION` but lacks attribution frame | Omit claim; prompt lint warning. |
| `INTENSITY_DRIFT` | `RECOMMENDATION` contains unquoted superlatives or amplified urgency | Omit claim. |
| `NUMERIC_FABRICATION` | `FACT` statement contains numbers not found in `verbatim_quote` | Omit claim. |

---

## 5. Architectural Separation: Extractor vs. Synthesizer

To guarantee zero regression and predictable pipeline execution, Phase C enforces a strict separation:

### 5.1. `DeterministicExtractor` (Phase C.2 Default, Pure Python)
- **Role**: Deterministic, non-generative, 100% offline rule-based component.
- **Mechanism**:
  - Extracts literal paragraphs or key sentence slices directly from `SelectedNote.content_text`.
  - Generates candidate `DigestClaim` objects where:
    - `statement == verbatim_quote` (zero paraphrase, zero distortion).
    - `claim_type` is assigned deterministically based on structural position or explicit keywords from note metadata.
  - Zero LLM involvement; runs in < 5ms; zero cost; zero hallucination possibility.

### 5.2. `ClaimValidator` (Phase C.2 Core Engine)
- **Role**: Universal invariant checker that sits between any proposer (Rule Extractor or LLM) and the Markdown renderer.
- **Guarantees**:
  - Independent verification: Validator does not care *who* generated the claim. It evaluates solely against the immutable `SelectedNote.content_text`.
  - Immutable audit trail: All rejected claims and omission reasons are recorded in `DigestManifest.errors`.

### 5.3. Future `LocalSynthesizer` (Optional, Strictly Frozen in v0.1)
- When enabled in future versions, a local LLM acts strictly as an untrusted candidate proposer.
- Any output from the local LLM must be parsed into `list[DigestClaim]` and pass through `ClaimValidator`.
- If the LLM drifts in intensity or hallucinates a number, `ClaimValidator` drops the claim without human intervention.

---

## 6. Acceptance Criteria for Phase C.2

When implementation is authorized, Phase C.2 will be accepted only if:
1. **Exact Verbatim Verification**:
   - `ClaimValidator` passes 100% when `verbatim_quote` matches `SelectedNote.content_text`.
   - `ClaimValidator` fails immediately (`QUOTE_NOT_VERBATIM`) if even a single punctuation mark or whitespace in `verbatim_quote` differs.
2. **Foreign Note Rejection**:
   - `ClaimValidator` fails (`EVIDENCE_NOTE_NOT_IN_BUNDLE`) if a claim cites a `note_id` outside the active `EvidenceBundle`.
3. **Attribution & Intensity Enforcement**:
   - `OPINION` claims without author framing (`"作者认为..."` etc.) fail validation.
   - `RECOMMENDATION` claims with fabricated superlatives fail validation.
4. **Deterministic Reproducibility**:
   - `DeterministicExtractor` fed identical `EvidenceBundle` produces identical candidate claims and identical hashes.
5. **Zero External Dependencies**:
   - No external APIs, no network calls, no unverified third-party libraries.
