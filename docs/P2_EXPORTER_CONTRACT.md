# Exporter Contract v0.2 (Hardened)

## Purpose
Define the strict engineering boundaries, physical isolation requirements, input requirements, output guarantees, and non-guarantees for the P2.1 Exporter downstream projection layer.

---

## 1. Physical Isolation & Output Boundary Guard

The Exporter is a strictly read-only downstream projection layer. Under no circumstances may it mutate or write into P1 artifacts or storage directories.

### Boundary Rules (Strict Fail-Closed):
1. `vault_dir != data_dir` and `vault_dir` cannot reside inside `data_dir`.
2. `data_dir` cannot reside inside `vault_dir`.
3. `vault_dir != state_db.parent` and `vault_dir` cannot reside inside `state_db.parent`.
4. `state_db.parent` cannot reside inside `vault_dir`.
5. `vault_dir != state_db`.

Any boundary violation immediately aborts execution with a `ValueError` prior to any filesystem creation.

---

## 2. Input Specification & Path Traversal Guards

### Source Data:
1. **SQLite State Database**: `sync.db` (opened strictly in read-only URI mode `mode=ro`).
2. **Physical Artifacts Directory**: `data/<note_id>/`
   - `canonical.json` (authoritative normalized data)
   - `assets/<filename>` (persisted image/video assets)

### Candidate Selection:
- Strictly queries records with `status = 'COMPLETE'`.
- Non-COMPLETE records (`PENDING`, `FETCHING`, `MEDIA_PARTIAL`, `RETRYABLE_FAILED`, `FINAL_FAILED`) are excluded from candidate evaluation.

### Path Traversal Protection:
- `note_id` must be a safe single-component path name: non-empty string, no `/`, `\`, `\0`, `..`, or `.`.
- Media `filename` must be a safe single-component filename: non-empty string, no `/`, `\`, `\0`, `..`, or `.`.

### Admission Criteria (All must pass to be exported):
1. `data/<note_id>/canonical.json` exists, is readable, and is a valid JSON object (`dict`).
2. `canonical.note_id == DB.note_id`.
3. If `content` or `author` are present, they must be objects (`dict`).
4. If `media` is present, it must be a list of objects (`dict`).
5. For each media item declared in `canonical.json`:
   - `filename` is valid and safe.
   - `size_bytes` is present and an integer > 0.
   - `data/<note_id>/assets/<filename>` exists.
   - File size matches `size_bytes`.
6. Zero-media notes (plain text) are admitted if `media` list is empty and points 1–4 pass.

### Rejection Handling:
- Records failing any admission criteria are skipped.
- Skipped notes are recorded in `export_manifest.json` with explicit reason codes (`MISSING_CANONICAL`, `INVALID_CANONICAL_SCHEMA`, `INVALID_NOTE_ID_PATH`, `INVALID_MEDIA_FILENAME`, `MEDIA_MISSING_SIZE_BYTES`, `MEDIA_FILE_MISSING: <filename>`, `MEDIA_SIZE_MISMATCH`, etc.).
- **Zero-Mutation Rule**: The Exporter never attempts to repair, re-fetch, retry, or download missing assets.

---

## 3. Output Specification & Stale Artifact Handling

Target directory: `Vault/` (or user-specified directory)

```text
Vault/
├── notes/
│   └── <note_id>.md
├── assets/
│   └── <note_id>/
│       └── <filename>
└── export_manifest.json
```

### Deterministic Asset Copy:
- Assets are copied atomically via temporary staging files (`.<filename>.tmp` -> rename).
- If destination asset already exists with identical file size, SHA-256 content hashes are compared:
  - If SHA-256 matches: redundant copy is skipped (preserving file modification timestamp and disk I/O).
  - If SHA-256 differs: destination is atomically replaced with the verified source asset.

### Stale Artifact Reconciliation (Managed File Cleanup):
- In full evaluation runs (`limit is None`), any note markdown in `Vault/notes/` or asset directory in `Vault/assets/` not present in the current successfully exported set is pruned.
- **Rationale for Managed Cleanup vs. Whole-Directory Replacement**:
  1. *Obsidian Configuration Safety*: Users store `.obsidian/` configs, plugins, and custom notes in their Vault. Whole-directory replacement would destroy user data.
  2. *POSIX Directory Replacement Limitation*: POSIX `rename(2)` on an existing directory returns `[Errno 66] Directory not empty`. Non-empty directory replacement cannot be done in a single atomic syscall.
  3. *Incremental Asset Caching*: Preserves verified existing media, preventing tens of gigabytes of re-copying.

### Markdown Content:
- **Frontmatter**:
  - `title`: string
  - `note_id`: string
  - `author_name`: string
  - `author_id`: string (if present)
  - `source_url`: string (if present)
  - `collected_at`: string (ISO datetime, if present)
  - `stats`: `liked_count`, `collected_count`, `comment_count`, `share_count` (only if present)
  - *(Strictly no fake publish_time, no hallucinated tags)*
- **Body**:
  - Header with title and metadata links.
  - Content section preserving original text.
  - Local relative media references: `../assets/<note_id>/<filename>`.

### Manifest Specification (`export_manifest.json`):
- Atomic write via temporary file (`.export_manifest.json.tmp` -> replace).
- Snapshot semantics clearly distinguish:
  - `tracked_count`: total records in DB
  - `complete_candidates`: total COMPLETE records in DB
  - `evaluated_candidates`: COMPLETE records evaluated in this run (bounded by `limit`)
  - `exported_count`: successfully exported notes in this run
  - `stale_cleaned_count`: count of pruned stale files/folders
  - `remote_coverage_proof`: `"UNPROVEN"`
  - `skipped_notes`: list of `{"note_id": str, "reason": str}`
  - `failed_notes`: list of `{"note_id": str, "error": str}`

---

## 4. Guarantees

- **Physical Boundary Security**: Vault directory cannot overlap with, contain, or reside within `data/` or `state_db` parent directory.
- **Path Traversal Protection**: Note IDs and filenames are strictly sanitized.
- **Content Output Determinism**: Markdown notes and asset files are 100% deterministic given the same inputs; repeated runs are idempotent. Manifest metadata (`export_time`) records runtime execution context.
- **No Mutation**: Zero writes to `src/xhs_ingest/`, zero writes to `sync.db`, zero writes to `data/`.
- **Manifest Evidence**: Every exported, skipped, or failed note is accounted for with verifiable proof.

---

## 5. Non-Guarantees

- **Remote Completeness**: Does not guarantee all user favorites on Xiaohongshu are exported (only exports locally verified COMPLETE records).
- **Semantic Correctness**: Does not interpret or rewrite content semantics.
- **Metadata Enrichment**: No AI summary, no embedding, no automatic tag generation.
