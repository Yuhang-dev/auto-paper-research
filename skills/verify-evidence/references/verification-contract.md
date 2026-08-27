# Verification Contract

## Semantic draft

Paper verification returns:

- the canonical `paper_id`;
- one decision for every supplied entity ID;
- `verdict`: `supported`, `contradicted`, or `insufficient`;
- a concise rationale;
- zero or more PDF viewer page numbers actually inspected;
- for claims only, `claim_assessment`: `supported`, `contested`, `refuted`, or
  `open`.

The semantic draft never contains Wiki paths, lifecycle timestamps, credentials,
or free-form Markdown.

Assessment verification returns:

- the canonical assessment ID;
- `supported`, `contradicted`, or `insufficient`;
- the confirmed non-consensus result;
- rationale and the claim/experiment IDs actually compared.

## Runtime gates

The runtime rejects or withholds promotion when:

- a returned ID was not supplied;
- a supplied ID is omitted;
- a page number is outside the local PDF;
- a quantitative experiment lacks a precise locator;
- a claim has no structured support/contradiction edge;
- an assessment cites an unresolved or unverified input;
- the proposed page set introduces a Wiki validation error.

`supported` is necessary but not sufficient for promotion. All deterministic
gates must also pass.

## Provenance metadata

Published entities may include a `verification` mapping with:

```yaml
verification:
  skill: verify-evidence
  verdict: supported
  verified_at: "<ISO-8601 UTC>"
  source_sha256: "<PDF SHA-256>"
  pdf_pages: [7]
  rationale: "<bounded explanation>"
```

For an unresolved check, lifecycle status remains `needs-review` and the same
mapping records the reason.
