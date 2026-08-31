---
schema_version: "0.2"
id: "benchmark:{{benchmark_slug}}"
type: benchmark
title: "{{benchmark_title}}"
aliases: []
status: draft
created_at: "{{iso_timestamp}}"
updated_at: "{{iso_timestamp}}"
task: "{{task}}"
metrics: []
source: {}
evidence:
  locator: "{{evidence_locator}}"
  pdf_page: 0
relations: {}
---

# {{benchmark_title}}

## Task

{{task}}

## Provenance

- Paper: [[paper:{{paper_slug}}]]
- Evidence: {{evidence_locator}}
