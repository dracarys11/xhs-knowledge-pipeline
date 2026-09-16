# P1 Sync Freeze

Freeze date: 2026-09-16  
Scope: the P1 incremental-sync implementation in `src/xhs_ingest/` and its
documented operational contract. This freeze records the implementation and
acceptance boundary; it does not claim that every production-scale scenario
has been exercised.

## 1. Frozen architecture

- `cli.py` is the command boundary. `sync` is deliberately controlled by an
  explicit `--max-notes` slice or `all`.
- `XhsPlaywrightCollector` / `CollectorSession` own browser-backed
  acquisition. A sync run uses one persistent browser context, enumerates
  favorites page-by-page, then fetches notes sequentially.
- `state.py` owns workflow state only: SQLite schema v2, note lifecycle,
  retry budget, media execution records, and run metadata. It never owns or
  validates `data/` artifacts.
- `sync.py` is the sole orchestration and state-to-filesystem boundary. It
  performs startup recovery, COMPLETE artifact guarding, page checkpointing,
  media resume, atomic artifact writes, and final commit.
- `normalizer.py` and `renderer.py` remain pure transformations;
  `media.py` downloads sequentially with atomic per-file replacement and
  checksums. The pipeline is single-process, single-threaded, and ordered.

## 2. Frozen contracts

- Favorites enumeration yields server pages. Only an explicit server terminal
  page (`has_more == false`) or verified empty state proves completion;
  a slice or stalled/unproven enumeration must not return success.
- `FavoriteRef.note_id` is the stable state key. Detail fetch consumes the
  enumerated reference and must prove that returned detail belongs to that
  same note; it must not silently re-enumerate to obtain a token.
- The state machine has nine statuses: `DISCOVERED`, `PENDING`, `FETCHING`,
  `DETAIL_SUCCESS`, `MEDIA_SYNCING`, `MEDIA_PARTIAL`, `COMPLETE`,
  `RETRYABLE_FAILED`, and `FINAL_FAILED`. Transitions and retry-budget
  terminalization are StateStore invariants.
- `FETCHING` and `MEDIA_SYNCING` are crash traces, not resting states; startup
  recovery returns them to resumable work. `FINAL_FAILED` can only be reset
  through the explicit retry-failed path.
- `COMPLETE` requires durable `raw.json`, `canonical.json`, `post.md`, a
  `.complete` marker, and verified media. Existing verified media is skipped;
  partial media resume rebuilds from `raw.json` without re-fetching detail.
- Exit code `0` requires both proven enumeration completion and no remaining
  non-COMPLETE notes. Exit `2` denotes run-level abort; exit `3` denotes
  incomplete or unproven work.

## 3. Known limitations

- There is no process-level single-instance lock; concurrent `sync` processes
  can contend for the browser profile and interleave state activity.
- Enumeration may safely stop as unproven after five no-response scrolls; weak
  or high-latency networks can therefore require a later rerun.
- Throughput is intentionally serial: typical notes take roughly 10–25
  seconds, so a large library can take hours.
- A valid interactive Web session is required. Session revocation, login
  walls, rate limits, and risk controls require user intervention.
- Real-environment validation has not yet covered one uninterrupted 1000+
  note run, a true server end-of-stream, exhausted retry budget, remote
  deletion/unfavorite handling, or media above 500 MB.

## 4. P2 backlog

1. Run and document a full `--max-notes all` validation through a real server
   terminal page and exit `0`.
2. Add an exclusive runtime lock for `.xhs-state/sync.db`.
3. Evaluate bounded intra-note media concurrency without changing note-level
   sequencing or anti-risk controls.
4. Provide a headed-mode pause/resume flow for risk-control challenges.
5. Track notes that disappear from the remote favorites listing (for example,
   soft `UNFAVORITED` state) with an explicit migration and retention policy.

## 5. Forbidden changes without ADR

The following require an approved Architecture Decision Record before change:

- Replacing the single-process, sequential runner with worker concurrency,
  queues, a workflow engine, or distributed coordination.
- Moving filesystem integrity responsibility from `sync.py` into StateStore,
  or making acquisition components depend on StateStore.
- Weakening fail-closed completion proof, detail identity proof, artifact
  verification, or the `COMPLETE` final-commit ordering.
- Changing the persisted schema/version, note lifecycle vocabulary,
  transition authority, retry-budget semantics, or state recovery rules.
- Adding token re-derivation during a run, direct signed-API acquisition,
  multi-account/boards support, daemon scheduling, or automatic retry loops.
- Changing the canonical data contract or the atomic artifact layout under
  `data/<note_id>/` in a way that breaks resume or existing artifacts.
