# Engineering Decisions

## D-001: Personal data portability is the project boundary

The project serves a user's own, authorized data migration and local knowledge
use. It does not pursue public-content scale or dataset creation.

## D-002: Evidence precedes offline processing

Real-account observation produces minimal, redacted evidence. Parsing,
indexing, testing, and reasoning continue offline from that fixture.

## D-003: Personal instances do not belong in Git

Real `Vault/` and `knowledge/` directories are ignored. The repository carries
only code, contracts, tests, and synthetic demo data.

## D-004: Knowledge projection is downstream-only

Export and collection indexing may read verified local artifacts but must not
trigger acquisition, repair artifacts, or mutate P1 state.

## D-005: P2.2 is blocked by acquisition governance

Member Import cannot expand until Gate 0's credential hygiene, normal browser
context, and human-assisted fixture boundaries are complete and verified.
