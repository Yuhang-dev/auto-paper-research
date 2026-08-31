---
name: evidence-extract
description: >
  Extract citation-ready atomic EvidenceCards from deeply read papers or
  official technical sources. Use only when source text and stable locators are
  available; do not use metadata or skim notes as formal evidence.
---

# Evidence Extract

## Purpose

Turn selected full-text material into small, traceable evidence units for a
scientific review.

## Evidence requirements

Every card must preserve:

- source identity, canonical URL, version, and supplied content hash;
- one atomic author claim, result, limitation, or implementation fact;
- the distinction between author statement and current-agent interpretation;
- applicable model, method, benchmark, task, context length, metric, and value
  when present;
- a real locator: PDF page plus table/figure/section, or a stable official-web
  section;
- whether it supports or opposes a current understanding claim.

## Rules

- Extract only what the supplied source supports.
- Do not extract citation-ready evidence from ResearchGate, Academia.edu,
  Scribd, or another secondary mirror. Resolve the primary paper first.
- Preserve experimental conditions with every number or comparison.
- Do not compare two systems unless the source reports comparable settings.
- Do not convert absence of evidence into a negative result.
- Do not label an item cross-checked or verified merely because it was extracted.
- Exclude useful-looking statements that lack a usable locator.
- A static official Web page may support metadata or author discussion, but it
  must not substitute for a paper PDF when making quantitative experimental
  claims. Page-level Web evidence must use a section found in the supplied
  content or the canonical page URL.
- Keep different experiments and conclusions in separate cards.

## Output

Return only the requested `EvidenceExtraction` schema. Source hashing,
independent-source checks, citation validation, and report rendering are
deterministic code responsibilities.
