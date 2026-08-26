"""LangChain tools that expose deterministic research capabilities."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence

import yaml
from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool

from tools.wiki.indexer import build_index
from tools.wiki.query import (
    entity_payload,
    related_entities,
    resolve_entity,
    search_entities,
    structured_query,
)
from tools.wiki.validator import validate_index

from .config import HarnessSettings
from .memory import recall_notes, remember_note
from .state import HarnessContext, HarnessState


def _bounded_json(payload: Any, limit: int) -> str:
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(rendered) <= limit:
        return rendered
    preview_length = max(200, limit - 180)
    return json.dumps(
        {
            "ok": True,
            "truncated": True,
            "original_chars": len(rendered),
            "preview": rendered[:preview_length],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _inside_repository(settings: HarnessSettings, value: str) -> Path:
    raw_path = Path(value)
    if not raw_path.is_absolute():
        raw_path = settings.repository_root / raw_path
    resolved = raw_path.resolve()
    try:
        resolved.relative_to(settings.repository_root.resolve())
    except ValueError as exc:
        raise ValueError("Path must remain inside the research repository") from exc
    return resolved


def _safe_subprocess_text(text: str, limit: int) -> str:
    token = os.getenv("DEEPXIV_TOKEN", "")
    safe = text.replace(token, "<redacted>") if token else text
    return safe[-limit:]


def build_tools(settings: HarnessSettings) -> List[BaseTool]:
    """Build tools as closures over immutable repository configuration."""

    output_limit = settings.tool_output_chars

    def current_index():
        return build_index(settings.wiki_root, settings.wiki_meta_root)

    @tool
    def wiki_search(
        query: str,
        entity_type: Optional[str] = None,
        status: Optional[str] = None,
        year: Optional[int] = None,
        limit: int = 10,
    ) -> str:
        """Search Wiki IDs, titles, aliases, metadata, and Markdown text."""

        records = search_entities(
            current_index(),
            query,
            entity_type=entity_type,
            status=status,
            year=year,
        )[: max(1, min(limit, 25))]
        return _bounded_json(
            {"ok": True, "count": len(records), "entities": records},
            output_limit,
        )

    @tool
    def wiki_show(reference: str, include_body: bool = False) -> str:
        """Resolve a canonical ID, title, alias, or legacy path and show one Wiki entity."""

        index = current_index()
        resolution, entity, candidates = resolve_entity(index, reference)
        if entity is None:
            return _bounded_json(
                {
                    "ok": False,
                    "resolution": resolution,
                    "reference": reference,
                    "candidates": list(candidates),
                },
                output_limit,
            )
        return _bounded_json(
            {
                "ok": True,
                "resolution": resolution,
                "entity": entity_payload(entity, include_body=include_body),
            },
            output_limit,
        )

    @tool
    def wiki_related(reference: str, depth: int = 2, limit: int = 25) -> str:
        """Traverse typed Wiki relations around one entity for a small number of hops."""

        if not 1 <= depth <= 4:
            return _bounded_json(
                {"ok": False, "error": "depth must be between 1 and 4"},
                output_limit,
            )
        index = current_index()
        resolution, entity, candidates = resolve_entity(index, reference)
        if entity is None:
            return _bounded_json(
                {
                    "ok": False,
                    "resolution": resolution,
                    "candidates": list(candidates),
                },
                output_limit,
            )
        records = related_entities(index, str(entity.entity_id), depth=depth)
        records = records[: max(1, min(limit, 50))]
        return _bounded_json(
            {
                "ok": True,
                "source": entity.entity_id,
                "count": len(records),
                "entities": records,
            },
            output_limit,
        )

    @tool
    def wiki_experiment_query(
        benchmark: Optional[str] = None,
        method: Optional[str] = None,
        model: Optional[str] = None,
        min_context: Optional[int] = None,
        max_context: Optional[int] = None,
        sparsity_target: Optional[str] = None,
        min_sparsity: Optional[float] = None,
        max_sparsity: Optional[float] = None,
        status: Optional[str] = None,
        limit: int = 25,
    ) -> str:
        """Query structured experiment entities while preserving evaluation conditions."""

        records = structured_query(
            current_index(),
            entity_type="experiment",
            status=status,
            benchmark=benchmark,
            method=method,
            model=model,
            min_context=min_context,
            max_context=max_context,
            sparsity_target=sparsity_target,
            min_sparsity=min_sparsity,
            max_sparsity=max_sparsity,
        )[: max(1, min(limit, 50))]
        return _bounded_json(
            {"ok": True, "count": len(records), "experiments": records},
            output_limit,
        )

    @tool
    def wiki_validate(max_diagnostics: int = 30) -> str:
        """Validate Wiki schema, canonical IDs, links, relations, and evidence policies."""

        index = current_index()
        diagnostics = validate_index(index)
        counts = Counter(item.severity for item in diagnostics)
        bounded = diagnostics[: max(1, min(max_diagnostics, 100))]
        return _bounded_json(
            {
                "ok": counts.get("ERROR", 0) == 0,
                "counts": {
                    severity: counts.get(severity, 0)
                    for severity in ("ERROR", "WARNING", "INFO")
                },
                "diagnostics": [item.as_dict() for item in bounded],
            },
            output_limit,
        )

    @tool
    def wiki_stats() -> str:
        """Return deterministic Wiki corpus, relation, link, and diagnostic statistics."""

        index = current_index()
        diagnostics = validate_index(index)
        return _bounded_json(
            {"ok": True, "stats": index.stats(diagnostics)},
            output_limit,
        )

    @tool
    def search_run_status(run_path: str, candidate_limit: int = 20) -> str:
        """Read one existing paper-search run and summarize queries, candidates, and coverage."""

        path = _inside_repository(settings, run_path)
        if path.suffix.casefold() not in {".yaml", ".yml"} or not path.is_file():
            return _bounded_json(
                {"ok": False, "error": "Search run must be an existing YAML file"},
                output_limit,
            )
        with path.open("r", encoding="utf-8") as handle:
            run = yaml.safe_load(handle)
        if not isinstance(run, dict):
            return _bounded_json(
                {"ok": False, "error": "Search run is not a YAML mapping"},
                output_limit,
            )
        queries = run.get("queries") or []
        candidates = run.get("candidates") or []
        query_statuses = Counter(
            str((query.get("execution") or {}).get("status") or "unknown")
            for query in queries
            if isinstance(query, dict)
        )
        candidate_preview = [
            {
                "candidate_id": item.get("candidate_id"),
                "title": item.get("title"),
                "year": item.get("year"),
                "review_state": item.get("review_state"),
                "relevance": item.get("relevance"),
            }
            for item in candidates[: max(1, min(candidate_limit, 50))]
            if isinstance(item, dict)
        ]
        return _bounded_json(
            {
                "ok": True,
                "path": path.relative_to(settings.repository_root).as_posix(),
                "run": run.get("run"),
                "query_statuses": dict(sorted(query_statuses.items())),
                "coverage": run.get("coverage"),
                "candidate_count": len(candidates),
                "candidates": candidate_preview,
            },
            output_limit,
        )

    @tool
    def deepxiv_search_run(
        run_path: str,
        query_ids: Optional[List[str]] = None,
        dry_run: bool = True,
        runtime: ToolRuntime[HarnessContext, HarnessState] = None,
    ) -> str:
        """Preview or execute a planned DeepXiv search run through the existing deterministic script."""

        path = _inside_repository(settings, run_path)
        if path.suffix.casefold() not in {".yaml", ".yml"} or not path.is_file():
            return _bounded_json(
                {"ok": False, "error": "Search run must be an existing YAML file"},
                output_limit,
            )
        if not dry_run and (runtime is None or not runtime.context.allow_network):
            return _bounded_json(
                {
                    "ok": False,
                    "error": "Network execution is disabled. Invoke the Harness with allow_network=true.",
                },
                output_limit,
            )
        command = [
            sys.executable,
            str(
                settings.repository_root
                / "skills"
                / "search-paper"
                / "scripts"
                / "deepxiv_search.py"
            ),
            "--run",
            str(path),
        ]
        for query_id in query_ids or []:
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", query_id):
                return _bounded_json(
                    {"ok": False, "error": f"Invalid query ID: {query_id!r}"},
                    output_limit,
                )
            command.extend(["--query-id", query_id])
        if dry_run:
            command.append("--dry-run")
        child_environment = dict(os.environ)
        child_environment["PYTHONUTF8"] = "1"
        child_environment.setdefault(
            "TIKTOKEN_CACHE_DIR",
            str(settings.repository_root / ".harness" / "tiktoken-cache"),
        )
        try:
            completed = subprocess.run(
                command,
                cwd=str(settings.repository_root),
                env=child_environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return _bounded_json(
                {"ok": False, "error": "DeepXiv search timed out after 300 seconds"},
                output_limit,
            )
        return _bounded_json(
            {
                "ok": completed.returncode == 0,
                "dry_run": dry_run,
                "exit_code": completed.returncode,
                "stdout": _safe_subprocess_text(completed.stdout, output_limit // 2),
                "stderr": _safe_subprocess_text(completed.stderr, output_limit // 2),
            },
            output_limit,
        )

    @tool
    def remember_research_memory(
        text: str,
        topic: str = "general",
        kind: Literal[
            "observation", "decision", "preference", "open-question"
        ] = "observation",
        evidence_ids: Optional[List[str]] = None,
        runtime: ToolRuntime[HarnessContext, HarnessState] = None,
    ) -> str:
        """Persist one compact cross-thread research note; include evidence IDs when grounded."""

        if runtime is None or runtime.store is None:
            return _bounded_json(
                {"ok": False, "error": "Research memory store is unavailable"},
                output_limit,
            )
        record = remember_note(
            runtime.store,
            runtime.context.workspace_id,
            text=text,
            topic=topic,
            kind=kind,
            evidence_ids=evidence_ids,
        )
        return _bounded_json({"ok": True, "memory": record}, output_limit)

    @tool
    def recall_research_memory(
        query: str = "",
        limit: int = 8,
        runtime: ToolRuntime[HarnessContext, HarnessState] = None,
    ) -> str:
        """Recall compact research notes shared across threads in the current workspace."""

        if runtime is None or runtime.store is None:
            return _bounded_json(
                {"ok": False, "error": "Research memory store is unavailable"},
                output_limit,
            )
        records = recall_notes(
            runtime.store,
            runtime.context.workspace_id,
            query=query,
            limit=limit,
        )
        return _bounded_json(
            {"ok": True, "count": len(records), "memories": records},
            output_limit,
        )

    return [
        wiki_search,
        wiki_show,
        wiki_related,
        wiki_experiment_query,
        wiki_validate,
        wiki_stats,
        search_run_status,
        deepxiv_search_run,
        remember_research_memory,
        recall_research_memory,
    ]
