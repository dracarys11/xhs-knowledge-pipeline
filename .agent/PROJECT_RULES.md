# Project Rules

## Project Identity

Name: `xhs-knowledge-pipeline`

Current phase: Phase C MVP

Core principle: evidence first. No semantic invention.

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
