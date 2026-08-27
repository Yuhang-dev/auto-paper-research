---
name: ingest-paper
description: >
  Convert a selected academic-paper candidate and repository-local PDF into
  evidence-grounded LLM-Wiki paper, method, benchmark, model, claim, and
  experiment drafts. Use when ingesting a selected search result, refreshing
  structured knowledge from a paper, or preparing Wiki evidence for later
  verification.
---

# Ingest Paper

## Purpose

Convert one paper into linked, provenance-preserving Wiki knowledge. This is an
extraction workflow, not a free-form summary and not an independent verification.

The semantic extractor returns a `PaperIngestDraft`. It must not write Markdown
directly. Deterministic harness code resolves identities, renders pages, validates
an isolated shadow Wiki, and publishes source pages transactionally.

## Required context

Before extracting:

1. Read `references/wiki-schema.md`.
2. Read `references/evidence-policy.md`.
3. Read `references/ingest-draft-schema.md`.
4. Consult the existing Wiki catalog supplied by the harness before proposing a
   reusable entity.

Assets document the target page shapes. The deterministic writer, rather than the
model, applies them.

## Preconditions

Require:

- one candidate with `review_state: selected-for-ingest`;
- an explicit repository-relative `local_pdf_path`;
- candidate identity and discovery provenance;
- page-aware text extracted from that PDF.

Do not ingest from an abstract alone when the output would contain experimental
claims. If the source or central evidence is incomplete, use `needs-review` and
omit unsupported entities.

## Workflow

### 1. Resolve paper identity

Extract title, authors, year, venue, arXiv ID, DOI, and canonical URLs. Prefer DOI,
then arXiv ID, then normalized title for duplicate detection. Preserve unknown
values as `null`; never guess.

When the paper already exists, return matching identifiers so the deterministic
compiler reuses its canonical paper ID.

### 2. Extract problem and scope

Record the research problem, motivation, assumptions, and empirical scope in your
own concise wording. Do not copy the abstract as the problem statement. Keep
generality no broader than the paper's evaluated models, tasks, and settings.

### 3. Resolve reusable entities

For each method, benchmark, and model:

1. search the supplied catalog by canonical ID, title, and alias;
2. set `existing_id` when an equivalent typed entity exists;
3. otherwise provide a stable local key and lowercase kebab-case `proposed_slug`;
4. attach a page-aware evidence locator.

Do not create two local keys for aliases of the same entity. This V1 path does not
create concept pages; record a missing concept-normalization need as an open
question for a later Wiki-link workflow.

### 4. Extract atomic claims

Separate:

- author claims;
- claims supported by a reported experiment;
- current-agent interpretation.

Record attribution, evidence type, evidence status, scope, and locator separately.
Never present agent inference as an author claim. An `experiment-supported` claim
must be referenced by at least one experiment.

### 5. Extract experiments

Represent one material result per experiment record. Preserve:

- method, model, and benchmark local keys;
- context length;
- sparsity target, pattern, ratio, or budget when reported;
- metric name, direction, and unit;
- result value, baseline, and comparison;
- PDF page plus table, figure, section, or appendix locator;
- supported and contradicted claim keys.

Do not combine rows from different settings into a synthetic result.

### 6. Record limitations

Keep author-reported limitations separate from agent analysis. Give inferred
limitations a supporting locator whenever possible. Missing comparisons are not
negative results unless the paper actually reports them.

### 7. Return the structured draft

Return exactly one `PaperIngestDraft` matching
`references/ingest-draft-schema.md`. The `candidate_id` must match the selected
candidate. Use only `draft` or `needs-review`; ingestion must never assign
`verified`.

### 8. Deterministic publication

The harness will:

1. resolve or allocate canonical IDs;
2. reuse existing typed entities;
3. render V0.2 Markdown pages;
4. stage them in an isolated Wiki copy;
5. rebuild the graph and reject every schema error;
6. atomically publish and rebuild generated indexes;
7. roll back source pages if publication fails.
8. mark the source candidate `ingested` and record its canonical paper ID only
   after Wiki publication succeeds.

Do not bypass this path with direct file writes.

## Decision rules

### Uncertain evidence

- Use `partial` when only part of the wording or scope is supported.
- Use `unlocated` when no adequate locator was found.
- Omit unsupported quantitative experiments.
- Use `needs-review` when identity, source access, or central evidence remains
  unresolved.

### Existing versus new entity

- Reuse only a canonical entity of the expected type.
- Do not treat a same-named concept as a method automatically.
- Do not overwrite an existing source page merely to add extraction output.
- Stable child IDs are derived from paper identity, local key, and content.

### Verification boundary

`located` means the extractor found evidence. It does not mean a second agent or
human verified the evidence. Only the separate verification workflow may set a
Wiki page to `verified` or a claim assessment to `supported`, `contested`, or
`refuted`.

## Self-check

- [ ] Candidate ID exactly matches the selected search record.
- [ ] Metadata is present or explicitly unknown.
- [ ] Existing typed entities were checked before proposing new ones.
- [ ] Every reusable entity has an evidence locator.
- [ ] Every quantitative result preserves its conditions and locator.
- [ ] Every experiment-supported claim is linked from an experiment.
- [ ] Agent analysis is not attributed to the paper.
- [ ] No unsupported result, page number, identifier, or link was invented.
- [ ] Output follows the structured draft and Wiki V0.2 contracts.
- [ ] No page is marked `verified`.

## Output

One validated `PaperIngestDraft`. Publication may create zero or one paper page
and zero or more method, benchmark, model, claim, and experiment pages. Existing
entities may be reused. Do not create unrelated pages.
