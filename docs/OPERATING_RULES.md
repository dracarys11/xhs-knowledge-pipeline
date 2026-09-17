# Operating Rules: Live Account Interaction & Knowledge Projection Boundary

## 1. Principle: Decoupling Acquisition Risk from Knowledge Consumption

The core architecture strictly separates **Acquisition Risk** (interacting with third-party remote platforms like Xiaohongshu) from **Knowledge Consumption Value** (parsing, indexing, searching, and viewing knowledge in Obsidian).

```
                      Acquisition Boundary (High Risk)
                                     │
                             Remote Platform (XHS)
                                     │
                         Controlled Evidence Probe
                                     │
                         Immutable Sealed Fixture
                                     ▼
                      Knowledge Boundary (Zero Risk)
                                     │
                       knowledge/evidence/collections/
                                     │
                           Collection Indexer
                                     ▼
                            Obsidian Vault Index
```

---

## 2. Real Account Rules

When interacting with live user browser profiles or authenticated sessions:

### Allowed:
1. **Single Controlled Evidence Run**: Authorized one-pass acquisition to capture an explicit protocol fact or reproducible fixture.
2. **Deterministic Response Sealing**: Persisting authentic raw responses immediately into versioned fixtures under `knowledge/evidence/` with SHA256 integrity verification.

### Forbidden:
1. **Repeated Live Probing**: Executing live network probes in loops or during routine development.
2. **Exploratory Live Browsing**: Using automated agents to freely click, scroll, explore, or reverse-engineer UI state on live accounts.
3. **Frontend State Discovery Loops**: Writing scripts that repeatedly inspect live Pinia/Vue state stores across sessions.
4. **Live Ingestion of Unverified Endpoints**: Calling unevidenced remote APIs without prior offline fixture confirmation.

All downstream code (normalizers, importers, parsers, indexers, vault exporters) must be developed and tested **100% offline** against saved fixtures and mock environments.

---

## 3. Evidence Hygiene Gate (Global URL Sanitization)

To prevent security tokens, transient session parameters, and tracking artifacts from leaking into permanent personal knowledge vaults:

1. **Universal Sanitization**: Every URL entering the Obsidian Vault projection must pass through a strict sanitization gate.
2. **Prohibited URL Parameters**:
   - Platform security / signature tokens: `xsec_token`, `xsec_source`, `x_trace_id`
   - Marketing / Tracking parameters: `utm_*`, `spm`, `from_*`, `ref`, `source`
   - User session identifiers: `session_id`, `auth_token`, `token`
3. **Canonical Bare Form**:
   - Xiaohongshu note URLs must be projected as bare canonical URLs:
     `https://www.xiaohongshu.com/explore/<note_id>`
   - Connector implementations must not rely on upstream platform sanitization; the Knowledge Projection Layer must enforce this boundary unconditionally.

---

## 4. Multi-Source Architecture Roadmap

The Knowledge Projection Layer is platform-agnostic:
- Future connectors (GitHub, Reddit, RSS, YouTube) implement independent, read-only collectors producing canonical artifacts.
- The projection layer handles links, tags, collections, search indexing, and LLM agent interactions without platform-specific coupling.
