# LLM-Wiki Schema

## Purpose

Use this schema as the V0 contract for all pages written by `ingest-paper`.
Markdown files under `wiki/` are the source of truth.

## Directory layout

```text
wiki/
├── papers/
├── concepts/
├── benchmarks/
└── errors/
```

- Store one paper per file under `wiki/papers/`.
- Store reusable concepts, methods, techniques, and architectures under `wiki/concepts/`.
- Link existing benchmark pages under `wiki/benchmarks/`.
- Do not write to `wiki/errors/` during ingestion unless explicitly requested.

## File names and IDs

- Use lowercase kebab-case file names, such as `longlora.md`.
- Keep a page ID stable after creation.
- Format paper IDs as `paper:<slug>`.
- Format concept IDs as `concept:<slug>`.
- Prefer DOI, then arXiv ID, then normalized title when checking paper duplicates.
- Update an existing page when any canonical identifier matches.
- Use `null` for unavailable metadata; never guess a value.

## Wiki links

Treat `wiki/` as the link root and omit the `.md` extension:

```markdown
[[papers/longlora]]
[[concepts/shifted-sparse-attention]]
[[benchmarks/longbench]]
[[concepts/shifted-sparse-attention|Shifted Sparse Attention]]
```

Link only to pages that exist. Record a missing target under `Open Questions`
instead of creating an unsupported page. Do not manually maintain backlinks.

## Paper page

Use this required frontmatter:

```yaml
---
id: "paper:<slug>"
type: paper
title: "<paper title>"
authors:
  - "<author>"
year: 2023
venue: null
identifiers:
  arxiv: null
  doi: null
urls:
  paper: "<canonical paper URL>"
status: draft
---
```

Allowed page statuses are:

- `draft`: ingested but not independently verified;
- `needs-review`: incomplete source access, unresolved identity, or uncertain evidence;
- `verified`: reserved for a separate evidence-verification workflow.

Include these body sections in this order:

1. `Problem`
2. `Method`
3. `Key Claims`
4. `Experiments`
5. `Limitations`
6. `Wiki Links`
7. `Open Questions`

### Claim records

Keep claims inside the paper page in V0. Give each claim a stable page-local ID:

```markdown
### C1

- **Statement:** A scoped, atomic claim.
- **Attribution:** author
- **Evidence type:** experiment-supported
- **Evidence location:** PDF p. 7, Table 2
- **Scope:** Model, dataset, context length, metric, and other conditions.
- **Evidence status:** located
```

Use these controlled values:

- `Attribution`: `author` or `agent-analysis`;
- `Evidence type`: `author-stated`, `experiment-supported`, or `inferred`;
- `Evidence status`: `located`, `partial`, or `unlocated`.

Do not renumber existing claim IDs during an update. Append the next available ID.

### Experiment records

Record important results in a table with these columns:

```markdown
| ID | Model | Dataset / Benchmark | Setting | Baseline | Metric | Result | Evidence |
|---|---|---|---|---|---|---|---|
```

Use stable page-local experiment IDs such as `E1`. Put setup details that do not
fit the table immediately above or below it.

## Concept page

Methods are concept pages with `kind: method`; do not create a separate methods
directory in V0.

Use this required frontmatter:

```yaml
---
id: "concept:<slug>"
type: concept
title: "<canonical name>"
aliases: []
kind: concept
status: draft
---
```

Allowed `kind` values are `concept`, `method`, `technique`, and `architecture`.
Include `Definition`, `Scope`, `Distinguishing Features`, `Provenance`,
`Linked Papers`, `Related Concepts`, and `Notes` sections.

Create a concept page only when it is reusable across papers. Add alternate names
to `aliases` instead of creating duplicate pages.

## Update rules

- Preserve correct existing content and merge new evidence into it.
- Do not silently remove claims, identifiers, aliases, or manually written notes.
- Mark conflicting evidence explicitly under `Open Questions`.
- Replace or remove all `{{placeholder}}` tokens copied from an asset.
- Do not add frontmatter fields or body sections outside this schema unless requested.
