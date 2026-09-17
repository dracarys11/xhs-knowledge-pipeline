# Problem: Remote Coverage Proof Unproven on Live Stream

## Status
PENDING

## Discovered
2026-09-17

## Impact
Does not block P1 core gates. Sync safely exits with code 3 (fail-closed) without falsely claiming full sync, while all notes already admitted into the local tracking set are correctly ingested and persisted.

## Evidence
- `docs/P1_FINAL_RUN_REPORT.md`
- `docs/P1_INTERRUPT_RESUME_REPORT.md`
- Run logs: `WARNING [collector-session] enumeration stalled without server termination proof; stopping without claiming completion`

## Trigger to reopen
Only reopen if:
- database coverage affected
- resume regression
- data corruption

## Current decision
No implementation change.
