---
schema_version: "0.2"
id: "model:{{model_slug}}"
type: model
title: "{{model_title}}"
aliases: []
status: draft
created_at: "{{iso_timestamp}}"
updated_at: "{{iso_timestamp}}"
family: "{{family}}"
parameters: null
source: {}
evidence:
  locator: "{{evidence_locator}}"
  pdf_page: 0
relations: {}
---

# {{model_title}}

## Model family

{{family}}

## Provenance

- Paper: [[paper:{{paper_slug}}]]
- Evidence: {{evidence_locator}}
