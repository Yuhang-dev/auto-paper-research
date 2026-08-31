---
name: revise-evidence
description: >
  Revise one Wiki method or claim retained by verify-evidence because its
  content contradicts the source or its evidence locator points to the wrong
  PDF page. Use only after independent verification has recorded actionable
  feedback and a bounded semantic correction is required before re-verification.
---

# Revise Evidence

## Purpose

Correct one evidence-grounded Wiki entity without weakening verification.

This Skill is a repair step between two independent verification passes:

```text
needs-review + verifier feedback
  -> bounded source-grounded revision
  -> draft
  -> verify-evidence again
```

The reviser does not mark anything verified.

## Required context

Before proposing a correction:

1. Read `references/revision-contract.md`.
2. Inspect the current entity and its retained `verification` record.
3. Use only the supplied page-aware PDF excerpt and source hash.
4. Respect the runtime-provided field allow-list.

## Eligibility

Revise an entity only when all are true:

- type is `method` or `claim`;
- status is `needs-review`;
- `verify-evidence` retained source hash, pages, verdict, and rationale;
- the failure is a source contradiction, locator-page mismatch, or invalid locator;
- fewer than two prior revisions are recorded;
- the same local PDF used by verification is available.

Do not use this Skill for missing experiment edges, missing paper sources,
schema migration, discovery, or unsupported inference. Route those problems to
ingest, search, or human review.

## Workflow

1. Confirm the exact entity, paper, reason code, and PDF hash.
2. Read only the supplied excerpt pages.
3. Correct the smallest allowed set of fields.
4. Cite every used PDF page in `source_pages`.
5. Return one structured revision draft following the contract.
6. Let the deterministic runtime validate and publish the change.
7. Leave the entity as `draft` for a fresh `verify-evidence` pass.

## Decision rules

### Locator failure

Change only `evidence`. The new locator page must be present in both
`source_pages` and the supplied excerpt.

### Source contradiction

For a method, change only `definition` and/or `evidence`.

For a claim, change only `statement`, `scope`, and/or `evidence`.

Do not change identity, relations, attribution, experiment results, lifecycle
status, or prior history.

## Output

Return exactly one `EvidenceRevisionDraft` JSON object. Do not write Wiki pages
directly. The runtime records the prior verification in `revision_history`,
resets status to `draft`, publishes transactionally, and schedules independent
verification.

## Self-check

- [ ] The target and PDF hash match the verifier feedback.
- [ ] The reason is eligible for semantic revision.
- [ ] Only allowed fields changed.
- [ ] Every source page was supplied in the excerpt.
- [ ] No evidence gate or experimental result was relaxed.
- [ ] The output does not claim that the entity is verified.
- [ ] This is no more than the second revision attempt.
