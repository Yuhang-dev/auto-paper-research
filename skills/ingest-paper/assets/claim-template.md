---
schema_version: "0.2"
id: "claim:{{claim_slug}}"
type: claim
title: "{{claim_title}}"
aliases: []
status: draft
created_at: "{{iso_timestamp}}"
updated_at: "{{iso_timestamp}}"
statement: "{{statement}}"
assessment: open
scope: {}
relations: {}
---

# {{claim_title}}

## Statement

{{statement}}

## Attribution and evidence

- Attribution: {{attribution}}
- Evidence type: {{evidence_type}}
- Evidence status: {{evidence_status}}
- Locator: {{evidence_locator}}
- Source paper: [[paper:{{paper_slug}}]]
