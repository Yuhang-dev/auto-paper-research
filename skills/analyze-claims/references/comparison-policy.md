# Claim Comparison Policy

## Required structured output

Return:

- `question`;
- `result`: `supported-consensus`, `contested`, or `insufficient-evidence`;
- one or more canonical `claim_ids`;
- one or more canonical `evidence_ids`;
- optional `method_family`;
- zero or more canonical `benchmark_ids`;
- `rationale`;
- `condition_alignment` entries for the dimensions actually compared.

The runtime creates the canonical assessment ID from a stable content
fingerprint. Do not propose a path or lifecycle status.

## Alignment dimensions

Each alignment entry contains:

- `dimension`;
- `status`: `aligned`, `partially-aligned`, `mismatched`, or `unknown`;
- `values`: the compared values as recorded in the Wiki;
- `note`.

For a `contested` result, at least the research question, task/benchmark,
metric semantics, and intervention scope must be sufficiently aligned. If a
material dimension is mismatched and explains the outcome difference, classify
the evidence as insufficient instead.

## Duplicate policy

An assessment is a duplicate when the normalized question, result, sorted claim
IDs, and sorted evidence IDs have the same fingerprint as an existing page.
The runtime rejects duplicate fingerprints even when wording differs slightly.
