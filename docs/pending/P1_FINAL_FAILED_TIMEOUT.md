# Problem: Single Note Enters FINAL_FAILED on Repeated Navigation Timeout

## Status
PENDING

## Discovered
2026-09-17

## Historical Observation
The record originally entered `FINAL_FAILED` after repeated navigation/network failures.

Current database state may contain a different latest error cause due to later retry attempts.

This document records the original boundary case, not the current mutable error field.

## Impact
Does not block P1 core gates. Note `69689236000000000e03f404` reached the 5-attempt budget cap due to persistent Playwright navigation timeouts. StateStore correctly coerced it to `FINAL_FAILED` atomically, protecting the run from infinite retry loops.

## Evidence
- `docs/P1_INTERRUPT_RESUME_REPORT.md`
- Original database record: note `69689236000000000e03f404` in `FINAL_FAILED` with `attempt_count=5`

## Trigger to reopen
Only reopen if:
- database coverage affected
- resume regression
- data corruption

## Current decision
No implementation change.
