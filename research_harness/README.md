# Research Harness Architecture

This package orchestrates existing deterministic research components. It does
not replace the Markdown Wiki, infer scientific evidence, or write Wiki source
pages.

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
- `research_evaluation.py`: deterministic Wiki/search inspection, measurable
  gap candidates, progress yield, and completion gates;
- `research_execution.py`: explicit deterministic action table; V1 binds only
  `search` to the registered `search-paper` script;
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
executor. Registration remains discovery-only. The outer controller uses one
explicit deterministic mapping, `search -> search-paper`, and resolves the
registered `scripts/deepxiv_search.py` resource before execution. Ingest and
verification executors remain deferred.

```powershell
D:\anaconda3\python.exe -B -m research_harness skills list
D:\anaconda3\python.exe -B -m research_harness skills show search-paper
D:\anaconda3\python.exe -B -m research_harness skills read search-paper references/search-strategy.md
```

## Safety boundaries

- The default database is `.harness/research-harness.sqlite3` under this D-drive
  repository. A C-drive path is rejected before SQLite opens it.
- Wiki tools in this version are read-only for Markdown source pages.
- DeepXiv execution is denied unless runtime context has `allow_network=True`.
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

The V1 executor currently supports search only. It produces a
`ResearchActionResult`, increments `research_iterations` and `tool_calls`
internally, re-inspects source files, measures progress, and loops until a done,
budget, no-progress, blocked, or unsupported-action route stops it. Tool failure
and a valid search with no new evidence are recorded as different outcomes.

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
```

The second `run` form requires `DEEPXIV_TOKEN` in the process environment.
Structured-LLM semantic ranking, query-plan generation, and ingest/verify
executors remain intentionally deferred.
