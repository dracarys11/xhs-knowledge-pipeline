# Acquisition Contract v1

## Status

**FROZEN GOVERNANCE CONTRACT**

This contract governs any Collector that accesses XHS through a user-owned,
authenticated account. It defines the access boundary before P2.2 Collection
Relationship Import may proceed.

This project is a Personal Account Knowledge Export Pipeline. A Collector is
not a general discovery mechanism and must not evolve into one.

## Collector Run Declaration

Before every real-account Collector run, the initiating human must supply and
approve this declaration:

```yaml
Purpose: <why this user-owned data access is necessary>
Scope: <exact account surface, identifiers, and maximum range>
Evidence: <minimal fixture or artifact that proves success or failure>
Stop Condition: <the exact event that ends this run>
Mutation Boundary: <whether the run can change remote state; normally false>
Expansion Risk: <how the run could grow beyond its declared scope>
```

The declaration is mandatory. An absent, ambiguous, or open-ended field means
the run must not start.

### Required Semantics

| Field | Required property |
| --- | --- |
| Purpose | Must describe personal data portability, not broad platform extraction. |
| Scope | Must be finite and specific; it cannot mean “explore,” “find more,” or “all accessible data.” |
| Evidence | Must identify the redacted, minimum fixture or completion proof saved for offline use. |
| Stop Condition | Must be observable and fail closed on missing proof, unexpected auth, verification, risk control, or unknown schema. |
| Mutation Boundary | Must declare all local and remote writes. Default is no remote mutation and no P1 mutation. |
| Expansion Risk | Must name possible automatic navigation, retries, pagination, or related-surface discovery; those actions remain prohibited unless separately approved. |

## Human-Assisted Acquisition Model

The only approved direction of control is:

```text
Human-approved goal and run budget
        |
        v
One-time, fixed-scope account observation
        |
        v
Redacted evidence fixture
        |
        v
Offline parser and indexer
        |
        v
Knowledge and agent layers
```

The browser may authenticate, observe the approved user-owned surface, and
capture the necessary response. It may not conduct random exploration, expand
to adjacent surfaces, retry continuously, simulate browsing at scale, or let
an Agent determine the next page or interaction.

The fixture is the handoff boundary. Parser, indexer, tests, knowledge graph,
and any future agent work must run offline against saved, redacted evidence.

## Credential and Provenance Boundary

Long-lived knowledge artifacts must contain data provenance, not access
credentials:

```text
Data provenance != Access credential
```

Stable source identity may be represented as:

```text
https://www.xiaohongshu.com/explore/<note_id>
```

It must not retain cookies, authorization headers, session tokens,
`xsec_token`, signed URLs, or private request parameters. Access context, when
needed, is non-secret and minimal, for example capture time and acquisition
method.

## P2.2 Gate 0: Required Ordering

P2.2 Member Import and Collection Expansion are blocked until these governance
steps are completed and independently verified:

```text
P2.2 Gate 0
        |
        v
Remove token persistence from long-lived artifacts and state
        |
        v
Remove stealth-oriented browser identity
        |
        v
Freeze the human-assisted acquisition model
        |
        v
Re-validate Phase 1 hashes and evidence boundaries
        |
        v
Continue Collection Relationship Import
```

No Collector run may be used to discover how to satisfy Gate 0. Gate 0 work
must be planned, reviewed, and validated with offline fixtures where possible.

## Phase Boundary

After Gate 0 and the constrained Collection Relationship Import are complete,
the project may consider later phases in this order:

```text
Phase 2: Collection Relationship Import
        |
        v
Phase 3: Knowledge Graph
        |
        v
Phase 4: Personal Agent
```

Neither a knowledge graph nor a personal agent receives authority to invoke a
Collector. Any later account observation starts a new human-approved run under
this contract.

## Non-Authorization

This contract does not authorize real-account access, token migration, changes
to P1, browser automation changes, or Member Import implementation. It is a
freeze boundary and review prerequisite only.
