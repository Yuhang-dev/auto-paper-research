---
name: search-paper
description: >
  Plan and execute traceable academic-paper searches, then build a
  deduplicated candidate corpus with relevance labels, coverage analysis,
  and search gaps. Use when starting a literature review, expanding from
  seed papers, refreshing a topic corpus, or filling known coverage gaps.
  Do not use for full-paper evidence extraction or verified Wiki claims.
---

# Search Paper

## Purpose

Turn one research question into a traceable, reviewable candidate-paper
corpus.

The goal is not merely to return a list of papers.

The goal is to:

1. translate the research question into a bounded search plan;
2. retrieve candidates through complementary query families;
3. preserve query and result provenance;
4. deduplicate and triage candidates consistently;
5. expose coverage gaps for the next search loop.

Every paper produced by this Skill remains a `candidate` until a separate
ingestion and evidence workflow reads the paper itself.

## Required context

Before searching:

1. Read `references/search-strategy.md`.
2. Read `references/deepxiv-sdk.md`.
3. Read `references/search-output-schema.md`.
4. Use `assets/search-run-template.yaml` for the search-run record.
5. Read any supplied topic scope, seed-paper list, previous search run, or
   coverage report.
6. In the Fast Research Loop, use only the supplied deterministic Wiki identity
   index for duplicate checks; do not load Wiki page contents into the search
   context.

Treat `references/`, `assets/`, and `scripts/` paths as relative to this Skill
directory. Treat `wiki/` and `research/` paths as relative to the repository
root.

## Input

Required:

- a research question or survey topic.

Optional:

- inclusion and exclusion criteria;
- time, venue, category, author, or citation constraints;
- seed papers and known aliases;
- required coverage facets;
- maximum queries, candidates, or search rounds;
- path to an earlier search-run record.

If optional constraints are absent, choose conservative defaults, record them
in the search run, and keep them reversible.

## Workflow

### 1. Frame the search problem

Write down:

- the primary research question;
- scope boundaries;
- included and excluded meanings of ambiguous terms;
- target years or venues, if any;
- evidence and implementation facets the review must cover.

Do not silently merge distinct meanings such as model sparsity, attention
sparsity, KV-cache sparsity, and expert sparsity.

### 2. Check existing knowledge

Search the Wiki and prior search runs before querying an external provider.

Collect:

- known paper identifiers;
- canonical concept names and aliases;
- seed papers;
- previously used queries;
- known coverage gaps.

Do not rerun an identical query with identical filters unless this is an
explicit time-based refresh.

### 3. Create the search run

Initialize a run with:

```powershell
conda run -n base python skills/search-paper/scripts/new_search_run.py `
  --topic-slug "<topic-slug>" `
  --question "<research-question>"
```

Use the path printed by the script. Fill the scope and query matrix before
network execution.

### 4. Build the query matrix

Create four to eight high-information queries for the first pass.

The matrix should normally include:

- direct topic wording;
- aliases and terminology variants;
- mechanism or architecture terms;
- long-context tasks or benchmark terms;
- efficiency, scaling, or system-bottleneck terms;
- limitation, failure, contradiction, or strong-baseline terms;
- open-source or implementation terms when engineering evidence matters.

Record the purpose and expected coverage facet for every query.

When structured research gaps are supplied, target the three highest-priority
open gaps. Keep at least one primary-paper query, use GitHub for a project gap,
and include a disconfirming query when a candidate non-consensus hypothesis is
still open.

Do not create dozens of minor paraphrases before observing first-pass results.

### 5. Execute discovery search

Use the installed `deepxiv-sdk` from Conda `(base)`:

```powershell
conda run -n base python skills/search-paper/scripts/deepxiv_search.py `
  --run "research/<topic-slug>/search-runs/<run-id>.yaml"
