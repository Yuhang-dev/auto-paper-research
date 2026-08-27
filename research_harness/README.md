# Research Harness Architecture

This package orchestrates deterministic research components around the Markdown
Wiki. Semantic extraction is model-assisted, but identity resolution, schema
validation, publication, rollback, progress measurement, and stopping remain
program-controlled.

## Modules

- `config.py`: environment configuration and the hard C-drive storage guard;
- `persistence.py`: one D-drive SQLite file with separate checkpoint and store
  connections in WAL/autocommit mode;
- `state.py`: thread-scoped LangGraph state and immutable runtime context;
- `memory.py`: explicit, deduplicated cross-thread research notes;
- `skill_registry.py`: strict discovery of `skills/*/SKILL.md`, parsed Skill
  metadata, and lazy access to registered supporting resources;
- `research_models.py`: strict snapshot, gap, decision, action-result, attempt,
  and done-criteria contracts;
- `ingest_models.py`: strict paper-extraction, evidence-locator, entity, claim,
  and experiment contracts;
- `paper_ingest.py`: page-aware PDF extraction, bounded evidence excerpt,
  Skill-conditioned structured extraction, entity resolution, and V0.2 page
  compilation;
- `search_runtime.py`: Skill-conditioned gap query planning, structured candidate
  screening, deterministic selection, search-run validation, and atomic updates;
- `paper_sources.py`: bounded acquisition of explicitly selected public arXiv
  PDFs into the repository D-drive source directory;
- `evidence_verification.py`: PDF/source prechecks, structured semantic
  verification, guarded Wiki lifecycle transitions, and assessment verification;
- `nonconsensus_analysis.py`: condition-aware comparison of verified claims and
  experiments into independently verifiable assessment pages;
- `research_evaluation.py`: deterministic Wiki/search inspection, measurable
  gap candidates, progress yield, and completion gates;
- `research_execution.py`: explicit capability-aware action table for `search`,
  `ingest`, `verify`, and `analyze_claims`;
- `research_control.py`: a read-only V0 control pass and the checkpointed V1
  inspect/decide/execute/observe loop;
- `tools.py`: LangChain wrappers around Wiki, search-run, DeepXiv, and memory
  capabilities;
- `graph.py`: prepare → model → ToolNode → observe loop;
- `cli.py`: run, chat, doctor, state, memories, tools, and Skill inspection
  commands.

## Skill registry boundary

`SkillRegistry` scans only immediate `skills/*/SKILL.md` packages. It validates
YAML frontmatter, requires the directory name to equal the declared Skill name,
loads the instruction body, and inventories files under `references/`,
`assets/`, `scripts/`, and `agents/`. Supporting file contents are read only
when explicitly requested and cannot escape the Skill package root.

This version deliberately has no intelligent Skill router and no generic Skill
executor. Registration remains discovery-only. The outer controller uses four
explicit mappings: `search -> search-paper`, `ingest -> ingest-paper`,
`verify -> verify-evidence`, and `analyze_claims -> analyze-claims`. Each runtime
loads only its registered instructions and required references. Models never
select arbitrary scripts or write Wiki Markdown directly.

```powershell
D:\anaconda3\python.exe -B -m research_harness skills list
D:\anaconda3\python.exe -B -m research_harness skills show search-paper
D:\anaconda3\python.exe -B -m research_harness skills read search-paper references/search-strategy.md
```

## Safety boundaries

- The default database is `.harness/research-harness.sqlite3` under this D-drive
  repository. A C-drive path is rejected before SQLite opens it.
- Ordinary Wiki query tools are read-only. Ingestion, verification, and
  assessment creation can write only through a guarded writer that validates an
  isolated shadow Wiki, atomically publishes source pages, rebuilds indexes, and
  rolls back on failure.
- Automatic PDF acquisition is limited to an explicitly selected arXiv
  candidate, approved HTTPS hosts, a bounded file size, and `sources/papers/`
  under the repository.
- DeepXiv and remote semantic extraction are denied unless runtime context has
  `allow_network=True`.
