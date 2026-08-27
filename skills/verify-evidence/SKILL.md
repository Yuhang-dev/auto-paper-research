---
name: verify-evidence
description: >
  Verify draft LLM-Wiki papers, claims, experiments, and non-consensus
  assessments against their cited source material. Use when promoting draft
  evidence to verified, checking quantitative locators and conditions, or
  recording why an item remains needs-review. Do not use for paper discovery
  or initial extraction.
---

# Verify Evidence

## Purpose

Decide whether a structured Wiki record is supported by the cited source under
the conditions recorded in that record.

Verification is not a second summary. It is a controlled lifecycle transition:

```text
draft / needs-review
  -> source and locator checks
  -> semantic comparison
  -> verified OR needs-review with an explicit reason
```

## Required context

1. Read `references/verification-contract.md`.
2. Read `../ingest-paper/references/evidence-policy.md` when verifying a paper
   bundle.
3. Inspect the current Wiki schema and structured relations.
4. Use the cited local PDF and page markers; do not substitute an abstract.

## Workflow

### 1. Resolve the verification target

Verify one bounded target at a time:

- an ingested paper and its directly linked entities; or
- one draft non-consensus assessment and its cited claims/experiments.

Do not mix unrelated papers in one paper verification bundle.

### 2. Run deterministic prechecks

Before semantic judgment, require:

- canonical IDs and resolvable relation targets;
- required schema fields;
- a repository-local PDF for paper verification;
- in-range PDF page locators for quantitative experiments;
- preserved model, benchmark, context, metric, result, and sparsity conditions;
- verified source claims and experiments for assessment verification.

A failed precheck is not repaired by guessing. Keep the entity `needs-review`.

### 3. Compare source and record

For each entity, return one verdict:

- `supported`: the cited source supports the structured record under its stated
  conditions;
- `contradicted`: the cited source conflicts with a material field or statement;
- `insufficient`: the supplied source excerpt cannot establish support.

Include a concise rationale and exact PDF viewer pages used.

For quantitative experiments, verify the result and all material conditions.
Matching a number without its baseline, metric, context length, model, or
benchmark is insufficient.

### 4. Verify claims conservatively

A claim may become `verified` only when:

- its statement is supported;
- at least one structured evidence edge resolves to a verified experiment; and
- author attribution is not confused with current-agent inference.

Do not mark a scientifically plausible claim verified merely because it sounds
consistent with the paper.

### 5. Verify assessments independently

Confirm that the assessment compares evidence addressing a sufficiently aligned
question. Check model, benchmark, context, metric, and method conditions before
accepting `contested`.

Different conditions normally imply `insufficient-evidence`, not a
contradiction.

### 6. Publish through the deterministic writer

The runtime owns status changes, timestamps, provenance fields, shadow-Wiki
validation, atomic replacement, index rebuilding, and rollback. The model must
not write Markdown or choose file paths.

## Decision rules

- Missing or invalid locator: retain `needs-review`.
- Extracted number absent from the cited page: retain `needs-review` and record
  the mismatch.
- Material condition omitted: retain `needs-review`.
- Source conflict: record `contradicted`; do not silently rewrite the record.
- Assessment with unverified inputs: do not mark the assessment verified.
- Existing verified entity: do not downgrade it during an unrelated pass.

## Self-check

- [ ] Every promoted entity has a source-backed verdict.
- [ ] Quantitative values retain their experimental conditions.
- [ ] PDF viewer page numbers are in range.
- [ ] Claim evidence edges resolve to verified experiments.
- [ ] Non-consensus judgments compare aligned conditions.
- [ ] Failed or insufficient checks remain visible as `needs-review`.
- [ ] No source file or credential was placed in model output.
- [ ] The staged Wiki has zero validation errors.

For a saved semantic draft, run `scripts/validate_verification_draft.py` with
`--kind paper` or `--kind assessment` before runtime publication.

## Output

Produce one structured verification draft for the runtime. The runtime may
publish zero or more verified lifecycle transitions and explicit unresolved
verification records. Do not create unrelated Wiki entities.
