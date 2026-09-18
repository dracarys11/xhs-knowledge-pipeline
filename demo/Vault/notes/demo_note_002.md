---
title: "Reliable LLM Evaluation"
note_id: "demo_note_002"
---

# Reliable LLM Evaluation

A passing test proves that its assertions succeeded, not that those assertions actually distinguished the intended failure mode. Green tests can hide weak comparisons, shared state, or accidental identity: if a reproducibility check reads back the same artifact that a previous run already deposited, the comparison degenerates into a file checking itself rather than an independent result.

Stronger evaluation explicitly snapshots state before each run, tests that invalid inputs fail closed, and compares two independently captured outputs rather than reusing a previously generated artifact. Negative tests — cases that must fail rather than pass — are often more informative than positive tests, because they reveal whether the boundary being checked is actually enforced.

Observable side effects are more reliable evidence than plausible textual explanations. Filesystem hashes, exact state-machine transitions, and byte-level artifact comparisons leave less room for an implementation to be subtly wrong while appearing correct in a report.

