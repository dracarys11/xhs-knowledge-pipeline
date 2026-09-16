# Post Freeze Development Rules

## Frozen invariants

1. StateStore is single owner of state transition
2. COMPLETE requires filesystem verification
3. No hidden retry policy outside StateStore
4. No implicit enumeration during fetch
5. No concurrent workers
6. No workflow engine

## Change policy

Any modification touching:

- `state.py`
- `sync.py`
- `collector.py`
- `media.py`

must include:

- reason
- invariant affected
- tests added
- rollback plan

## P2 allowed scope

Allowed:

- performance optimization
- observability
- CLI improvements
- UX

Not allowed without redesign:

- distributed architecture
- MQ
- worker pool
- async pipeline
