---
schema_version: "0.2"
id: method:sparse-window
type: method
title: Sparse Window Attention
aliases:
  - Window Sparse Attention
status: draft
created_at: "2026-08-26T09:00:00+08:00"
updated_at: "2026-08-26T09:00:00+08:00"
definition: Restrict each query token to a local attention window plus selected global tokens.
sparsity:
  target: attention
  pattern: structured
implementations:
  - https://example.org/alpha
relations:
  instance_of:
    - concept:attention-sparsity
---

# Sparse Window Attention

An instance of [[concept:attention-sparsity]].
