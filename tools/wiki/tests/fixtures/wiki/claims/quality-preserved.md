---
schema_version: "0.2"
id: claim:quality-preserved
type: claim
title: Sparse attention preserves RULER quality at 32k
aliases: []
status: verified
facets:
  - quality-metrics
created_at: "2026-08-26T09:00:00+08:00"
updated_at: "2026-08-26T09:00:00+08:00"
statement: The evaluated sparse-window configuration preserves at least 90 percent RULER accuracy at 32k context.
assessment: supported
scope:
  model: model:llama-7b
  benchmark: benchmark:ruler
  context_length: 32768
relations: {}
---

# Sparse attention preserves RULER quality at 32k

Evidence is recorded in [[experiment:alpha-ruler-32k]].
