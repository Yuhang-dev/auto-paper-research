---
schema_version: "0.2"
id: assessment:{{assessment_slug}}
type: assessment
title: "{{assessment_question}}"
aliases: []
status: draft
created_at: "{{created_at_iso8601}}"
updated_at: "{{updated_at_iso8601}}"
facets:
  - limitations-and-counter-evidence
question: "{{assessment_question}}"
result: insufficient-evidence
claim_ids: []
evidence_ids: []
method_family: null
benchmark_ids: []
rationale: "{{evidence_grounded_rationale}}"
verified: false
relations: {}
---

# {{assessment_question}}

## Scope

{{scope}}

## Evidence considered

{{evidence_summary}}

## Rationale

{{evidence_grounded_rationale}}