- Missing credentials, a missing validated query plan, and unsupported actions
  stop before execution with a structured `precondition_blocked` result.
- Tool output is bounded before it enters model context.
- The model sees a token-budgeted message suffix, not the complete checkpoint.
- Cross-thread memory is explicit and separates grounded notes from unverified
  notes.
- Secrets are read only from process environment variables and are never passed
  as tool arguments.

## Persistence scopes

`thread_id` selects short-term checkpoint state. `workspace_id` selects the
cross-thread memory namespace. Several threads can therefore share compact
research decisions without sharing their full conversations.

## Outer research control

The original model/tool loop remains the inner execution engine. The outer V0
control graph is deliberately model-free and non-mutating:

```text
START
  -> prepare
  -> inspect_research
  -> evaluate_gaps
  -> measure_progress
  -> check_done
  -> decide_next_action
  -> END
```

`research step` is one diagnostic control pass. It reads
the Markdown Wiki and search-run YAML, checkpoints compact control state in the
same D-drive SQLite database under a research-specific thread prefix, and
returns a finite action. It does not yet execute the action.

`research run` invokes the V1 loop:

```text
inspect -> evaluate -> check_done -> decide -> execute_action
   ^                                           |
   |           measure <- re-inspect <---------+
   +---- update per-(gap, action) attempts ----+
```

The V1 executor supports search and conditionally enables ingest, verify, and
claim analysis when a model-backed or injected semantic pipeline exists. It produces a
`ResearchActionResult`, increments `research_iterations` and `tool_calls`
internally, re-inspects source files, measures progress, and loops until a done,
budget, blocked, stalled, or unsupported-action route stops it. No-progress is
tracked per `(gap, action)` pair. Unsupported high-priority gaps are skipped when
another executable frontier exists; exhausted supported pairs stop as `stalled`,
while a frontier with no available executor stops as `blocked`. Tool failure and
a valid negative research result are recorded as different outcomes.

`research resume` requires an existing thread checkpoint. Its default
`--mode replan` submits a new graph input on that thread, returns to bootstrap,
and re-inspects Markdown/YAML truth before deciding. `--mode checkpoint` uses
`graph.invoke(None, config)` only when the checkpoint has a pending node and the
current network authorization exactly matches the stored invocation. A caught
Ctrl+C exits with code 130 after printing the thread and same-thread resume
guidance.

Completion uses Wiki evidence-facet coverage, not search candidate coverage.
Candidate/evidence state routes missing facets to search, ingest, or verify.
Completion also requires evidence counts by context bucket and engineering
metric, verified non-consensus assessments, no open blocking gap, quality, and
search saturation. Budget and attempted-action no-progress gates can stop a run
earlier. A `draft`
`DoneCriteria` file can never authorize automatic `finish`; the survey owner
must explicitly promote it to `active`.

```powershell
D:\anaconda3\python.exe -B -m research_harness research inspect long-context-sparse-models
D:\anaconda3\python.exe -B -m research_harness research evaluate long-context-sparse-models
D:\anaconda3\python.exe -B -m research_harness research step long-context-sparse-models --thread outer-v0
D:\anaconda3\python.exe -B -m research_harness research run long-context-sparse-models --thread outer-v1
D:\anaconda3\python.exe -B -m research_harness research run long-context-sparse-models --thread outer-v1 --allow-network
D:\anaconda3\python.exe -B -m research_harness research resume long-context-sparse-models --thread outer-v1 --allow-network
D:\anaconda3\python.exe -B -m research_harness research resume long-context-sparse-models --thread outer-v1 --mode checkpoint --allow-network
```

The second `run` form requires `DEEPXIV_TOKEN` for provider search and the
configured model-provider key for query planning, screening, ingest, verification,
or claim analysis. A selected arXiv paper can receive a bounded local source
automatically; other sources require an explicit repository-relative
`local_pdf_path`. Successful publication closes the candidate as `ingested`.
Verification promotes only source-backed records; `analyze_claims` publishes an
assessment as `needs-review`, and a separate verification pass must promote it.
Citation expansion and synthesis executors remain intentionally deferred.
