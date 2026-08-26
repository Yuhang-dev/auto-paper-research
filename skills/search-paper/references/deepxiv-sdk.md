# DeepXiv SDK Integration

## Integration boundary

This project uses the package installed with:

```text
pip install deepxiv-sdk
```

It does not use a DeepXiv MCP server.

The verified local environment when this reference was written is:

- Conda environment: `(base)`;
- package: `deepxiv-sdk`;
- verified version: `1.0.0`;
- import: `from deepxiv_sdk import Reader`;
- discovery method: `Reader.search(...)`.

Before a real run, verify the installed version:

```powershell
conda run -n base python -m pip show deepxiv-sdk
```

If installation is required, install only after user approval:

```powershell
conda run -n base python -m pip install deepxiv-sdk
```

## Authentication policy

Use the environment variable:

`DEEPXIV_TOKEN`

Registered free tokens are available from:

`https://data.rag.ac.cn/register`

The project script requires this variable before constructing `Reader`. It
never accepts a `--token` argument. This prevents a secret from entering
command history and prevents the SDK CLI from auto-registering an account.

Never:

- commit a token;
- place a real token in a Skill, YAML record, command argument, log, or Error
  Book;
- print the token while checking configuration;
- create or modify a credential file without explicit authorization.

If the token is missing, the script preserves the plan, marks selected queries
`blocked-credential`, and stops before network execution.

## Project scripts

Create a search-run record:

```powershell
conda run -n base python skills/search-paper/scripts/new_search_run.py `
  --topic-slug "<topic-slug>" `
  --question "<research-question>"
```

Preview planned calls without a credential or network:

```powershell
conda run -n base python skills/search-paper/scripts/deepxiv_search.py `
  --run "<search-run.yaml>" --dry-run
```

Execute planned queries:

```powershell
conda run -n base python skills/search-paper/scripts/deepxiv_search.py `
  --run "<search-run.yaml>"
```

Validate the record:

```powershell
conda run -n base python skills/search-paper/scripts/validate_search_run.py `
  "<search-run.yaml>"
```

The executor writes raw provider responses beside the run under `raw/` and
atomically updates the YAML record. Raw responses must not contain credentials.

## Python API contract

The verified `Reader.search` parameters are:

```text
query, size, offset, source, categories, authors, orgs,
venue, venues, venue_year, min_citation,
date_search_type, date_str, date_from, date_to,
use_fine_rerank, top_k
```

Important bounds in SDK 1.0.0:

- query: non-empty and at most 500 characters upstream;
- result size or `top_k`: 1 to 100;
- offset: 0 to 10000;
- source: `arxiv`, `biorxiv`, or `medrxiv`;
- date mode: `between`, `exact`, `after`, or `before`.

The expected response shape is:

```json
{
  "status": "success",
  "total_count": 0,
  "result": []
}
```

Depending on availability, an arXiv result may contain:

```text
arxiv_id, title, abstract, tldr, authors, categories,
citation_count, date, github_url, score, venue, venue_year
```

Treat all fields except the stable source identifier as optional. Preserve
unknown values as `null`.

## Search-mode policy

For candidate discovery, use retrieval search, not `agent_search` or
`deepxiv ask`.

Reasons:

- retrieval exposes a visible candidate set;
- query-to-paper provenance is auditable;
- discovery remains separate from generated synthesis;
- metadata and abstracts cannot verify survey claims.

Progressive paper reading may be used later by ingestion or evidence Skills,
but it is outside `search-paper`.

## Filters and ranking

In SDK 1.0.0:

- categories restrict results without representing relevance truth;
- author and organization filters also influence ranking;
- venue accepts one or more values;
- filters combine restrictively;
- fine reranking is opt-in.

Keep first-pass queries broad. If a filtered query returns no results, remove
one constraint at a time and record each attempt.

Provider score is a retrieval score, not paper quality or evidence strength.

## Failure handling

The SDK exposes:

- `AuthenticationError`;
- `BadRequestError`;
- `RateLimitError`;
- `NotFoundError`;
- `ServerError`;
- `APIError`.

`Reader` defaults to three retries. Do not add an unbounded outer retry loop.

Handle failures as follows:

- authentication: stop and request credential correction;
- bad request: correct query length, parameter, or filter shape;
- rate limit: stop the pass and preserve partial results;
- server or network failure: rely on bounded SDK retries, then record failure;
- malformed response: preserve safe raw details and do not invent fields.

Record provider, package version, query ID, error class, effect, recovery
action, and recurrence key. Sanitize messages before writing them.

## LoopEngineer boundary

The current scripts automate deterministic work:

- run initialization;
- batch query execution and resumption;
- raw-response preservation;
- field normalization;
- stable-ID deduplication;
- possible-version linking;
- metric recomputation;
- schema and secret-safety validation.

The model still performs work that requires research judgment:

- scope definition;
- query-family design;
- relevance reasons;
- coverage interpretation;
- non-consensus hypothesis identification.

Promote additional logic into scripts only after a stable repeated pattern
appears in the Error Book.
