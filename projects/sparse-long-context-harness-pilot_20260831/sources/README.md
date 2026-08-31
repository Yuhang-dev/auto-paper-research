# Error Book

The Error Book records repeatable Harness failures. It is separate from the
research Wiki because operational errors and research entities have different
lifecycles.

When errors occur, append one sanitized JSON object per occurrence to
`errors.jsonl`:

```json
{
  "recurrence_key": "evaluation_context_confusion",
  "skill": "ingest-paper",
  "entity": "paper:example",
  "observed": "Training context was extracted as evaluation context.",
  "timestamp": "2026-08-26T00:00:00Z"
}
```

Repeated recurrence keys are expected. A generated summary may aggregate their
counts and recommend a Skill, schema, validator, or script change.

Never store credentials or unsanitized provider errors here.
