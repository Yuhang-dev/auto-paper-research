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

### Budgets and execution totals

`run.budget` defines hard execution boundaries. Supported fields are:

- `max_queries`: maximum query records considered by the run;
- `max_candidates`: maximum total candidate records retained in the run;
- `max_provider_query_calls`: maximum calls to the provider search interface;
- `max_new_unique_candidates`: maximum new candidate identities admitted by
  this execution;
- `provider_max_retries`: retry count per provider query, from 0 through 10;
- `max_rounds`: maximum planned search rounds.

`null` means that the corresponding boundary is not configured by the search
record. A bounded Harness execution may supply a stricter value at runtime;
the effective value must be written back to `run.budget`.

After an execution, `run.execution_totals` records:

- `provider_query_calls`;
- `new_unique_candidates`.

Neither total may exceed its corresponding configured boundary. Provider call
count means calls to `deepxiv_sdk.Reader.search`; it does not claim visibility
into internal HTTP attempts made by the SDK.

Candidate admission is deterministic: queries are processed in run order and
results in provider-rank order. Exact stable-ID or DOI duplicates may still
merge discovery provenance after the new-identity limit is reached, but no new
candidate identity may be admitted.

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
- `execution.effective_size`, the provider result-size cap actually used;
- `execution.budget_skipped_new_count`, the number of otherwise admissible new
  identities skipped by the candidate budget;
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

After a reviewer selects a candidate and places its source PDF in the repository,
`local_pdf_path` may bind it to a repository-relative `.pdf` path. This field is a
handoff to `ingest-paper`; providers must not populate it and search execution must
not download a file implicitly.

After structured screening selects an arXiv candidate, the explicit `ingest` action
may acquire its public PDF when the run has network authorization. The Harness then
adds `local_pdf_path` and a `source_acquisition` mapping containing source URL,
SHA-256, byte size, timestamp, and whether a download occurred. This acquisition is
part of ingest, not provider search.

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
- `ingested`;
- `excluded`;
- `needs-review`.

After successful Wiki publication, the Harness changes `selected-for-ingest` to
`ingested` and records canonical `paper_id`, timestamp, changed Wiki paths, and
diagnostics under the candidate's `ingest` mapping. This prevents the Outer Loop
from repeatedly selecting the same handoff.

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
