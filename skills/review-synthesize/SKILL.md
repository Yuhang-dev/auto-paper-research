---
name: review-synthesize
description: >
  Update a scientific review's provisional understanding and produce a
  structured synthesis from SourceSkims and citation-ready EvidenceCards. Use
  for uncertainty-driven pivots, non-consensus assessment, and final review drafting.
---

# Review Synthesize

## Purpose

Build a useful scientific answer while keeping provisional navigation,
independent evidence, and interpretation visibly separate.

## Reasoning rules

- Use SourceSkims to identify routes and unanswered questions, never as formal
  report evidence.
- Link every quantitative, comparative, or conclusive statement to supplied
  EvidenceCard IDs.
- Treat configurations from the same paper as one source. SCCA, S2, LongMixed,
  or other within-paper variants cannot establish cross-paper disagreement.
- Call a claim consensus or contested only with at least two independent,
  experimentally comparable sources.
- Compare model, task, context, metric, baseline, hardware, and implementation
  conditions before interpreting differences.
- If evidence is sparse or incomparable, return `insufficient-evidence` or a
  single-source observation.
- Keep unresolved blocking questions explicit even when the search budget is
  exhausted.

## Search pivots

Prioritize the highest-impact open uncertainty. Useful pivots include independent
replication, counter-evidence, missing context-length buckets, engineering
measurements, and official implementation sources. Do not request more papers
only to increase corpus size.

When deterministic gaps are supplied, preserve their evidence boundary and
priority. Use provisional topology to organize the search, while treating
missing facets, single-source conclusions, experimental incomparability,
engineering evidence, and freshness as the reasons for a pivot.

## Final output

Return only the requested structured reasoning or synthesis schema. The caller
validates independent sources and EvidenceCard references, then renders the final
Markdown deterministically. Provisional concepts and relation candidates may
organize sections but cannot support report claims. Do not write Wiki pages or
promotion approvals.
