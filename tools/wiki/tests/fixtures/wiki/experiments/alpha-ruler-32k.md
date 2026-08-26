---
schema_version: "0.2"
id: experiment:alpha-ruler-32k
type: experiment
title: Alpha on RULER at 32k
aliases: []
status: verified
facets:
  - quality-metrics
  - synthetic-vs-real-tasks
created_at: "2026-08-26T09:00:00+08:00"
updated_at: "2026-08-26T09:00:00+08:00"
paper: paper:alpha
method:
  - method:sparse-window
model:
  - model:llama-7b
benchmark: benchmark:ruler
context_length: 32768
sparsity:
  target: attention
  ratio: 0.75
metric:
  name: accuracy
  unit: percent
result:
  value: 91.2
  direction: higher-is-better
evidence:
  locator: Table 2, row RULER-32k
  page: 7
relations:
  supports:
    - claim:quality-preserved
---

# Alpha on RULER at 32k

This experiment evaluates [[method:sparse-window]] with [[model:llama-7b]]
on [[benchmark:ruler]] and supports [[claim:quality-preserved]].
