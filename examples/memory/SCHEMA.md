# Memory file format

One fact per file. Markdown body + YAML frontmatter. The frontmatter is what makes
memory *organized* and *recallable* — not just a pile of notes.

```markdown
---
name: client-acme-billing-pref          # stable slug (id)
description: ACME pays net-30, invoices to billing@acme — one-line summary used by recall
type: project                            # user | feedback | knowledge | project | reference | incident | pattern
valid_from: 2026-06-26                   # when this fact became true
lastVerified: 2026-06-26                 # when you last confirmed it (drives staleness)
importance: 4                            # 1–5, slows decay for things that matter
relations:                               # typed edges to other memories (the graph)
  - {type: related, target: client-acme-contacts}
  - {type: cites, target: invoicing-procedure}
---

ACME pays net-30. Send invoices to billing@acme.example. Contact: Jane (AP).
Link related facts with [[client-acme-contacts]] in the body too.
```

## Why each field

- **bi-temporal (`valid_from` + `lastVerified`)** — separating "when it was true" from "when
  you last checked" lets recall *down-rank stale memories* instead of trusting them forever.
- **`type`** — drives a per-type decay half-life (a person's name barely decays; a token expires
  fast). See `recall.py`.
- **`importance`** — Ebbinghaus-style: important facts decay slower.
- **`relations` / `[[wikilinks]]`** — the typed knowledge graph. This is what lets recall
  *reason* ("this client → contact → that person"), not just string-match.

Keep one fact per file so edges are meaningful and forgetting is surgical.
