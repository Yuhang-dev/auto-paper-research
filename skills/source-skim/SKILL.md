---
name: source-skim
description: >
  Triage a paper or official source from metadata and short excerpts for a
  research-review funnel. Use for lightweight relevance assessment and deep-read
  selection; never treat skim output as citation-ready evidence.
---

# Source Skim

## Purpose

Produce a short, provisional navigation record that helps decide whether a
source deserves deep reading.

## Inputs

- the framed research question and scope;
- normalized source metadata;
- title, abstract, search excerpt, or another explicitly supplied short excerpt.

Do not assume access to the full source unless full text is present in the
input.

## Extract

- relevance and exclusion decision;
- likely method family and addressed facets;
- possible contribution and limitations;
- questions that require full-text confirmation;
- whether the source merits deep reading.

## Evidence boundary

- A skim is provisional and is not report evidence.
- Do not emit exact quantitative claims from a search snippet or abstract.
- Treat ResearchGate, Academia.edu, Scribd, and similar mirrors as navigation
  clues only. They must not be selected for evidence extraction; locate the
  paper's arXiv, DOI, publisher, proceedings, or OpenReview record instead.
- Do not invent a PDF page, table, figure, section, implementation detail, or
  experimental condition.
- Phrase uncertainty explicitly when the excerpt is incomplete.
- Prefer exclusion over forced relevance when the source is outside scope.
- A survey may guide taxonomy and citation chasing, but primary studies should
  receive the scarce experimental Deep Read slots.

## Output

Return only the requested `SourceSkim` or screening schema. Keep the summary
short enough for batch comparison. The deterministic funnel, deduplication, and
budget selection are handled by code, not by this Skill.
