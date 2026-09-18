# Project Rules

## Harness Bootstrap

Every agent session must load, before substantive work:

1. `.agent/PROJECT_RULES.md`
2. Its own role contract at `.agent/<ROLE>.md`

Before substantive work, the agent must report:

```text
Harness loaded:
- PROJECT_RULES.md
- <ROLE>.md
```

If either required file cannot be read, the agent must stop. It must not infer,
replace, or continue without its role contract.

## Project Identity

Name: `xhs-knowledge-pipeline`

Current phase: Phase C MVP

Core principle: evidence first. No semantic invention.

## Offline Development Safety

All development, review, audit, testing, demo, and documentation tasks are
**OFFLINE** unless the task explicitly authorizes live acquisition.

In offline mode, agents must not:

- Launch Playwright or Chromium for Xiaohongshu.
- Access `xiaohongshu.com`.
- Use `.xhs-profile` as an authenticated browser, session, or profile source; launch,
  attach to, or mutate a browser session backed by it.
- Run collector, sync, or acquisition commands.
- Authenticate to Xiaohongshu.
- Perform real-account probes.

Explicitly requested read-only forensic inspection of `.xhs-profile` filesystem
metadata or files is allowed only if it does not launch a browser, authenticate,
mutate the profile, or contact Xiaohongshu.

Broad commands or task labels — including `pytest`, `scripts/*`, CLI help, demo,
and audit — do not imply authorization for live acquisition.

Live acquisition requires separate, explicit authorization that names:

1. Purpose
2. Command or code path
3. Account/profile boundary
4. Expected scope
5. Stop condition

If an offline task requires live acquisition, stop and report that dependency.
Do not proceed automatically.

## Frozen Modules

The following modules are frozen:

- Retriever
- EvidenceExtractor
- ProvenanceValidator
- P1 storage layer

Agents must not modify frozen modules unless explicitly authorized.

## Forbidden Scope

Never introduce the following unless explicitly requested:

- LLM generation
- NLP inference
- embeddings
- ranking
- summarization
- topic extraction
- claims
- semantic classification

## Engineering Principles

All changes require:

1. Minimal surface area
2. Explicit tests
3. Deterministic behavior
4. Boundary verification
5. Evidence-based reporting
