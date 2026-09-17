# Architecture

## System Purpose

The system transforms a user's own, authorized platform data into local
knowledge artifacts with explicit provenance and failure boundaries.

```text
Human-approved observation -> evidence fixture -> canonical data
-> knowledge projection -> local use
```

## Layers

| Layer | Responsibility | Must not do |
| --- | --- | --- |
| Acquisition | Fixed-scope, human-approved observation | Autonomous exploration or downstream reasoning |
| Evidence | Redacted, minimum proof artifacts | Persist access credentials |
| Canonical data | Stable local representation of acquired facts | Become a session replay store |
| Projection | Read-only Markdown, media, and collection views | Repair, sync, or mutate P1 |
| Knowledge use | Offline browsing, indexing, and future reasoning | Directly control acquisition |

## Local Boundaries

`data/` and `.xhs-state/` hold P1 artifacts and state. `Vault/` and
`knowledge/` are local personal instances and remain Git-ignored. `demo/`
contains synthetic examples only.

## Current Phase Boundary

P1 is frozen. P2.1 is frozen pending its recorded acceptance gates. P2.2
Member Import is blocked by Acquisition Contract v1 Gate 0.
