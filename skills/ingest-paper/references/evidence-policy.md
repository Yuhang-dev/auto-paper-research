# Evidence Policy

## Evidence hierarchy

Use sources in this order:

1. the paper full text;
2. the paper appendix or official supplementary material;
3. the official publisher, DOI, or arXiv record for metadata;
4. secondary sources only when clearly labeled and only when the primary source is unavailable.

Do not use a secondary summary as evidence for a technical or quantitative claim.
If only an abstract is available, extract metadata and abstract-level statements,
set the page to `needs-review`, and omit unsupported experiment details.

## Claim classification

Classify each claim along three separate dimensions.

### Attribution

- `author`: stated or asserted by the paper authors;
- `agent-analysis`: interpretation introduced by the current agent.

### Evidence type

- `author-stated`: stated in prose without direct experimental support at the cited location;
- `experiment-supported`: supported by a reported experiment, table, figure, or ablation;
- `inferred`: reasoned from the paper but not directly stated by the authors.

### Evidence status

- `located`: the cited source passage, table, figure, or appendix supports the recorded wording;
- `partial`: evidence supports only part of the wording or scope;
- `unlocated`: no adequate evidence location was found.

`located` means evidence was found during ingestion. It does not mean that an
independent verifier has checked the page.

## Evidence locations

Make evidence locators precise enough for another reader to reproduce the check.
Use one or more of:

- section or subsection name;
- printed paper page, written as `paper p. N`;
- PDF viewer page, written as `PDF p. N`;
- table, figure, equation, algorithm, or appendix identifier;
- a short description of the supporting passage.

Distinguish printed page numbers from PDF viewer page numbers whenever they differ.

## Quantitative evidence

For every important quantitative result, preserve:

- reported value and unit;
- metric name and direction when relevant;
- model or model size;
- dataset or benchmark;
- baseline or comparison target;
- context length and other material settings;
- evidence location.

Do not combine values from different tables or settings into a new result unless it
is explicitly labeled `agent-analysis`. Do not silently convert metrics, average
results, or claim statistical significance.

## Scope and wording

- Keep the claim no broader than the cited evidence.
- Preserve qualifiers such as model family, dataset, task, scale, and experimental setting.
- Do not rewrite correlation as causation.
- Do not rewrite a best-case result as a general result.
- Distinguish comparison with a reported baseline from comparison with all prior work.

## Limitations

Put limitations explicitly stated by the authors under `Reported limitations`.
Put limitations inferred from setup, coverage, assumptions, or missing comparisons
under `Agent analysis`. Give inferred limitations an evidence locator when possible.

## Uncertainty and conflicts

- Use `null` for missing metadata.
- Use `partial` or `unlocated` when evidence is incomplete.
- Record conflicting numbers or statements instead of choosing one silently.
- Set the page to `needs-review` when source access, identity, or central evidence is unresolved.
- Never invent a page number, table, metric, result, identifier, or citation.

## Verification boundary

The ingestion workflow may create `draft` or `needs-review` pages only.
It must not assign `verified` to a page or describe its own extraction as independently verified.
