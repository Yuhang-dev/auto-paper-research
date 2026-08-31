---
name: wiki-link
description: >
  Resolve staged paper entities against the existing Markdown Wiki and publish
  canonical links without duplicating methods, concepts, benchmarks, models, or
  papers. Use only during explicit staged-to-Wiki promotion.
---

# Wiki Link

## Purpose

Turn an approved, structured paper draft into canonical Wiki relationships while
preserving the Markdown Wiki as source of truth.

## Rules

- Search canonical IDs, aliases, titles, DOI, arXiv ID, and repository identity
  before proposing a new entity.
- Reuse an equivalent entity and add an alias when appropriate.
- Do not merge merely similar methods whose mechanisms or scope differ.
- Preserve typed links among paper, method, model, benchmark, experiment, and
  claim entities.
- Run schema, relation, backlink, locator, and duplicate checks in a shadow
  index before publication.
- Publish the complete page set atomically; on any exception or user interrupt,
  roll back the transaction and re-raise.
- Never mark a draft verified merely because its links resolve.

The caller owns deterministic resolution, rendering, validation, rollback, and
publication. This Skill does not authorize Wiki writes by itself; the user must
explicitly run the staged publication command.
