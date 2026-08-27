# LLM-Wiki V0.2 Write Contract

Markdown under `wiki/` is the source of truth. JSON under `wiki/_generated/` is
rebuildable output and must never be edited as source.

The machine-readable authority is `wiki/_meta/schema.yaml` together with
`wiki/_meta/relation-types.yaml`. This reference summarizes only what
`ingest-paper` writes.

## Entity layout

```text
wiki/
├── papers/
├── methods/
├── benchmarks/
├── models/
├── claims/
└── experiments/
```

Canonical IDs use `<type>:<lowercase-kebab-slug>`. Body links use canonical IDs,
for example `[[method:shifted-sparse-attention]]`; path links are legacy-only.

## Base frontmatter

Every new page requires:

```yaml
schema_version: "0.2"
id: "<type>:<slug>"
type: "<type>"
title: "<title>"
aliases: []
status: draft
created_at: "<ISO-8601 timestamp>"
updated_at: "<ISO-8601 timestamp>"
relations: {}
```

Optional `facets` must use research facet IDs. Ingestion may assign `draft` or
`needs-review`; it must not assign `verified`.

## Type-specific fields

### Paper

Required: `authors`, `year`, `identifiers`, and `urls`. Optional `venue` is a
string or `null` and must not be inferred from an unsupported source.

Structured relations:

- `proposes` → method;
- `reports` → experiment.

An experiment's `paper` field also creates the inverse paper→experiment edge.

### Method

Required: `definition`. Optional: `sparsity`, `implementations`.

Do not store a new method as `concept(kind: method)`. That is legacy V0 behavior.

### Benchmark

Required: `task`, `metrics`, and `source`. `source` contains a canonical URL or
paper ID.

### Model

Required: `family`, `parameters`, and `source`.

### Claim

Required: `statement`, `assessment`, and `scope`. New claims use
`assessment: open`. Evidence attribution and extraction state belong in `scope`
and the body. Lifecycle status is not epistemic assessment.

### Experiment

Required:

- `paper`;
- `method`;
- `model`;
- `benchmark`;
- `context_length`;
- `sparsity`;
- `metric`;
- `result`;
- `evidence`.

`evidence.locator` must be reproducible. `relations.supports` and
`relations.contradicts` point from experiments to claims.

## Publication invariants

- Reuse exact paper identity and equivalent typed entities.
- Never create duplicate canonical IDs or ambiguous aliases intentionally.
- Link only canonical IDs that exist in the staged graph.
- Never overwrite an existing source page in V1 ingestion.
- Reject publication when the staged Wiki contains any `ERROR` diagnostic.
- Rebuild generated indexes only after source publication succeeds.
