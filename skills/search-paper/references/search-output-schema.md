# Search Output Schema

## Canonical location

Store each run at:

```text
research/<topic-slug>/search-runs/<run-id>.yaml
```

Use `assets/search-run-template.yaml` as the starting point.

## General rules

- Use UTF-8 YAML.
- Use ISO 8601 UTC timestamps.
- Use `null` for unknown metadata.
- Preserve provider values without silently correcting them.
- Keep source identifiers as strings.
- Never store secrets.
- Remove all template placeholders before completing a run.
- Keep every discovered paper at `status: candidate` until another Skill
  changes its lifecycle.

## Run

Required:

- `schema_version`: currently `0.1`;
- `run.id` and `run.topic_slug`;
- `run.question`;
- `run.created_at` and `run.updated_at`;
- `run.status`;
- `run.provider.name`, interface, package version, and source;
- `run.stop_reason` when no longer running.

Allowed run statuses:

- `planned`;
- `running`;
- `partial`;
- `complete`;
- `blocked-credential`;
- `blocked-provider`;
- `needs-review`.

`complete` means the configured candidate-search stop rule was met. It does
not mean the literature is exhaustive or the evidence is verified.

## Scope

Record:

- included and excluded concepts;
- time and source boundaries;
- required coverage facets;
- assumptions;
- unresolved scope questions.

For ambiguous terms, record the chosen meaning explicitly.

## Queries

Each query requires:

- `id` such as `Q01`;
- `round` and `family`;
- exact `text`;
- `purpose` and target facets;
- provider filters;
- execution status;
- returned and retained counts when executed;
- error reference when failed.

Allowed execution statuses:

- `planned`;
- `succeeded`;
- `empty`;
- `failed`;
- `skipped-duplicate`;
- `blocked-credential`.

Do not overwrite an earlier query when refining it. Add a new query ID and
link it with `derived_from`.

## Candidates

Use:

```text
<source>:<canonical-source-id>
```

Required candidate fields:

- `candidate_id`;
- `status: candidate`;
- `source` and `source_id`;
- `title`;
- `discovered_by`;
- `relevance`;
- `review_state`.

Metadata may include authors, date, year, venue, abstract, TLDR, categories,
citation count, retrieval score, paper URL, PDF URL, DOI, and repository URL.

Do not derive venue, citations, or repository ownership. A paper and PDF URL
may be deterministically derived from an arXiv ID.

`discovered_by` is a list. Each item records:

- query ID;
- provider rank;
- provider score;
- exact returned source ID or version;
- retrieval timestamp.

`execution.raw_result_path` is relative to the repository root and must not
contain parent-directory traversal.

## Relevance

After screening, `relevance` requires:

- label: `core`, `adjacent`, `background`, or `exclude`;
- five 0-to-2 scores from `search-strategy.md`;
- reason;
- basis.

During retrieval, label, scores, and reason may be `null`. Such a run must
remain `needs-review` or `partial`.

Allowed basis values:

- `title-only`;
- `title-and-abstract`;
- `provider-metadata`;
- `manual-note`;
- `null` before screening.

Allowed review states:

- `metadata-only`;
- `abstract-screened`;
- `selected-for-ingest`;
- `excluded`;
- `needs-review`.

An excluded candidate requires `exclusion_reason`.

## Duplicate and version relationships

Use:

- `duplicate_of` only for a manually confirmed duplicate;
- `possible_version_of` for an uncertain preprint/publication relation;
- `alternate_identifiers` to retain exact returned IDs and external IDs.

The retained record merges all query provenance from exact stable-ID
duplicates.

## Coverage and metrics

Each coverage facet records:

- name;
- status: `covered`, `partial`, `missing`, or `not-required`;
- supporting candidate IDs;
- note;
- optional next-query suggestion.

Deterministic metrics include:

- executed queries;
- raw retrieved hits;
- unique candidates;
- duplicate rate;
- candidates by relevance label;
- untriaged candidates;
- candidates missing important metadata;
- new core candidates by round.

Metrics describe the search process, not scientific evidence strength.

## Errors and limitations

Each error records:

- ID;
- timestamp and phase;
- provider and package version when relevant;
- query ID when relevant;
- error class;
- sanitized message;
- effect;
- recovery action;
- recurrence key.

Use recurrence keys to detect deterministic failures across runs. Promote only
repeated, actionable failures into a new rule, validator, script, or Error
Book entry.

Limitations should mention provider coverage, missing citation expansion,
unresolved scope, metadata-only screening, and budget constraints when
applicable.
