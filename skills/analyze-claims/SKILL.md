---
name: analyze-claims
description: >
  Compare verified LLM-Wiki claims and experiments to produce evidence-grounded
  non-consensus assessments. Use when checking whether papers agree, conflict,
  or address insufficiently aligned conditions. Do not use on unverified
  candidate metadata or to verify source extraction.
---

# Analyze Claims

## Purpose

Turn verified claim and experiment records into a bounded assessment of one
research question:

- `supported-consensus`;
- `contested`;
- `insufficient-evidence`.

The objective is to expose non-consensus structure without manufacturing a
conflict target.

## Required context

1. Read `references/comparison-policy.md`.
2. Inspect verified claim, experiment, benchmark, method, model, and paper
   entities supplied by the runtime.
3. Inspect existing assessment signatures to avoid duplicate analyses.

## Workflow

### 1. Select an answerable comparison

Choose a narrow question supported by the supplied verified records. Prefer a
comparison that helps the current research gap.

Do not combine unrelated claims merely to create an assessment.

### 2. Align conditions

Compare at least:

- intervention or method family;
- model and scale;
- benchmark and task type;
- context length;
- metric and direction;
- sparsity target or budget;
- prefill versus decode phase;
- hardware and implementation when the conclusion is about efficiency.

Record important mismatches in the rationale.

### 3. Classify the result

Use `supported-consensus` only when aligned evidence supports compatible scoped
conclusions.

Use `contested` only when sufficiently aligned evidence supports materially
incompatible conclusions. Different benchmarks, models, context ranges, or
engineering stacks are normally not a contradiction by themselves.

Use `insufficient-evidence` when the records are too sparse or conditions are
not comparable. This is a valid research result.

### 4. Preserve provenance

Return only canonical claim, experiment, and benchmark IDs from the supplied
bundle. Explain the reasoning without inventing results or filling missing
conditions.

### 5. Publish for independent verification

The deterministic runtime assigns the assessment ID, renders the Wiki page,
validates all relations, and publishes it with:

```yaml
status: needs-review
verified: false
```

`analyze-claims` never self-verifies its assessment. `verify-evidence` performs
that separate transition.

## Decision rules

- Unverified input: exclude it from analysis.
- Fewer than one claim and one experiment: no assessment target is ready.
- Duplicate evidence/question signature: do not create another page.
- Apparent conflict caused by condition mismatch: use `insufficient-evidence`.
- No reliable conflict found: return consensus or insufficient evidence; do not
  force `contested`.

## Self-check

- [ ] Every cited claim and experiment is verified.
- [ ] The question is narrower than the overall survey topic.
- [ ] Material conditions were compared explicitly.
- [ ] `contested` is not based only on different experimental settings.
- [ ] IDs come from the supplied bundle.
- [ ] The assessment is novel relative to existing signatures.
- [ ] The output remains `needs-review` pending independent verification.

For a saved semantic draft, run
`scripts/validate_assessment_draft.py <draft.json>` before publication.

## Output

Return one structured non-consensus assessment draft. The runtime creates one
assessment page and no unrelated Wiki entities.
