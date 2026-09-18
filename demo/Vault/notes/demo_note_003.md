---
title: "Retrieval Design Notes"
note_id: "demo_note_003"
---

# Retrieval Design Notes

A local-first knowledge pipeline can preserve provenance without claiming external truth. Storing an exact excerpt alongside the SHA-256 hash of the source file records where a piece of text came from within a local artifact; it does not verify what the original platform published or whether the external content is accurate. That distinction keeps the provenance claim precise and auditable rather than overreaching.

Portable Markdown files in ordinary directory layouts reduce dependence on continued access to a proprietary platform. Exported knowledge artifacts are plain text and ordinary filesystem structures — readable with standard tools without any specialised software — so the exported archive remains locally inspectable even if the originating service becomes unavailable, changes its API, or restricts access.

Immutable generation directories paired with a single current-pointer file make published outputs easier to inspect, reproduce, and audit. A new publication creates a fresh generation directory and atomically updates the pointer; previous generations are not rewritten. This makes it possible to compare any two published versions directly by reading the files on disk.

