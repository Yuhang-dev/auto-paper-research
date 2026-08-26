---
name: ingest-paper
description: >
  Ingest academic papers into the LLM research wiki by extracting
  metadata, research problems, methods, claims, experiments,
  limitations, and concept links. Use when adding a new paper,
  updating an existing paper page, or converting a paper into
  structured wiki knowledge.
---

# Ingest Paper

## Purpose

Convert one academic paper into structured, evidence-grounded
LLM-Wiki knowledge.

The goal is not merely to summarize the paper.

The goal is to:

1. create or update the paper page;
2. connect the paper with existing concepts;
3. record claims and experimental evidence;
4. preserve enough provenance for later verification.

## Required context

Before writing wiki content:

1. Read `references/wiki-schema.md`.
2. Read `references/evidence-policy.md`.
3. Use `assets/paper-template.md` when creating a new paper page.
4. Use `assets/concept-template.md` only when a new concept page is necessary.

Treat `references/` and `assets/` paths as relative to this Skill directory.
Treat `wiki/` paths as relative to the repository root.

## Workflow

### 1. Identify the paper

Extract:

- title;
- authors;
- year;
- venue;
- paper URL or identifier.

Check whether the paper already exists in the wiki.

If it exists, update the existing page instead of creating a duplicate.

### 2. Understand the research problem

Identify:

- problem addressed;
- motivation;
- assumptions;
- scope.

Do not copy the abstract as the problem statement.

### 3. Extract the method

Identify:

- core method;
- important components;
- how it differs from prior work.

Before creating a concept page, search the existing wiki for:

- canonical name;
- aliases;
- semantically equivalent concepts.

Reuse existing concepts whenever possible.

### 4. Extract claims

Separate:

- author claims;
- experimentally supported claims;
- interpretation by the current agent.

Do not represent agent inference as an author claim.

Follow `references/evidence-policy.md`.

### 5. Extract experiments

Record important:

- models;
- datasets or benchmarks;
- context lengths;
- baselines;
- metrics;
- experimental settings;
- quantitative results.

Preserve evidence locations whenever practical.

### 6. Record limitations

Prefer limitations explicitly stated or directly supported by
the paper.

Clearly mark inferred limitations as analysis rather than
paper claims.

### 7. Link the wiki

Add links to:

- concepts;
- methods;
- benchmarks;
- related papers.

Do not create duplicate concept pages simply because different
papers use different terminology.

### 8. Write the paper page

Create or update the page according to:

`assets/paper-template.md`

Set a newly ingested or updated page to `draft` or `needs-review`.
Do not set it to `verified`; verification belongs to a separate workflow.

Replace or remove every template placeholder before finishing.

Do not invent fields or change the wiki schema unless explicitly
requested.

## Decision rules

### Existing concept

If an equivalent concept already exists:

- reuse the existing page;
- add an alias if appropriate.

### New concept

Create a new concept page only when:

- no equivalent concept exists; and
- the concept is important enough to be reused across papers.

### Uncertain evidence

If a claim cannot be verified:

- retain the uncertainty explicitly;
- do not fabricate evidence;
- do not mark it verified.

## Self-check

Before finishing, verify:

- [ ] Required paper metadata is present.
- [ ] The paper does not duplicate an existing paper ID.
- [ ] Existing concepts were checked before creating new pages.
- [ ] Important empirical claims have evidence locations.
- [ ] Quantitative results preserve their experimental conditions.
- [ ] Agent inference is not presented as an author claim.
- [ ] Wiki links point to intended pages.
- [ ] Aliases do not create duplicate concepts.
- [ ] No unresolved template placeholders remain.
- [ ] The output follows `references/wiki-schema.md`.

## Output

The result should consist of:

- one created or updated paper page;
- zero or more necessary concept pages;
- links to existing wiki entities.

Do not create unrelated wiki pages.
