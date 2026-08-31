# Research Wiki Contract

The Wiki is the research plugin's domain state store. It is not conversation
memory or agent memory.

Markdown pages are the source of truth. Files under `wiki/_generated/` are
deterministic, rebuildable indexes.

## Entity types

- `paper`
- `method`
- `experiment`
- `claim`
- `concept`
- `benchmark`
- `model`
- `assessment`

Every current-schema page uses `schema_version: "0.2"` and a stable canonical
ID such as `paper:longlora`. File paths are storage locations, not identity.

Evidence-bearing pages may declare a controlled `facets` list. Newly ingested
methods, benchmarks, models, claims, and experiments preserve page-aware
`evidence` locators so verification can assemble the cited PDF pages. A verified
page contributes covered evidence; a draft page contributes partial evidence.
Search-run candidate coverage is a separate process signal and never satisfies
Wiki evidence coverage.

## Links and relations

Use canonical IDs for navigational links:

```markdown
[[paper:longlora]]
[[benchmark:ruler|RULER]]
```

Use frontmatter `relations` for scientific relationships. Relation source and
target types are defined in `relation-types.yaml`.

Body links are navigational. They do not by themselves assert that an
experiment supports a claim.

`states` links a paper to an author-stated claim. `compares_against` links an
experiment to baseline methods without implying that the paper proposed them.
Experiment-supported claims still require `supports` or `contradicts` evidence
edges; a paper `states` edge is not a substitute for quantitative evidence.

## Status

`status` is lifecycle state:

- `candidate`
- `draft`
- `needs-review`
- `verified`
- `deprecated`

`needs-review` is an ingested source page with unresolved identity, source access,
or central evidence. Like `draft`, it contributes partial rather than verified
evidence coverage.

When `revise-evidence` corrects actionable verifier feedback, it preserves the
old `verification` mapping inside append-only `revision_history`, resets the page
to `draft`, and requires a fresh independent verification pass. Automatic
revision is capped at two history entries per entity.

Claim epistemic state belongs in the claim page's `assessment` field:

- `open`
- `supported`
- `contested`
- `refuted`

Do not overload lifecycle status with claim assessment.

Non-consensus reviews are first-class `assessment` pages. Their `result` is one
of:

- `supported-consensus`
- `contested`
- `insufficient-evidence`

`insufficient-evidence` is a valid verified result when the considered claims,
experiments, benchmarks, and rationale are preserved.

## Legacy compatibility

Pages without `schema_version` and links such as `[[papers/longlora]]` remain
readable during V0.2. They produce migration warnings. New pages must follow
the current schema.

## Read-only engine commands

Run commands from the repository root:

```powershell
python -B -m tools.wiki index
python -B -m tools.wiki validate
python -B -m tools.wiki search "sparse attention"
python -B -m tools.wiki show paper:longlora
python -B -m tools.wiki backlinks paper:longlora
python -B -m tools.wiki neighbors paper:longlora
python -B -m tools.wiki related paper:longlora --depth 2
python -B -m tools.wiki query --type experiment --min-context 32768
python -B -m tools.wiki stats
```

`index` and `validate` only rewrite rebuildable files under `_generated/`.
They never rewrite Markdown source pages. Writer and migration commands remain
intentionally deferred.
