# Offline Demo

This directory contains synthetic data only. It demonstrates the collection
projection boundary without requiring an XHS account, browser session, or
network access.

```text
demo/evidence/collections + demo/data
        |
        v
python -m xhs_knowledge
        |
        v
Vault/collections/*.md
```

Run the offline collection-index demo:

```bash
PYTHONPATH=src python -m xhs_knowledge \
  --evidence-dir demo/evidence/collections \
  --data-dir demo/data \
  --vault-dir /tmp/xhs-demo-vault
```

The command demonstrates offline collection-index generation. The combined
post-export shape is illustrated under `demo/expected_vault/`; its synthetic
notes represent the prior P2.1 projection step. The fixture contains no real
account identifiers, cookies, session material, signed URLs, or token-bearing
URLs.