```

Follow `references/deepxiv-sdk.md`.

For this Skill:

- use `deepxiv_sdk.Reader.search` for paper discovery;
- do not use a DeepXiv MCP integration;
- do not use agentic answers as verified survey evidence;
- pass credentials only through `DEEPXIV_TOKEN`;
- preserve exact queries, filters, returned ranks, scores, and identifiers;
- keep provider metadata separate from current-agent judgment;
- treat provider-call, new-candidate, result-size, and retry limits as hard
  execution boundaries;
- preserve duplicate discovery provenance without admitting a new identity
  after the candidate limit is reached.

Run broad discovery before adding narrow filters. Change one major constraint
at a time when diagnosing weak recall.

### 6. Normalize and deduplicate

The search script normalizes stable identifiers, merges repeated discovery
provenance, links possible versions conservatively, and recomputes process
metrics.

Deduplication priority is:

1. exact source plus canonical source identifier;
2. DOI or another stable identifier;
3. normalized title plus year as a possible-version signal;
4. manual confirmation for preprint, workshop, and conference versions.

Do not merge two papers only because their titles are similar.

### 7. Triage relevance

Label each candidate:

- `core`;
- `adjacent`;
- `background`;
- `exclude`.

Use the rubric in `references/search-strategy.md` and include a short reason.

Assign one provisional source role:

- `survey`;
- `primary-study`;
- `benchmark`;
- `reproduction`;
- `project`;
- `background`.

The deterministic funnel applies role targets and fills missing roles by stable
ranking. Do not change relevance scores merely to satisfy a quota.

The label is a screening judgment based on metadata and, when available, the
abstract. It is not an evidence-verification status.

Never infer experimental results, limitations, or paper conclusions from a
title or abstract.

### 8. Run the gap-directed loop

After each pass, inspect:

- new unique and new `core` candidates;
- duplicate rate;
- coverage by required facet;
- missing metadata;
- unresolved terminology;
- provider and query failures.

Use uncovered facets and repeated false positives to design the next pass. Do
not broaden every facet at once.

Treat orphan nodes as search hints only. A gap becomes actionable through
missing evidence, independence, comparability, engineering coverage, or
freshness rather than graph degree alone.

Stop according to `references/search-strategy.md`. A zero-result query is not
evidence that no relevant literature exists.

### 9. Expand from seeds when needed

Use limited backward or forward citation expansion only when a seed is clearly
central or a required coverage gap remains.

Record the seed, direction, provider, number screened, and number retained. If
no citation-graph provider is configured, record that gap; do not pretend
DeepXiv retrieval performed citation expansion.

### 10. Validate the run

Run:

```powershell
conda run -n base python skills/search-paper/scripts/validate_search_run.py `
  "research/<topic-slug>/search-runs/<run-id>.yaml"
```

Use `--fix-metrics` only to recompute deterministic metrics. Resolve remaining
warnings before marking a search run complete.

### 11. Write the search result

The canonical output is:

`research/<topic-slug>/search-runs/<run-id>.yaml`

It must preserve:

- scope and assumptions;
- query matrix and filters;
- normalized candidates and discovery provenance;
- relevance labels and screening reasons;
- coverage status and gaps;
- errors, limitations, and stop reason.

Do not create paper or concept Wiki pages during search. Hand screened and
selected candidates to `ingest-paper`; the deterministic runtime caps automatic
selection and records the screening basis.

### 12. Feed the improvement loop

Record failures inside the search run first.

When the same deterministic failure recurs across runs, promote it to the
project Error Book and propose:

- a query-planning rule;
- a normalization or validation rule;
- a reusable script or function;
- a revision to this Skill.

Do not silently modify this Skill from one noisy result.

## Decision rules

### Missing DeepXiv token

If `DEEPXIV_TOKEN` is unavailable:

- save the search plan;
- mark execution `blocked-credential`;
- do not auto-register an account;
- do not write a token into the repository, commands, logs, or output.

### Empty results

If a query returns no results:

- verify terminology and source;
- remove one restrictive filter at a time;
- try a documented alias query;
- retain the zero-result query in provenance.

### Excessive false positives

If results are too broad:

- add one discriminating mechanism, task, or scope term;
- prefer a new query family over a long Boolean expression;
- document the false-positive pattern for the next loop.

### Incomplete metadata

If provider metadata is absent:

- preserve the stable identifier and provenance;
- use `null` for unknown fields;
- do not guess authors, venue, year, citations, or repository URLs.

### Conflicting versions

If a preprint and published version may be the same work:

- retain both identifiers;
- mark `possible_version_of` until manually confirmed;
- do not erase version-specific provenance.

## Self-check

Before finishing, verify:

- [ ] The research question and scope boundaries are explicit.
- [ ] Ambiguous sparsity meanings are separated.
- [ ] Four to eight purposeful first-pass queries were considered.
- [ ] A limitation or disconfirming query family was considered.
- [ ] Every executed query preserves its exact filters and status.
- [ ] Execution totals do not exceed the configured hard boundaries.
- [ ] Effective provider sizes and budget-skipped candidates are recorded.
- [ ] Candidates have stable identifiers whenever available.
- [ ] Duplicates merge provenance rather than lose it.
- [ ] Relevance labels include reasons and screening basis.
- [ ] Metadata screening is not presented as verified paper evidence.
- [ ] Coverage gaps and the stop reason are explicit.
- [ ] No credentials or secrets appear in project files.
- [ ] No paper or concept Wiki page was created by this Skill.
- [ ] The validator passes and the output follows the schema.

## Output

The result should consist of:

- one search-run YAML record;
- one deduplicated candidate set embedded in that record;
- a coverage and gap assessment;
- zero or more Error Book candidates for repeated failures.

Do not produce verified claims or unrelated Wiki pages.
