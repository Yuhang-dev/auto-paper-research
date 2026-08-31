# PaperIngestDraft Contract

The runtime Pydantic authority is `research_harness/ingest_models.py`. Unknown
fields are forbidden and models are immutable after validation.

## Root

```text
PaperIngestDraft
├── candidate_id
├── paper: PaperDraft
├── methods[]: MethodDraft
├── benchmarks[]: BenchmarkDraft
├── models[]: ModelDraft
├── claims[]: ClaimDraft
└── experiments[]: ExperimentDraft
```

`candidate_id` must exactly match the selected search candidate.

## Local keys

Reusable entities, claims, and experiments use page-local keys. A key starts with
a letter and contains at most 64 letters, digits, underscores, or hyphens. Keys
must be unique within their entity group.

Experiments refer to method, model, benchmark, and claim local keys. Unknown keys
are rejected. The same claim cannot be both supported and contradicted by one
experiment. An experiment cannot list the same method as both evaluated and
baseline.

## EvidenceLocator

Required:

- `pdf_page`: one-indexed PDF viewer page;
- `description`: concise description of what supports the record.

Optional: `paper_page`, `section`, and `element` such as `Table 2`, `Figure 4`, or
`Appendix B.5`.

Every method, benchmark, model, and experiment requires a locator. A claim with
`evidence_status: located` or `evidence_type: experiment-supported` also requires
one.

## Reusable entities

Method, benchmark, and model records contain:

- `key`;
- either a catalog-backed `existing_id` or a new `proposed_slug`;
- canonical `title`, aliases, facets, and evidence;
- type-specific fields.

Every method also has a required paper-local role: `proposed`, `baseline`, or
`prior-work`. This role controls whether the paper receives a structured
`proposes` edge; it is not a permanent property of the reusable method entity.

An `existing_id` must use the expected type prefix. New slugs use lowercase
kebab-case.

## Claims

Controlled values:

- attribution: `author` or `agent-analysis`;
- evidence type: `author-stated`, `experiment-supported`, or `inferred`;
- evidence status: `located`, `partial`, or `unlocated`.

Claims require a non-empty scoped mapping (`minProperties: 1`). Put at least one
source-supported model, benchmark, context length, setting, or metric there when
it constrains the wording. Do not use `unknown`, `null`, or a placeholder merely
to satisfy the minimum. Omit an unsupported claim; when doing so, remove its key
from every experiment claim-reference list.

## Experiments

Each experiment requires non-empty method and model keys, one benchmark key,
positive integer context length, non-empty sparsity mapping, metric, result, and
evidence. `method_keys` identifies evaluated methods.
`baseline_method_keys` identifies comparison methods and may be empty. One record
represents one material result under one coherent setting.

Every `experiment-supported` claim must appear in an experiment's
`supports_claim_keys` or `contradicts_claim_keys`.

## Offline validation

Validate a JSON draft without a model or Wiki mutation:

```powershell
D:\anaconda3\python.exe skills/ingest-paper/scripts/validate_ingest_draft.py draft.json
```
