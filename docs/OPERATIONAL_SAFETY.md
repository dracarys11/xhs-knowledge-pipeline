# Operational Safety: XHS Personal Data Access

> This project prioritizes correctness, provenance, and user-controlled data
> portability over collection volume.

> The system must prefer incomplete but proven data over broad but
> unverifiable extraction.

## 1. Project Identity

This project is a **Personal Account Knowledge Export Pipeline**. It exists to
make data from a user's own, actively authorized account portable, traceable,
and useful for local knowledge management and analysis.

```text
User-owned data
        |
        v
Evidence-based extraction
        |
        v
Local knowledge representation
```

It is not a public-web collection system, a generic crawling framework, or a
dataset-generation tool:

```text
Public web crawling
        |
        v
Mass collection
        |
        v
Dataset creation
```

Project documentation, README material, and code comments must not position
the project as a platform for scraping, crawler frameworks, detection bypass,
anti-ban tactics, or stealth collection.

## 2. Account Boundary

Allowed use is limited to:

- the user's own logged-in account;
- data access the user has actively authorized;
- local knowledge-base synchronization and export;
- one-off or low-frequency runs with a declared purpose.

The following are prohibited:

- multi-account batch operation;
- collection from third-party accounts;
- scaled public-content collection;
- collection for training datasets or commercial databases.

```text
Personal portability > Platform extraction
```

## 3. Browser Automation Rules

Browser automation may only:

```text
authenticate
observe user-owned data
capture required responses
```

It must not:

```text
simulate human browsing at scale
randomly explore
automatically discover unrelated surfaces
mass navigate
```

### Run Budget

Every real-account run requires a recorded budget before it starts:

- **Purpose** — the specific user need;
- **Scope** — exact account surface and data range;
- **Evidence** — the artifact that proves the requested result;
- **Stop condition** — the event after which the run ends.

Example of an allowed run:

```text
Collect: user's collection metadata
Expected: 2 boards
Stop: after completion evidence is captured
```

Disallowed run objectives include “explore all pages,” “find more endpoints,”
or “retry continuously.” A run may not expand its own scope from observations.

## 4. Evidence-First Rule

Each real-account access must follow this one-way boundary:

```text
Observation
        |
        v
Evidence artifact
        |
        v
Offline processing
```

This is prohibited:

```text
Browser
        |
        v
LLM reasoning
        |
        v
More browsing
```

An agent must not use an observation to initiate more browsing. Missing data is
an offline finding, not permission to expand access.

## 5. Fail-Closed Rules

The following signals immediately stop the active run:

```text
risk_controlled
verification_required
captcha
unexpected_auth_state
schema_unknown
```

On stop, the run must not automatically retry, change access strategy, raise
request frequency, or attempt to bypass verification. It must record an
honest terminal state such as:

```text
UNKNOWN
FAILED
INCOMPLETE
```

It must never infer success from an empty page, an unrecognized response, or a
missing completion signal.

## 6. Separation of Layers

The architecture must retain this direction of control:

```text
Acquisition Layer
        |
        v
Evidence Layer
        |
        v
Canonical Data
        |
        v
Projection Layer
        |
        v
Agent / Reasoning Layer
```

The Agent / Reasoning layer must never directly control the Acquisition layer.
If offline analysis identifies a gap, the allowed path is:

```text
AI identifies missing data
        |
        v
Acquisition request
        |
        v
Human-approved collection run
```

## 7. Data Handling Rules

Evidence artifacts must be redacted, reproducible, and limited to the minimum
fields needed to establish the observed fact. They must not persist:

- cookies;
- authorization headers;
- session tokens;
- `xsec_token` values;
- signed URLs;
- private request parameters.

Authentication material remains in the browser-managed session only. Evidence
may record redacted field names, response status, schema shape, and completion
signals, but not reusable credentials or signed request material.

## 8. Development Review Checklist

Every proposed collector or importer must answer these questions before any
real-account run:

| Review | Required answer |
| --- | --- |
| Purpose | Why is this access necessary for the user's own data? |
| Scope | Which exact surface and records are in scope? |
| Evidence | Which minimal artifact proves completion or failure? |
| Stop condition | What immediately ends the run? |
| Mutation boundary | Can it change P1 data, state, profile, or outputs? |
| Expansion risk | Could it become a crawler through automatic discovery or repetition? |

No answer means no real-account run.

## 9. Current Architecture Review

**Verdict: partially aligned; not yet fully compliant with this policy.**

Aligned characteristics:

- P1 separates acquisition, raw evidence, canonical artifacts, state, and
  downstream projection.
- P2.1 export is downstream and read-only with respect to P1 data and state.
- Current failure contracts distinguish authentication, risk, parsing, and
  incomplete outcomes instead of treating them as completed work.
- Evidence reports explicitly retain `UNPROVEN` remote coverage rather than
  claiming full account coverage.

Open policy gaps, recorded here without authorizing implementation changes:

1. P1 persists `xsec_token` in `.xhs-state/sync.db` and in
   `.xhs-profile/tokens_cache.json`. This conflicts with §7's prohibition on
   retaining reusable tokens. See `src/xhs_ingest/state.py` and
   `src/xhs_ingest/collector.py`.
2. P1 and the current collection-discovery implementation launch Chromium with
   `--disable-blink-features=AutomationControlled`. This is incompatible with
   the project's prohibition on detection-bypass positioning and behavior.
3. The current P2.2 collection discovery uses automated page navigation and
   tab clicks to trigger observations. Its scope must remain a human-approved,
   fixed-budget run; it must not become autonomous exploration.
4. Historical audit and evidence documents contain terms such as “scraper” or
   “crawling” when describing external projects and risks. They are not project
   positioning, but future README material and code comments must follow §1.

This document is a governance boundary. It does not reopen P1, authorize
credential migration, or permit changes to acquisition behavior on its own.
