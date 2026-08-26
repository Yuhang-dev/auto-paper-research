# Search Strategy

## Purpose

This reference defines how `search-paper` turns a research question into a
small set of complementary searches and decides when another search round is
useful.

Search is a recall-building and screening activity. It does not verify paper
claims.

## 1. Frame the topic before writing queries

For every run, specify:

- population or model family;
- intervention or mechanism;
- task and context regime;
- outcomes of interest;
- engineering constraints;
- time, venue, language, and source boundaries;
- explicit exclusions.

Resolve ambiguous terms in the run record. For the current long-context
sparsity review, do not treat these as interchangeable:

- sparse attention edges or attention patterns;
- token selection and KV-cache sparsification;
- layer, head, activation, or weight sparsity;
- mixture-of-experts routing sparsity;
- retrieval or compression methods that reduce effective context without a
  sparse model structure.

An excluded category may still be retained as `adjacent` when it supplies a
strong baseline or reveals a boundary of the topic.

## 2. Build query families

Use four to eight queries in the first pass. Each query must have a distinct
purpose.

### Direct topic

Combine the main mechanism with the target regime:

`<sparsity mechanism> <long-context term> <model family>`

### Terminology and aliases

Search terminology used by different communities, for example:

- long context, long sequence, extended context, infinite context;
- sparse attention, block sparse, sliding window, global-local attention;
- token pruning, token eviction, KV compression, KV sparsity;
- efficient prefill, long-context decoding, memory-efficient inference.

Do not place every alias into one query. Separate queries show which
terminology retrieved each paper.

### Mechanism or architecture

Target how sparsity is produced:

- static versus dynamic;
- local, global, block, strided, or hierarchical patterns;
- learned versus heuristic selection;
- training-time versus inference-time sparsity;
- exact versus approximate computation.

### Task and benchmark

Separate synthetic recall from real long-document workloads:

- needle or passkey retrieval;
- long-document question answering;
- summarization;
- multi-hop reasoning;
- code or repository understanding;
- long-context language modelling;
- named benchmarks such as LongBench or RULER when in scope.

### Systems and scaling

Search for evidence about:

- prefill latency and throughput;
- decoding latency;
- peak memory and KV-cache memory;
- FLOPs or attended-token ratio;
- kernel support and hardware utilization;
- training cost;
- quality-efficiency scaling with context length.

### Limitations and non-consensus

Always consider at least one query designed to retrieve counter-evidence or
failure analysis. Useful terms include:

- limitation, failure, degradation, bottleneck, lost in the middle;
- dense baseline, retrieval baseline, strong baseline;
- benchmark sensitivity, length generalization, distribution shift;
- accuracy-efficiency trade-off, kernel overhead, irregular sparsity;
- reproducibility, ablation, negative result.

The purpose is to find papers that may challenge the dominant framing. Do not
label a conclusion non-consensus until full-paper evidence has been compared
across sources.

### Open-source implementation

When engineering evidence matters, include repository, kernel, code,
implementation, FlashAttention, Triton, CUDA, or serving-system terms.

A repository URL in provider metadata is only a lead. Verify ownership,
version, license, and reproducibility separately.

## 3. Apply filters conservatively

Start with semantic terms, then add filters for a recorded reason.

DeepXiv filters combine restrictively. A narrow date window, venue list, and
minimum-citation floor can return zero results while relevant papers exist.

Use citation thresholds only for a specific historical or influence question.
They systematically disadvantage recent work.

Record every filter. When recall is weak, loosen one filter at a time.

## 4. Screen candidates consistently

Score each dimension from 0 to 2:

- `sparsity_alignment`: directly studies the included sparsity type;
- `long_context_alignment`: long-context behaviour is a primary setting;
- `evidence_value`: appears to contain relevant comparative or empirical
  evidence;
- `engineering_value`: contributes implementation, scaling, or systems
  evidence;
- `challenge_value`: may expose a limitation, contradiction, or unusually
  strong alternative.

Scores describe screening value, not paper quality or truth.

Assign labels using both scores and a written reason:

- `core`: sparsity and long-context alignment are high, and the paper is
  central to a required facet;
- `adjacent`: directly useful to one side of the question, a strong comparator,
  or an enabling systems contribution;
- `background`: foundational terminology, architecture, benchmark, or survey
  context without direct evidence for the primary question;
- `exclude`: outside scope, duplicate-only, unavailable to the planned evidence
  workflow, or clearly irrelevant.

Do not apply a score threshold mechanically when the written reason
contradicts it. Correct the scores or escalate the ambiguity.

## 5. Deduplicate without losing provenance

Prefer stable identifiers. Normalize arXiv IDs by removing URL wrappers and
version suffixes for work-level matching, but preserve the exact returned
version in provenance.

For title matching:

- compare case-insensitively;
- normalize whitespace and punctuation;
- use year and authors as confirmation signals;
- manually inspect short or generic titles.

When duplicates merge, retain every discovering query and rank. Retrieval
across independent query families is useful search information.

## 6. Measure coverage

At minimum, report coverage across:

- technical families;
- sparsity objects;
- static versus dynamic mechanisms;
- training versus inference stages;
- prefill versus decoding bottlenecks;
- synthetic versus real long-document tasks;
- quality, efficiency, memory, and systems metrics;
- representative open-source implementations;
- limitations and counter-evidence;
- publication years and venues.

Use `covered`, `partial`, `missing`, or `not-required`. Include candidate IDs
supporting `covered` and `partial` assessments.

Candidate coverage means papers have been found for review. It does not mean a
final survey conclusion is supported.

## 7. Decide whether to continue

Continue with a targeted pass when:

- a required facet is `missing` or weakly `partial`;
- terminology remains unresolved;
- one query family dominates the corpus;
- obvious neighboring work is probably missing;
- false positives reveal a fixable query ambiguity.

Stop when one condition is met and record it:

- required candidate-level coverage targets are met and two consecutive
  targeted passes add no new `core` candidate;
- the configured query, candidate, time, or request budget is reached;
- progress requires a new provider, credential, citation graph, or human scope
  decision;
- the user explicitly ends the run.

Do not claim exhaustive coverage from one provider or the absence of new
results.

## 8. Use citation expansion sparingly

Backward expansion is useful for foundations and benchmark origins. Forward
expansion is useful for comparisons, reproductions, and failure analyses.

For each expansion, record:

- seed paper ID;
- direction;
- provider or source;
- number screened;
- number retained;
- uncovered facet it was meant to address.

Stop expanding a seed when it mostly reproduces screened candidates.
