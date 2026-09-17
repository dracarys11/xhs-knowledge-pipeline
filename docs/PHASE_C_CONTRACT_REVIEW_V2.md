# Phase C Contract Review v2

## Core Conclusion

**Phase C Contract v1.0 is ready to freeze for a deliberately narrow v0.1
implementation:** deterministic Vault retrieval, evidence extraction, claim
validation, and draft digest artifacts. It is not yet authorization to add an
LLM integration, a retrieval framework, external access, or autonomous
knowledge publication.

## Supporting Evidence

- P1 `collected_at` is assigned by the normalizer at acquisition time, so it
  proves local capture time rather than user save time or source publish time.
- Vault Markdown exposes that acquisition timestamp as `collected_at`; it does
  not establish an independent temporal relationship for a collection member.
- The existing acquisition and safety contracts require a one-way offline
  boundary: downstream reasoning may not initiate browsing or collection.
- A backlink alone is not sufficient provenance: only a selected note ID plus
  an exact verified excerpt establishes the source of a candidate claim.

## Frozen Decisions

1. `DigestClaim.claim_type` is mandatory: `FACT`, `OPINION`,
   `RECOMMENDATION`, or `SUMMARY`.
2. A quote proves that a source said something, not that the statement is an
   independently verified fact. Rendering must preserve author attribution for
   opinions and recommendations.
3. v0.1 selection order is observed `collection_position ASC`, then `note_id
   ASC`. `collected_at`, `indexed_at`, and filesystem mtime are prohibited as
   substitute ranking or user-activity timestamps.
4. The pure-Python v0.1 component is `DeterministicExtractor`, which emits
   exact candidate excerpts. It does not synthesize findings or assign claim
   types.
5. Every manifest records `vault_schema_version`, `retriever_version`, and a
   human-review state. Automated output begins as `DRAFT`; only a human can
   promote it to `REVIEWED` or `PUBLISHED`.
6. `DeterministicExtractor` is deliberately non-semantic: it may read
   frontmatter/title/collection membership and slice paragraphs or locate exact
   quotes, but cannot infer topics, sentiment, importance, or recommendations.
7. A digest manifest is durable provenance for one run, not a v0.1 knowledge
   graph or claims index. It may support a separately contracted future layer
   without granting that layer authority now.

## Possible Problems and Treatment

| Risk | Treatment in v0.1 |
| --- | --- |
| A source opinion becomes a system fact | Mandatory claim type and attribution-aware rendering. |
| An absent relationship position tempts an implementation fallback | Fail closed with `SELECTION_POSITION_MISSING`. |
| Runtime timestamps prevent byte-identical artifacts | Reproduce the evidence bundle/content fingerprint; treat runtime metadata separately. |
| An LLM claims sources beyond the selected set | Optional future component receives only a sealed bundle; validator rejects unknown IDs or non-verbatim quotes. |
| Rule extraction grows into hidden NLP | An explicit capability allowlist forbids semantic inference and keyword classification. |

## Alternative Interpretations Rejected

- `collected_at` is **not** “the day the user favorited this note.” The current
  data model only supports acquisition time.
- A deterministic sentence tokenizer is **not** a trustworthy fact extractor.
  It is limited to identifying source-addressable excerpts.
- A validated citation is **not** automatic human approval of a generated
  digest. Validation establishes structural provenance; review establishes
  publication intent.

## Confidence

**High** for the contract boundary and v0.1 scope. **Deliberately deferred**:
local LLM integration, semantic retrieval, vector indexes, automatic review,
and any feature that accesses a browser, collector, network, or remote API.
