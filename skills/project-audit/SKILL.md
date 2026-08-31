---
name: project-audit
description: >
  Audit an official open-source project or repository for a scientific review,
  including implementation scope, license, version, activity, reproducibility,
  and claimed performance. Use for project skims or located project EvidenceCards.
---

# Project Audit

## Purpose

Assess whether an open-source project materially supports a method's engineering
or reproducibility claims.

## Inspect

- owner/repository and canonical project URL;
- relationship to a paper or method;
- implemented training, prefill, decode, KV-cache, or kernel path;
- supported models, hardware, dependencies, and documented constraints;
- license, release/version evidence, maintenance signals, and reproducibility
  artifacts;
- reported latency, throughput, memory, and quality results with conditions.

## Rules

- Prefer repository metadata, tagged releases, source code, and official
  documentation over third-party summaries.
- A README claim is an author/project claim, not independently verified
  performance.
- Do not infer kernel support, hardware compatibility, or maintained status from
  the repository name.
- For exact engineering claims, require an official locator and preserve commit,
  tag, or content hash when supplied.
- Skim-mode output remains non-citable; deep-read mode may emit EvidenceCards
  only from located official material.

## Output

Return the schema requested by the caller. Repository normalization, REST API
calls, deduplication, and maturity aggregation are handled deterministically.
