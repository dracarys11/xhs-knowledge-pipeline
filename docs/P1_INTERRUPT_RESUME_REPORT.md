# P1 Interrupt Resume Test

## Result

PASS

## Scenario

- **Test Script**: `scripts/p1_interrupt_resume_test.sh`
- **Run ID**: `p1_resume_20260917_100252`
- **Execution Log**: `runs/p1_resume_20260917_100252/`
- **Started sync**: Launched `python -m xhs_ingest.cli sync --max-notes all` (PID 61927).
- **Interrupted after 120s**: Sent `SIGTERM` (`kill -TERM 61927`). Process was actively processing notes and was terminated with signal 15 (`Terminated: 15`). Note `6932dc8f000000001e0344b4` was interrupted mid-download in `MEDIA_SYNCING`.
- **Restarted sync**: Launched resume run. Startup recovery automatically detected the interrupted state (`{'media_syncing_reset': 1}`) and reset it to `DETAIL_SUCCESS`. Media resume pipeline resumed `6932dc8f000000001e0344b4` without refetching detail, and subsequent fetch queue completed all remaining pending notes.

## Before

- **COMPLETE**: 122
- **PENDING**: 15
- **RETRYABLE_FAILED**: 1
- **TRACKED**: 138 (distinct note IDs: 138)

## After

- **COMPLETE**: 137 (+15)
- **FINAL_FAILED**: 1 (note `69689236000000000e03f404` exhausted 5 attempts due to persistent navigation timeout)
- **PENDING**: 0
- **TRACKED**: 138 (distinct note IDs: 138)

## Verified

- **No database corruption**: SQLite WAL database remained completely intact and valid across SIGTERM kill.
- **No duplicate records**: `tracked_count` and `distinct_note_id_count` strictly match (138 == 138); no duplicate rows created.
- **Resume succeeded**:
  - Interrupted note `6932dc8f000000001e0344b4` in `MEDIA_SYNCING` was cleanly recovered by `store.recover_interrupted()` to `DETAIL_SUCCESS`.
  - Resumed via `_media_phase` and completed to `COMPLETE`.
  - All other 14 notes were fetched, downloaded, and sealed with `.complete`.
- **State transitions valid**:
  - `MEDIA_SYNCING` → `DETAIL_SUCCESS` (startup crash recovery).
  - `DETAIL_SUCCESS` → `MEDIA_SYNCING` → `COMPLETE` (media resume pipeline).
  - `PENDING` → `FETCHING` → `DETAIL_SUCCESS` → `MEDIA_SYNCING` → `COMPLETE` (fetch queue).
  - `RETRYABLE_FAILED` → `FETCHING` → `FINAL_FAILED` (atomic budget cap exhaustion at 5 attempts).
  - Zero illegal transitions recorded.

## Pending

- **Remote coverage proof**: Server termination proof (`has_more == False` or empty-state proof) remains unproven on live XHS web stream due to live favorites list cursor characteristics (exits with code 3).
- **Media edge cases**: Network disconnections on multi-gigabyte or ultra-large media downloads (>500MB) remain to be tested under simulated packet loss.
