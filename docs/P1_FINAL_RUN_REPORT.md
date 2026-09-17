# P1 Final Run Report

## Decision

P1 status: **NOT PASSED**

The full run did not obtain a server-backed enumeration completion proof.
This report records the evidence without treating a stopped enumeration as a
successful one.

## Environment and Run

- Environment: host macOS with the persisted real-session browser profile
- Start: 2026-09-17T07:00:16+0800
- End: 2026-09-17T08:00:32+0800
- Exit code: 3
- Enumeration: pages 1 through 10 were observed; the collector stopped after
  its no-progress threshold without a server terminal signal.
- Completion proof: missing (`UNPROVEN`)

## Sync Evidence

```text
Tracked notes:       138
COMPLETE:            136
MEDIA_PARTIAL:         1
RETRYABLE_FAILED:      1
FINAL_FAILED:          0
```

All 136 `COMPLETE` records passed the filesystem artifact guard: `raw.json`,
`canonical.json`, `post.md`, `.complete`, and `assets/` are present and the
recorded media verification passed.

The run recovered three records that had previously been blocked by the
restricted environment's DNS resolution. The navigation strategy change was
active during the run (`domcontentloaded`), though one detail page still
timed out and remains retryable.

## Verified

- Local state database contains 138 distinct tracked notes with consistent
  state totals.
- 136 completed notes have verified on-disk artifacts.
- The media-resume path recovered three previously partial notes under a
  network-capable host environment.
- Existing crash recovery, retry-boundary, and state-transition behavior are
  covered by the P1 automated test suite.

## Pending Problems

Archived in [docs/pending/](pending/README.md):
- [P1_REMOTE_COVERAGE_PROOF.md](pending/P1_REMOTE_COVERAGE_PROOF.md)
- [P1_FINAL_FAILED_TIMEOUT.md](pending/P1_FINAL_FAILED_TIMEOUT.md)
- [P1_LARGE_MEDIA_RECOVERY.md](pending/P1_LARGE_MEDIA_RECOVERY.md)

These remain pending problems. They are not a reason to expand architecture,
state vocabulary, retry policy, or verifier layers unless they directly block
the P1 database-coverage or interrupt/resume gates.

## P1 Core Gates Remaining

1. Database coverage: local state consistency is verified, but coverage of
   the remote collection remains unproven until an explicit server terminal
   signal is observed.
2. Interrupt/resume: validate the existing recovery contract through the
   focused automated regression tests; do not alter the implementation for
   this report.
