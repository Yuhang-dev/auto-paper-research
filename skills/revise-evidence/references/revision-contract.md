# Evidence Revision Contract

The runtime supplies one current Wiki entity, its independent verification
feedback, an exact field allow-list, and a page-aware PDF excerpt.

Return one JSON object with this shape:

```json
{
  "entity_id": "method:example",
  "entity_type": "method",
  "paper_id": "paper:example",
  "reason_code": "source-contradiction",
  "source_sha256": "64 lowercase hexadecimal characters",
  "source_pages": [3],
  "rationale": "What the supplied source page establishes and why the old field was wrong.",
  "updates": {
    "definition": "Corrected source-grounded definition",
    "evidence": {
      "pdf_page": 3,
      "paper_page": "3",
      "section": "Method",
      "element": null,
      "description": "Definition stated in the method section."
    }
  }
}
```

Allowed reason codes:

- `source-contradiction`
- `locator-page-mismatch`
- `invalid-locator`

Allowed update fields are only:

- `definition`
- `statement`
- `scope`
- `evidence`

The runtime provides the exact subset permitted for the current target.
Omitted fields remain unchanged. Null is not a deletion mechanism.

Hard gates:

1. Entity ID, entity type, paper ID, reason code, and source hash must match.
2. `source_pages` must be non-empty, unique, and contained in excerpt pages.
3. Every changed fact must be supported by `source_pages`.
4. A revised evidence locator must point to a listed `source_pages` page.
5. Locator-only failures permit only `evidence`.
6. A method contradiction permits only `definition` and `evidence`.
7. A claim contradiction permits only `statement`, `scope`, and `evidence`.
8. Do not alter experiment results, identity, relations, attribution, or status.
9. Do not mark the entity verified. A separate verifier decides that later.
10. Do not infer missing facts or use pages outside the supplied excerpt.

The runtime rejects extra fields and records the previous verification before
publishing the correction transactionally.
