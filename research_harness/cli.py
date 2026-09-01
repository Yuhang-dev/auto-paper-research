"""Command-line interface for the LangGraph research harness."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import signal
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import yaml  # type: ignore[import-untyped]
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage

from tools.wiki.indexer import build_index
from tools.wiki.validator import validate_index

from .canary_models import CanaryLimits, CanaryRunReport
from .config import HarnessSettings
from .graph import ResearchHarness
from .memory import list_notes, recall_notes
from .paper_ingest import StagedWikiPublisher
from .persistence import HarnessPersistence
from .research_control import AutonomousResearchController, ResearchController
from .research_evaluation import (
    check_done,
    decide_next_action,
    evaluate_gaps,
    inspect_research,
    load_done_criteria,
    resolve_research_directory,
)
from .skill_registry import RESOURCE_GROUPS, SkillRegistry, SkillSpec
from .staged_ingest import StagedPaperRecord, StagedPaperStore
from .trajectory import (
    ensure_annotation_sidecar,
    export_checkpoint_trajectory,
    trajectory_directory,
)


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _add_format(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("text", "json"), default="text")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LangGraph orchestration for the LLM-Wiki research Harness.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--db",
        type=Path,
        help="Persistent SQLite path on any writable drive.",
    )
    parser.add_argument(
        "--model",
        help="OpenAI-compatible model string, e.g. openai:<served-model-name>.",
    )
    parser.add_argument(
        "--model-base-url",
        help="OpenAI-compatible API root, e.g. http://127.0.0.1:8000/v1.",
    )
    parser.add_argument("--workspace", help="Cross-thread memory namespace.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser(
        "doctor", help="Check dependencies, Wiki, and storage."
    )
    _add_format(doctor)

    run = subparsers.add_parser("run", help="Run one task in a persistent thread.")
    run.add_argument("task")
    run.add_argument("--thread", required=True)
    run.add_argument("--allow-network", action="store_true")
    _add_format(run)

    chat = subparsers.add_parser("chat", help="Start an interactive persistent thread.")
    chat.add_argument("--thread", required=True)
    chat.add_argument("--allow-network", action="store_true")

    state = subparsers.add_parser(
        "state", help="Inspect the latest checkpoint for a thread."
    )
    state.add_argument("--thread", required=True)
    state.add_argument("--message-limit", type=int, default=20)
    _add_format(state)

    memories = subparsers.add_parser(
        "memories", help="List or keyword-filter cross-thread research memory."
    )
    memories.add_argument("--query", default="")
    memories.add_argument("--limit", type=int, default=20)
    _add_format(memories)

    tools = subparsers.add_parser("tools", help="List available deterministic tools.")
    _add_format(tools)

    skills = subparsers.add_parser(
        "skills",
        help="Inspect repository-local Skills without executing or routing them.",
    )
    skill_commands = skills.add_subparsers(dest="skill_command", required=True)
    skill_list = skill_commands.add_parser("list", help="List registered Skills.")
    _add_format(skill_list)
    skill_show = skill_commands.add_parser(
        "show", help="Show parsed frontmatter, instructions, and resource inventory."
    )
    skill_show.add_argument("name")
    _add_format(skill_show)
    skill_read = skill_commands.add_parser(
        "read", help="Read one registered reference, asset, script, or agent file."
    )
    skill_read.add_argument("name")
    skill_read.add_argument("resource")
    _add_format(skill_read)

    research = subparsers.add_parser(
        "research",
        help="Inspect or advance the deterministic outer research control loop.",
    )
    research_commands = research.add_subparsers(
        dest="research_command",
        required=True,
    )
    research_inspect = research_commands.add_parser(
        "inspect", help="Build a read-only ResearchSnapshot."
    )
    research_inspect.add_argument("research_id")
    _add_format(research_inspect)

    research_evaluate = research_commands.add_parser(
        "evaluate", help="Inspect, generate measurable gaps, and apply DoneCriteria."
    )
    research_evaluate.add_argument("research_id")
    research_evaluate.add_argument("--criteria", type=Path)
    research_evaluate.add_argument("--iteration", type=int, default=0)
    research_evaluate.add_argument("--tool-calls", type=int, default=0)
    research_evaluate.add_argument("--no-progress-rounds", type=int, default=0)
    _add_format(research_evaluate)

    research_step = research_commands.add_parser(
        "step", help="Run and checkpoint one non-mutating outer-loop control pass."
    )
    research_step.add_argument("research_id")
    research_step.add_argument("--criteria", type=Path)
    research_step.add_argument("--thread")
    _add_format(research_step)

    research_run = research_commands.add_parser(
        "run",
        help="Run the persisted V1 inspect/decide/execute/observe loop.",
    )
    research_run.add_argument("research_id")
    research_run.add_argument("--criteria", type=Path)
    research_run.add_argument("--thread")
    research_run.add_argument(
        "--allow-network",
        action="store_true",
        help=(
            "Authorize network-backed search or semantic extraction for this invocation."
        ),
    )
    _add_format(research_run)

    research_resume = research_commands.add_parser(
        "resume",
        help="Resume an existing research thread; replan from source truth by default.",
    )
    research_resume.add_argument("research_id")
    research_resume.add_argument("--criteria", type=Path)
    research_resume.add_argument("--thread", required=True)
    research_resume.add_argument(
        "--mode",
        choices=("replan", "checkpoint"),
        default="replan",
        help=(
            "replan re-inspects Markdown/YAML truth; checkpoint resumes the exact "
            "pending LangGraph node."
        ),
    )
    research_resume.add_argument(
        "--allow-network",
        action="store_true",
        help=(
            "Authorize network actions for replan; exact checkpoint mode requires "
            "this to match the pending checkpoint."
        ),
    )
    _add_format(research_resume)

    research_review = research_commands.add_parser(
        "review",
        help="Run the review-first fast research loop without writing the Wiki.",
    )
    review_commands = research_review.add_subparsers(
        dest="review_command",
        required=True,
    )

    review_start = review_commands.add_parser(
        "start", help="Start a persisted scientific-review run."
    )
    review_start.add_argument("research_id")
    review_start.add_argument("--thread", required=True)
    review_start.add_argument("--run-id")
    review_start.add_argument(
        "--profile",
        choices=("smoke", "seed5", "standard", "literature50"),
        default="standard",
        help=(
            "smoke runs 8→4→2; seed5 deep-reads five curated papers without "
            "provider discovery; standard runs 50→20→10; literature50 requires "
            "at least 50 unique paper skims before readiness"
        ),
    )
    review_start.add_argument(
        "--seed-manifest",
        type=Path,
        help=(
            "Curated exact-identity paper manifest. seed5 defaults to "
            "research/<research-id>/seed-papers.yaml."
        ),
    )
    review_start.add_argument("--allow-network", action="store_true")
    review_start.add_argument(
        "--allow-single-model-fallback",
        action="store_true",
        help="Explicitly allow the fast model to perform reasoning tasks too.",
    )
    review_start.add_argument(
        "--stop-after",
        choices=(
            "frame",
            "retrieval",
            "screening",
            "skim",
            "reasoning",
            "deep-read",
            "assessment",
            "synthesis",
        ),
        default="synthesis",
    )
    _add_format(review_start)

    review_resume = review_commands.add_parser(
        "resume", help="Resume a review after re-inspecting run artifacts by default."
    )
    review_resume.add_argument("research_id")
    review_resume.add_argument("--thread", required=True)
    review_resume.add_argument(
        "--mode", choices=("checkpoint", "replan"), default="replan"
    )
    _add_format(review_resume)

    review_status = review_commands.add_parser(
        "status", help="Inspect a review checkpoint and its artifact funnel."
    )
    review_status.add_argument("research_id")
    review_status.add_argument("--thread", required=True)
    _add_format(review_status)

    review_synthesize = review_commands.add_parser(
        "synthesize", help="Regenerate the review from the current Evidence Pool."
    )
    review_synthesize.add_argument("research_id")
    review_synthesize.add_argument("--thread", required=True)
    _add_format(review_synthesize)

    review_canary = review_commands.add_parser(
        "canary", help="Run the isolated 8 → 4 → 2 review smoke profile."
    )
    review_canary.add_argument("research_id")
    review_canary.add_argument("--thread")
    review_canary.add_argument("--run-id")
    review_canary.add_argument("--allow-network", action="store_true")
    review_canary.add_argument(
        "--allow-single-model-fallback", action="store_true"
    )
    review_canary.add_argument(
        "--stop-after",
        choices=(
            "frame",
            "retrieval",
            "screening",
            "skim",
            "reasoning",
            "deep-read",
            "assessment",
            "synthesis",
        ),
        default="synthesis",
    )
    _add_format(review_canary)

    review_promote = review_commands.add_parser(
        "promote",
        help="Preview or execute explicit handoff into deferred PaperIngestDraft.",
    )
    review_promote.add_argument("research_id")
    review_promote.add_argument("--thread", required=True)
    review_promote.add_argument("--manifest", type=Path)
    review_promote.add_argument("--execute", action="store_true")
    review_promote.add_argument(
        "--allow-network",
        action="store_true",
        help="Authorize model/API calls during an executed promotion.",
    )
    _add_format(review_promote)

    research_canary = research_commands.add_parser(
        "canary",
        help="Run a hard-bounded online flow in an isolated workspace.",
    )
    research_canary.add_argument("research_id")
    research_canary.add_argument("--run-id")
    research_canary.add_argument("--source-run", type=Path)
    research_canary.add_argument(
        "--allow-network",
        action="store_true",
        help="Explicitly authorize the Canary's provider and model calls.",
    )
    research_canary.add_argument(
        "--stop-after",
        choices=(
            "retrieval",
            "screening",
            "ingest",
            "verification",
            "revision",
            "reverification",
            "analysis",
        ),
        default="screening",
    )
    research_canary.add_argument("--max-planned-queries", type=int, default=1)
    research_canary.add_argument("--max-provider-query-calls", type=int, default=1)
    research_canary.add_argument("--max-new-unique-candidates", type=int, default=5)
    research_canary.add_argument("--max-selected-candidates", type=int, default=3)
    research_canary.add_argument("--max-papers-ingested", type=int, default=1)
    research_canary.add_argument("--max-actions", type=int, default=3)
    research_canary.add_argument("--deadline-seconds", type=int, default=300)
    research_canary.add_argument("--provider-max-retries", type=int, default=0)
    research_canary.add_argument(
        "--defer-wiki",
        action="store_true",
        help=(
            "Stage validated paper drafts without Wiki lookup or publication. "
            "Use research publish-staged in a separate pass."
        ),
    )
    _add_format(research_canary)

    research_publish_staged = research_commands.add_parser(
        "publish-staged",
        help="Publish deferred paper drafts without another model call.",
    )
    research_publish_staged.add_argument("research_id")
    research_publish_staged.add_argument(
        "--run-id",
        required=True,
        help="Canary or review run that owns the staged draft queue.",
    )
    research_publish_staged.add_argument(
        "--target",
        choices=("canary", "formal"),
        default="canary",
        help=(
            "Publish into the isolated Canary Wiki by default; formal explicitly "
            "updates the repository Wiki source of truth."
        ),
    )
    research_publish_staged.add_argument("--max-papers", type=int, default=20)
    research_publish_staged.add_argument(
        "--preview",
        action="store_true",
        help="Compile and validate pages without writing the Wiki or staging queue.",
    )
    _add_format(research_publish_staged)

    research_export = research_commands.add_parser(
        "export-trajectory",
        help="Derive a JSONL evaluation trajectory from SQLite checkpoints.",
    )
    research_export.add_argument("research_id")
    research_export.add_argument("--thread", required=True)
    _add_format(research_export)
    return parser.parse_args(argv)


def _settings(args: argparse.Namespace) -> HarnessSettings:
    return HarnessSettings.from_env(
        database_path=args.db,
        model=args.model,
        model_base_url=args.model_base_url,
        workspace_id=args.workspace,
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, BaseMessage):
        return _message_payload(value)
    if hasattr(value, "isoformat") and callable(value.isoformat):
        return value.isoformat()
    return value


def _emit_json(value: Any) -> None:
    print(json.dumps(_json_safe(value), ensure_ascii=False, indent=2, sort_keys=True))


def _message_payload(message: AnyMessage) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "id": message.id,
        "type": message.type,
        "content": message.content,
    }
    name = getattr(message, "name", None)
    if name:
        payload["name"] = name
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        payload["tool_calls"] = tool_calls
    return payload


def _final_ai_message(state: Dict[str, Any]) -> Optional[AIMessage]:
    for message in reversed(state.get("messages") or []):
        if isinstance(message, AIMessage):
            return message
    return None


def _content_text(message: AIMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def _doctor(settings: HarnessSettings, output_format: str) -> int:
    registry = SkillRegistry(settings.skills_root)
    research_controls = []
    for directory in sorted(
        (item for item in settings.research_root.iterdir() if item.is_dir()),
        key=lambda item: item.name.casefold(),
    ):
        if not (directory / "done-criteria.yaml").is_file():
            continue
        criteria = load_done_criteria(settings, directory.name)
        research_controls.append(
            {
                "research_id": directory.name,
                "criteria_status": criteria.status,
            }
        )
    index = build_index(settings.wiki_root, settings.wiki_meta_root)
    diagnostics = validate_index(index)
    diagnostic_counts = {
        severity: sum(item.severity == severity for item in diagnostics)
        for severity in ("ERROR", "WARNING", "INFO")
    }
    with HarnessPersistence(settings) as persistence:
        checkpoint_counts = persistence.checkpoint_counts()
    packages: Dict[str, Optional[str]] = {}
    for name in (
        "langchain",
        "langgraph",
        "langgraph-checkpoint-sqlite",
        "langchain-openai",
        "deepxiv-sdk",
        "tavily-python",
        "requests",
        "pydantic",
    ):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    path = settings.database_path
    openai_key_configured = bool(os.getenv("OPENAI_API_KEY", "").strip())
    result = {
        "ok": diagnostic_counts["ERROR"] == 0,
        "repository_root": str(settings.repository_root),
        "database_path": str(path),
        "database_drive": path.drive,
        "database_exists": path.exists(),
        "database_bytes": path.stat().st_size if path.exists() else 0,
        "checkpoint_counts": checkpoint_counts,
        "model": settings.model,
        "model_configured": bool(settings.model),
        "model_base_url": settings.model_base_url,
        "model_endpoint_host": settings.model_endpoint_host,
        "openai_key_configured": openai_key_configured,
        "model_configuration_ready": bool(
            settings.model and settings.model_base_url and openai_key_configured
        ),
        "deepxiv_token_configured": bool(os.getenv("DEEPXIV_TOKEN")),
        "semantic_scholar_key_configured": bool(
            os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()
            or os.getenv("S2_API_KEY", "").strip()
        ),
        "tavily_key_configured": bool(os.getenv("TAVILY_API_KEY", "").strip()),
        "github_token_configured": bool(os.getenv("GITHUB_TOKEN", "").strip()),
        "review_models": {
            "fast_model": os.getenv("HARNESS_FAST_MODEL", "").strip()
            or settings.model,
            "fast_base_url_configured": bool(
                os.getenv("HARNESS_FAST_MODEL_BASE_URL", "").strip()
                or settings.model_base_url
            ),
            "fast_key_configured": bool(
                os.getenv("HARNESS_FAST_API_KEY", "").strip()
                or os.getenv("OPENAI_API_KEY", "").strip()
            ),
            "reasoning_model": os.getenv("HARNESS_REASONING_MODEL", "").strip()
            or None,
            "reasoning_base_url_configured": bool(
                os.getenv("HARNESS_REASONING_MODEL_BASE_URL", "").strip()
            ),
            "reasoning_key_configured": bool(
                os.getenv("HARNESS_REASONING_API_KEY", "").strip()
            ),
        },
        "strict_msgpack": os.getenv("LANGGRAPH_STRICT_MSGPACK") == "true",
        "skills": {"count": len(registry), "names": list(registry.names)},
        "research_controls": {
            "count": len(research_controls),
            "topics": research_controls,
        },
        "wiki_diagnostics": diagnostic_counts,
        "packages": packages,
    }
    if output_format == "json":
        _emit_json(result)
    else:
        print(yaml.safe_dump(result, allow_unicode=True, sort_keys=False).rstrip())
    return 0 if result["ok"] else 1


def _run(settings: HarnessSettings, args: argparse.Namespace) -> int:
    with ResearchHarness(settings) as harness:
        result = harness.invoke(
            args.task,
            thread_id=args.thread,
            workspace_id=settings.workspace_id,
            allow_network=args.allow_network,
        )
    final_message = _final_ai_message(result)
    if final_message is None:
        raise RuntimeError("Harness completed without an AI response")
    if args.format == "json":
        _emit_json(
            {
                "thread_id": args.thread,
                "workspace_id": settings.workspace_id,
                "response": _message_payload(final_message),
                "iteration": result.get("iteration", 0),
                "tool_failures": result.get("tool_failures", 0),
                "stop_reason": result.get("stop_reason", ""),
                "context_message_count": result.get("context_message_count", 0),
                "recalled_memory_count": result.get("recalled_memory_count", 0),
            }
        )
    else:
        print(_content_text(final_message))
    return 0


def _chat(settings: HarnessSettings, args: argparse.Namespace) -> int:
    print("Persistent research chat. Type /exit to quit.")
    with ResearchHarness(settings) as harness:
        while True:
            try:
                task = input("research> ").strip()
            except EOFError:
                break
            if task.casefold() in {"/exit", "/quit", "exit", "quit"}:
                break
            if not task:
                continue
            result = harness.invoke(
                task,
                thread_id=args.thread,
                workspace_id=settings.workspace_id,
                allow_network=args.allow_network,
            )
            final_message = _final_ai_message(result)
            if final_message:
                print(_content_text(final_message))
    return 0


def _state(settings: HarnessSettings, args: argparse.Namespace) -> int:
    with HarnessPersistence(settings) as persistence:
        assert persistence.checkpointer is not None
        checkpoint = persistence.checkpointer.get_tuple(
            {"configurable": {"thread_id": args.thread}}
        )
    if checkpoint is None:
        print(f"No checkpoint found for thread {args.thread!r}.")
        return 1
    values = dict(checkpoint.checkpoint.get("channel_values") or {})
    messages = list(values.get("messages") or [])
    values["messages"] = [
        _message_payload(message)
        for message in messages[-max(1, min(args.message_limit, 100)) :]
    ]
    payload = {
        "thread_id": args.thread,
        "checkpoint_id": checkpoint.config.get("configurable", {}).get("checkpoint_id"),
        "state": values,
    }
    if args.format == "json":
        _emit_json(payload)
    else:
        print(
            yaml.safe_dump(
                _json_safe(payload), allow_unicode=True, sort_keys=False
            ).rstrip()
        )
    return 0


def _memories(settings: HarnessSettings, args: argparse.Namespace) -> int:
    with HarnessPersistence(settings) as persistence:
        assert persistence.store is not None
        if args.query:
            records = recall_notes(
                persistence.store,
                settings.workspace_id,
                query=args.query,
                limit=args.limit,
            )
        else:
            records = list_notes(
                persistence.store,
                settings.workspace_id,
                limit=args.limit,
            )
    payload = {
        "workspace_id": settings.workspace_id,
        "count": len(records),
        "memories": records,
    }
    if args.format == "json":
        _emit_json(payload)
    else:
        print(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).rstrip())
    return 0


def _tools(settings: HarnessSettings, output_format: str) -> int:
    from .tools import build_tools

    records: List[Dict[str, str]] = []
    for item in build_tools(settings):
        records.append({"name": item.name, "description": item.description})
    if output_format == "json":
        _emit_json(records)
    else:
        for item in records:
            print(f"{item['name']}: {item['description']}")
    return 0


def _display_path(path: Path, repository_root: Path) -> str:
    try:
        return path.relative_to(repository_root).as_posix()
    except ValueError:
        return str(path)


def _skill_record(
    spec: SkillSpec,
    repository_root: Path,
    *,
    include_instructions: bool,
) -> Dict[str, Any]:
    grouped_resources = {
        group: [item.relative_path for item in spec.resources_in(group)]
        for group in RESOURCE_GROUPS
    }
    record: Dict[str, Any] = {
        "name": spec.name,
        "description": spec.description,
        "root": _display_path(spec.root, repository_root),
        "skill_file": _display_path(spec.skill_file, repository_root),
        "resource_count": len(spec.resources),
        "resources": grouped_resources,
    }
    if include_instructions:
        record["instructions"] = spec.instructions
    return record


def _skills(settings: HarnessSettings, args: argparse.Namespace) -> int:
    registry = SkillRegistry(settings.skills_root)
    if args.skill_command == "list":
        records = [
            _skill_record(
                spec,
                settings.repository_root,
                include_instructions=False,
            )
            for spec in registry.list()
        ]
        if args.format == "json":
            _emit_json({"count": len(records), "skills": records})
        else:
            for record in records:
                print(
                    f"{record['name']}: {record['description']} "
                    f"({record['resource_count']} resources)"
                )
        return 0

    spec = registry.get(args.name)
    if args.skill_command == "show":
        record = _skill_record(
            spec,
            settings.repository_root,
            include_instructions=True,
        )
        if args.format == "json":
            _emit_json(record)
        else:
            print(yaml.safe_dump(record, allow_unicode=True, sort_keys=False).rstrip())
        return 0

    if args.skill_command == "read":
        content = spec.read_resource(args.resource)
        if args.format == "json":
            _emit_json(
                {
                    "skill": spec.name,
                    "resource": str(args.resource).replace("\\", "/"),
                    "content": content,
                }
            )
        else:
            print(content, end="" if content.endswith("\n") else "\n")
        return 0

    raise RuntimeError(f"Unsupported skills command: {args.skill_command}")


def _emit_payload(payload: Any, output_format: str) -> None:
    if output_format == "json":
        _emit_json(payload)
    else:
        print(
            yaml.safe_dump(
                _json_safe(payload), allow_unicode=True, sort_keys=False
            ).rstrip()
        )


def _research_state_payload(
    research_id: str, state: Mapping[str, Any]
) -> Dict[str, Any]:
    return {
        "research_id": research_id,
        "phase": state.get("phase", ""),
        "control_passes": state.get("control_passes", 0),
        "research_iterations": state.get("research_iterations", 0),
        "snapshot": state.get("snapshot"),
        "gaps": state.get("gaps", []),
        "progress": state.get("progress"),
        "evaluation": state.get("evaluation"),
        "decision": state.get("decision"),
        "current_gap": state.get("current_gap"),
        "action_result": state.get("action_result"),
        "attempts_by_gap_action": state.get("attempts_by_gap_action", {}),
        "no_progress_rounds": state.get("no_progress_rounds", 0),
        "tool_calls": state.get("tool_calls", 0),
        "stop_reason": state.get("stop_reason", ""),
    }


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        kill_process_group = getattr(os, "killpg", None)
        sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
        if kill_process_group is None:
            process.kill()
        else:
            kill_process_group(process.pid, sigkill)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def _redact_secret_values(value: str) -> str:
    result = value
    for name in (
        "DEEPXIV_TOKEN",
        "SEMANTIC_SCHOLAR_API_KEY",
        "S2_API_KEY",
        "TAVILY_API_KEY",
        "GITHUB_TOKEN",
        "OPENAI_API_KEY",
        "HARNESS_FAST_API_KEY",
        "HARNESS_REASONING_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        secret = os.getenv(name, "")
        if secret:
            result = result.replace(secret, "[REDACTED]")
    return result


def _run_research_canary(
    settings: HarnessSettings,
    args: argparse.Namespace,
) -> int:
    if not args.allow_network:
        raise ValueError(
            "Online Canary execution requires explicit --allow-network authorization"
        )
    resolve_research_directory(settings, args.research_id)
    if args.stop_after != "retrieval" and not settings.model:
        raise ValueError(
            "screening or later Canary stages require HARNESS_MODEL/--model"
        )
    limits = CanaryLimits(
        max_planned_queries=args.max_planned_queries,
        max_provider_query_calls=args.max_provider_query_calls,
        max_new_unique_candidates=args.max_new_unique_candidates,
        max_selected_candidates=args.max_selected_candidates,
        max_papers_ingested=args.max_papers_ingested,
        max_actions=args.max_actions,
        deadline_seconds=args.deadline_seconds,
        provider_max_retries=args.provider_max_retries,
        wiki_write_mode="deferred" if args.defer_wiki else "immediate",
        stop_after=args.stop_after,
    )
    run_id = args.run_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
        + uuid.uuid4().hex[:8]
    )
    request_root = settings.repository_root / ".harness" / "canary-requests"
    request_root.mkdir(parents=True, exist_ok=True)
    request_path = request_root / f"{uuid.uuid4().hex}.json"
    request = {
        "research_id": args.research_id,
        "run_id": run_id,
        "limits": limits.model_dump(mode="json"),
        "source_run": str(args.source_run) if args.source_run else None,
        "base_database_path": str(settings.database_path),
        "model": settings.model,
        "model_base_url": settings.model_base_url,
        "workspace_id": settings.workspace_id,
    }
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=str(request_root),
        prefix=f".{request_path.name}.",
        suffix=".tmp",
    ) as handle:
        json.dump(request, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, request_path)

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "research_harness.canary_worker",
                "--request",
                str(request_path),
            ],
            cwd=str(settings.repository_root),
            env=dict(os.environ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
    except BaseException:
        request_path.unlink(missing_ok=True)
        raise
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=limits.deadline_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_tree(process)
        stdout, stderr = process.communicate()
    except BaseException:
        _terminate_process_tree(process)
        raise
    finally:
        request_path.unlink(missing_ok=True)

    canary_root = settings.repository_root / ".harness" / "canary" / run_id
    report_path = canary_root / "report.json"
    if timed_out and not report_path.is_file():
        canary_root.mkdir(parents=True, exist_ok=True)
        report = CanaryRunReport(
            run_id=run_id,
            research_id=args.research_id,
            status="timeout",
            stage_reached="not-started",
            stop_after=limits.stop_after,
            started_at="unknown",
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration_seconds=float(limits.deadline_seconds),
            workspace_root=canary_root.relative_to(
                settings.repository_root
            ).as_posix(),
            limits=limits,
            invariants={"hard_deadline_enforced": True},
            error_codes=("canary-deadline-exceeded",),
        )
        report_path.write_text(
            json.dumps(
                report.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    if report_path.is_file():
        report_payload = json.loads(report_path.read_text(encoding="utf-8-sig"))
        _emit_payload(report_payload, args.format)
        return 0 if report_payload.get("status") == "passed" else 1
    detail = _redact_secret_values((stderr or stdout).strip()[-2000:])
    raise RuntimeError(
        "Canary worker stopped without a report"
        + (f": {detail}" if detail else "")
    )


def _canary_publish_settings(
    settings: HarnessSettings,
    canary_root: Path,
    *,
    target: str,
) -> HarnessSettings:
    if target == "formal":
        return settings
    workspace = canary_root / "workspace"
    isolated = HarnessSettings(
        repository_root=settings.repository_root,
        wiki_root=workspace / "wiki",
        wiki_meta_root=workspace / "wiki" / "_meta",
        skills_root=settings.skills_root,
        research_root=workspace / "research",
        database_path=canary_root / "publish-staged.sqlite3",
        model=None,
        model_base_url=None,
        workspace_id=f"canary-publish:{canary_root.name}",
        context_token_budget=settings.context_token_budget,
        max_tool_iterations=settings.max_tool_iterations,
        tool_output_chars=settings.tool_output_chars,
    )
    isolated.validate()
    return isolated


def _mark_staged_candidate_published(
    settings: HarnessSettings,
    record: StagedPaperRecord,
    *,
    result: Any,
) -> Optional[str]:
    path = (settings.repository_root / record.candidate.search_run_path).resolve()
    try:
        path.relative_to(settings.repository_root.resolve())
    except ValueError:
        return None
    if not path.is_file():
        return None
    payload = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        return None
    matched = next(
        (
            item
            for item in payload.get("candidates") or []
            if isinstance(item, dict)
            and item.get("candidate_id") == record.candidate.candidate_id
        ),
        None,
    )
    if not isinstance(matched, dict) or matched.get("review_state") not in {
        "staged-for-wiki",
        "ingested",
    }:
        return None
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    matched["review_state"] = "ingested"
    matched["ingest"] = {
        "paper_id": result.paper_id,
        "status": result.status,
        "ingested_at": timestamp,
        "wiki_paths": [f"wiki/{item}" for item in result.changed_paths],
        "diagnostic_codes": list(result.diagnostic_codes),
        "stage_id": record.stage_id,
    }
    header = payload.get("run")
    if isinstance(header, dict):
        header["updated_at"] = timestamp
    rendered = yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            handle.write(rendered)
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return path.relative_to(settings.repository_root).as_posix()


def _run_publish_staged(
    settings: HarnessSettings,
    args: argparse.Namespace,
) -> int:
    if args.max_papers < 1 or args.max_papers > 100:
        raise ValueError("--max-papers must be between 1 and 100")
    canary_root = settings.repository_root / ".harness" / "canary" / args.run_id
    review_root = settings.repository_root / ".harness" / "review-runs" / args.run_id
    if canary_root.is_dir():
        run_root = canary_root
        run_kind = "canary"
    elif review_root.is_dir():
        run_root = review_root
        run_kind = "review"
    else:
        raise FileNotFoundError(
            f"No Canary or review staging run found for {args.run_id!r}"
        )
    if run_kind == "review" and args.target != "formal":
        raise ValueError(
            "Review promotions can only be published with --target formal; "
            "the review run already provides the isolation boundary"
        )
    store = StagedPaperStore(
        settings.repository_root,
        run_root / "artifacts" / "staged",
    )
    target_id = (
        f"canary:{args.run_id}"
        if args.target == "canary"
        else f"formal:{args.research_id}"
    )
    target_settings = (
        settings
        if run_kind == "review"
        else _canary_publish_settings(
            settings,
            run_root,
            target=args.target,
        )
    )
    recorder = None
    manifest = run_root / "artifacts" / "semantic-manifest.json"
    if manifest.is_file():
        from .artifacts import SemanticArtifactRecorder

        recorder = SemanticArtifactRecorder(
            settings.repository_root,
            run_root / "artifacts",
        )
    publisher = StagedWikiPublisher(
        target_settings,
        store,
        artifact_recorder=recorder,
    )
    records = store.pending(args.research_id, target_id=target_id)[: args.max_papers]
    results = []
    for position, record in enumerate(records, start=1):
        result = publisher.publish(
            record,
            target_id=target_id,
            preview=args.preview,
            action_id=f"publish-staged-{position:04d}",
        )
        handoff = None
        if not args.preview:
            handoff = _mark_staged_candidate_published(
                settings,
                record,
                result=result,
            )
        results.append(
            {
                "stage_id": record.stage_id,
                "candidate_id": record.candidate.candidate_id,
                "paper_id": result.paper_id,
                "status": result.status,
                "entities_created": len(result.created_entity_ids),
                "entities_reused": len(result.reused_entity_ids),
                "wiki_paths": [f"wiki/{item}" for item in result.changed_paths],
                "candidate_handoff": handoff,
                "model_calls": 0,
            }
        )
    _emit_payload(
        {
            "research_id": args.research_id,
            "run_id": args.run_id,
            "run_kind": run_kind,
            "target": args.target,
            "target_id": target_id,
            "preview": bool(args.preview),
            "papers_processed": len(results),
            "model_calls": 0,
            "results": results,
        },
        args.format,
    )
    return 0


def _review_run_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def _review_checkpoint_context(
    settings: HarnessSettings,
    *,
    research_id: str,
    thread_id: str,
):
    from .review_control import read_review_checkpoint
    from .review_storage import ReviewArtifactStore

    state = read_review_checkpoint(
        settings,
        research_id=research_id,
        thread_id=thread_id,
    )
    run_id = str(state.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("Review checkpoint does not contain a run_id")
    config = ReviewArtifactStore.load_config(settings, run_id)
    if config.research_id != research_id:
        raise ValueError("Review checkpoint research_id does not match run config")
    if config.thread_id != thread_id:
        raise ValueError("Review checkpoint thread does not match run config")
    return state, config, ReviewArtifactStore(settings, config)


def _review_state_payload(
    state: Mapping[str, Any],
    store: Any,
) -> Dict[str, Any]:
    readiness = store.readiness()
    paths = {
        "review": store.relative(store.report_path) if store.report_path.is_file() else None,
        "sources": store.relative(store.sources_path) if store.sources_path.is_file() else None,
        "skims": store.relative(store.skims_path) if store.skims_path.is_file() else None,
        "evidence_cards": (
            store.relative(store.cards_path) if store.cards_path.is_file() else None
        ),
        "trajectory": (
            store.relative(store.trajectory_path)
            if store.trajectory_path.is_file()
            else None
        ),
        "promotion_manifest": (
            store.relative(store.promotion_path)
            if store.promotion_path.is_file()
            else None
        ),
        "technology_map": (
            store.relative(store.technology_map_path)
            if store.technology_map_path.is_file()
            else None
        ),
        "coverage_matrix": (
            store.relative(store.coverage_path)
            if store.coverage_path.is_file()
            else None
        ),
        "research_gaps": (
            store.relative(store.gaps_path) if store.gaps_path.is_file() else None
        ),
        "nonconsensus_assessments": (
            store.relative(store.assessments_path)
            if store.assessments_path.is_file()
            else None
        ),
    }
    report_exists = store.report_path.is_file()
    sources = store.sources()
    skims = store.skims()
    deep_read_ids = store.deep_read_completed()
    source_types = {item.source_id: item.source_type for item in sources}
    synthesis_draft = store.synthesis_draft()
    report_conclusions = (
        len(synthesis_draft.core_findings)
        + len(synthesis_draft.task_and_performance)
        + len(synthesis_draft.engineering_bottlenecks)
        if synthesis_draft
        else 0
    )
    configured_seed_ids = set(store.config.seed_source_ids)
    role_counts: Dict[str, int] = {}
    for skim in skims:
        role_counts[skim.source_role] = role_counts.get(skim.source_role, 0) + 1
    coverage = store.coverage()
    gaps = tuple(item for item in store.gaps() if item.status == "open")
    technology_map = store.technology_map()
    relation_candidates = technology_map.get("relation_candidates", [])
    assessments = store.assessments()
    assessment_results: Dict[str, int] = {}
    for assessment in assessments:
        assessment_results[assessment.result] = (
            assessment_results.get(assessment.result, 0) + 1
        )
    return {
        "schema_version": "review-status-0.1",
        "research_id": store.config.research_id,
        "run_id": store.config.run_id,
        "thread_id": store.config.thread_id,
        "profile": store.config.profile,
        "models": {
            "fast": store.config.fast_model,
            "fast_base_url": store.config.fast_model_base_url,
            "reasoning": store.config.reasoning_model,
            "reasoning_base_url": store.config.reasoning_model_base_url,
            "fingerprint": store.config.model_fingerprint,
            "single_model_fallback_used": store.config.single_model_fallback_used,
        },
        "phase": "completed" if report_exists else state.get("phase", "not-started"),
        "completed": bool(state.get("completed") or report_exists),
        "stop_reason": state.get("stop_reason", ""),
        "round_number": int(state.get("round_number") or 0),
        "funnel": {
            "sources": len(sources),
            "seed_sources": sum(
                item.source_id in configured_seed_ids for item in sources
            ),
            "paper_sources": sum(
                item.source_type == "paper" for item in sources
            ),
            "skims": len(skims),
            "paper_skims": sum(
                source_types.get(item.source_id) == "paper" for item in skims
            ),
            "deep_reads": len(deep_read_ids),
            "deep_read_papers": sum(
                source_types.get(source_id) == "paper"
                for source_id in deep_read_ids
            ),
            "evidence_cards": len(store.cards()),
            "report_conclusions": report_conclusions,
            "promotion_limit": store.config.max_promotions,
        },
        "readiness": readiness.model_dump(mode="json") if readiness else None,
        "research_map": {
            "source_role_counts": dict(sorted(role_counts.items())),
            "facet_coverage": (
                {
                    item.facet: {
                        "status": item.status,
                        "independent_sources": len(item.independent_source_ids),
                        "evidence_cards": len(item.evidence_card_ids),
                    }
                    for item in coverage.facets
                }
                if coverage
                else {}
            ),
            "top_unresolved_gaps": [
                item.model_dump(mode="json") for item in gaps[:5]
            ],
            "relation_candidates": len(relation_candidates),
            "nonconsensus_assessments": {
                "total": len(assessments),
                "evidence_pool": sum(
                    item.basis == "evidence-pool" for item in assessments
                ),
                "results": dict(sorted(assessment_results.items())),
            },
            "next_recommended_pivot": gaps[0].question if gaps else None,
        },
        "run_local_errors": len(store.error_events()),
        "progress": store.progress(),
        "paths": paths,
        "fast_loop_wiki_written": False,
    }


def _run_review_start(
    settings: HarnessSettings,
    args: argparse.Namespace,
    *,
    canary: bool,
) -> int:
    from .review_control import ReviewController
    from .model_client import ReviewModelBundle
    from .progress import ConsoleProgress
    from .review_models import ReviewRunConfig, SourceScreening
    from .review_semantics import LangChainReviewSemanticEngine
    from .review_storage import (
        ReviewArtifactStore,
        load_review_scope,
        load_review_seed_sources,
    )

    if not args.allow_network:
        raise ValueError("research review start/canary requires --allow-network")
    scope = load_review_scope(settings, args.research_id)
    profile = "smoke" if canary else args.profile
    seed_manifest = getattr(args, "seed_manifest", None)
    if profile == "seed5" and seed_manifest is None:
        seed_manifest = (
            settings.research_root / args.research_id / "seed-papers.yaml"
        )
    seed_sources = (
        load_review_seed_sources(settings, args.research_id, seed_manifest)
        if seed_manifest is not None
        else ()
    )
    if profile == "seed5" and len(seed_sources) != 5:
        raise ValueError("seed5 requires exactly five curated paper sources")
    bundle = ReviewModelBundle.from_env(
        settings,
        allow_single_model_fallback=bool(args.allow_single_model_fallback),
        require_reasoning=not canary and profile != "smoke",
    )
    run_id = args.run_id or _review_run_id("review-smoke" if canary else "review")
    thread_id = args.thread or f"review:{args.research_id}:{run_id}"
    args.thread = thread_id
    config = ReviewRunConfig.for_profile(
        research_id=args.research_id,
        run_id=run_id,
        thread_id=thread_id,
        profile=profile,
        question=scope.question,
        title=scope.title,
        required_facets=scope.required_facets,
        candidate_hypotheses=scope.candidate_hypotheses,
        seed_source_ids=tuple(item.source_id for item in seed_sources),
        allow_network=True,
        allow_single_model_fallback=bool(args.allow_single_model_fallback),
        canary=canary,
        stop_after=args.stop_after,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        fast_model=bundle.fast.model,
        fast_model_base_url=bundle.fast.base_url,
        reasoning_model=bundle.reasoning.model,
        reasoning_model_base_url=bundle.reasoning.base_url,
        model_fingerprint=bundle.fingerprint,
        single_model_fallback_used=bundle.single_model_fallback,
    )
    semantic_engine = LangChainReviewSemanticEngine(bundle, settings.skills_root)
    store = ReviewArtifactStore(settings, config)
    if seed_sources:
        store.initialize()
        store.write_sources(seed_sources)
        store.write_screenings(
            tuple(
                SourceScreening(
                    source_id=item.source_id,
                    source_role=str(
                        item.metadata.get("source_role") or "primary-study"
                    ),
                    label="core",
                    relevance_score=1.0,
                    evidence_potential=1.0,
                    engineering_value=0.9,
                    counterevidence_value=0.7,
                    reason=str(item.metadata["selection_rationale"]),
                    target_facets=item.target_facets,
                )
                for item in seed_sources
            )
        )
    with ConsoleProgress(path=store.progress_path) as progress:
        with ReviewController(
            settings,
            config=config,
            scope=scope,
            semantic_engine=semantic_engine,
            progress=progress,
        ) as controller:
            state = controller.start()
    _emit_payload(_review_state_payload(state, store), args.format)
    return 0


def _run_review_resume(settings: HarnessSettings, args: argparse.Namespace) -> int:
    from .review_control import ReviewController
    from .progress import ConsoleProgress

    _state, config, store = _review_checkpoint_context(
        settings,
        research_id=args.research_id,
        thread_id=args.thread,
    )
    with ConsoleProgress(path=store.progress_path) as progress:
        with ReviewController(settings, config=config, progress=progress) as controller:
            state = controller.resume(mode=args.mode)
    _emit_payload(_review_state_payload(state, store), args.format)
    return 0


def _run_review_status(settings: HarnessSettings, args: argparse.Namespace) -> int:
    state, _config, store = _review_checkpoint_context(
        settings,
        research_id=args.research_id,
        thread_id=args.thread,
    )
    _emit_payload(_review_state_payload(state, store), args.format)
    return 0


def _run_review_synthesize(settings: HarnessSettings, args: argparse.Namespace) -> int:
    from .review_control import ReviewController
    from .progress import ConsoleProgress
    from .review_providers import ReviewProviderRegistry

    _state, config, store = _review_checkpoint_context(
        settings,
        research_id=args.research_id,
        thread_id=args.thread,
    )
    providers = ReviewProviderRegistry(
        settings.repository_root,
        store.working_root,
        network_concurrency=config.network_concurrency,
    )
    with ConsoleProgress(path=store.progress_path) as progress:
        with ReviewController(
            settings,
            config=config,
            providers=providers,
            progress=progress,
        ) as controller:
            state = controller.synthesize_now()
    _emit_payload(_review_state_payload(state, store), args.format)
    return 0


def _run_review_promote(settings: HarnessSettings, args: argparse.Namespace) -> int:
    from .review_promotion import ReviewPromoter

    _state, config, store = _review_checkpoint_context(
        settings,
        research_id=args.research_id,
        thread_id=args.thread,
    )
    promoter = ReviewPromoter(settings, store)
    if args.execute:
        payload = promoter.execute(
            manifest_path=args.manifest,
            allow_network=bool(args.allow_network or config.allow_network),
        )
    else:
        payload = promoter.preview(args.manifest)
    _emit_payload(payload, args.format)
    return 0


def _run_review(settings: HarnessSettings, args: argparse.Namespace) -> int:
    if args.review_command == "start":
        return _run_review_start(settings, args, canary=False)
    if args.review_command == "canary":
        return _run_review_start(settings, args, canary=True)
    if args.review_command == "resume":
        return _run_review_resume(settings, args)
    if args.review_command == "status":
        return _run_review_status(settings, args)
    if args.review_command == "synthesize":
        return _run_review_synthesize(settings, args)
    if args.review_command == "promote":
        return _run_review_promote(settings, args)
    raise RuntimeError(f"Unsupported review command: {args.review_command}")


def _research(settings: HarnessSettings, args: argparse.Namespace) -> int:
    if args.research_command == "review":
        return _run_review(settings, args)

    if args.research_command == "canary":
        return _run_research_canary(settings, args)

    if args.research_command == "publish-staged":
        return _run_publish_staged(settings, args)

    if args.research_command == "export-trajectory":
        destination = trajectory_directory(
            settings.research_root,
            args.research_id,
            args.thread,
        )
        with AutonomousResearchController(
            settings,
            research_id=args.research_id,
        ) as controller:
            history = tuple(controller.get_state_history(args.thread))
            if not history:
                raise ValueError(
                    f"No autonomous checkpoint history exists for thread {args.thread!r}"
                )
            count = export_checkpoint_trajectory(
                history,
                destination=destination / "trajectory.jsonl",
                research_id=args.research_id,
                thread_id=args.thread,
            )
        ensure_annotation_sidecar(
            destination / "human-annotations.yaml",
            research_id=args.research_id,
            thread_id=args.thread,
        )
        _emit_payload(
            {
                "research_id": args.research_id,
                "thread_id": args.thread,
                "checkpoint_records": count,
                "trajectory_path": (
                    destination / "trajectory.jsonl"
                ).relative_to(settings.repository_root).as_posix(),
                "annotations_path": (
                    destination / "human-annotations.yaml"
                ).relative_to(settings.repository_root).as_posix(),
                "source_of_truth": "SQLite LangGraph checkpoints",
            },
            args.format,
        )
        return 0
    if args.research_command == "inspect":
        snapshot = inspect_research(settings, args.research_id)
        _emit_payload(snapshot.model_dump(mode="json"), args.format)
        return 0

    if args.research_command == "evaluate":
        if args.iteration < 0 or args.tool_calls < 0 or args.no_progress_rounds < 0:
            raise ValueError("Research counters cannot be negative")
        criteria = load_done_criteria(
            settings,
            args.research_id,
            args.criteria,
        )
        snapshot = inspect_research(settings, args.research_id)
        gaps = evaluate_gaps(snapshot, criteria)
        evaluation = check_done(
            snapshot,
            criteria,
            gaps,
            research_iteration=args.iteration,
            tool_calls=args.tool_calls,
            no_progress_rounds=args.no_progress_rounds,
        )
        decision = decide_next_action(gaps, evaluation)
        _emit_payload(
            {
                "research_id": args.research_id,
                "criteria": criteria.model_dump(mode="json"),
                "snapshot": snapshot.model_dump(mode="json"),
                "gaps": [gap.model_dump(mode="json") for gap in gaps],
                "evaluation": evaluation.model_dump(mode="json"),
                "decision": decision.model_dump(mode="json"),
            },
            args.format,
        )
        return 0

    if args.research_command == "step":
        with ResearchController(
            settings,
            research_id=args.research_id,
            criteria_path=args.criteria,
        ) as controller:
            state = controller.invoke(thread_id=args.thread)
        _emit_payload(
            {
                "research_id": args.research_id,
                "phase": state.get("phase", ""),
                "control_passes": state.get("control_passes", 0),
                "research_iterations": state.get("research_iterations", 0),
                "snapshot": state.get("snapshot"),
                "gaps": state.get("gaps", []),
                "progress": state.get("progress"),
                "evaluation": state.get("evaluation"),
                "decision": state.get("decision"),
                "current_gap": state.get("current_gap"),
                "no_progress_rounds": state.get("no_progress_rounds", 0),
                "tool_calls": state.get("tool_calls", 0),
                "stop_reason": state.get("stop_reason", ""),
            },
            args.format,
        )
        return 0

    if args.research_command == "run":
        with AutonomousResearchController(
            settings,
            research_id=args.research_id,
            criteria_path=args.criteria,
        ) as controller:
            state = controller.invoke(
                thread_id=args.thread,
                allow_network=args.allow_network,
            )
        _emit_payload(_research_state_payload(args.research_id, state), args.format)
        return 0

    if args.research_command == "resume":
        with AutonomousResearchController(
            settings,
            research_id=args.research_id,
            criteria_path=args.criteria,
        ) as controller:
            state = controller.resume(
                thread_id=args.thread,
                allow_network=args.allow_network,
                mode=args.mode,
            )
        _emit_payload(_research_state_payload(args.research_id, state), args.format)
        return 0

    raise RuntimeError(f"Unsupported research command: {args.research_command}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_utf8_stdio()
    args = parse_args(argv)
    try:
        settings = _settings(args)
        commands = {
            "doctor": lambda: _doctor(settings, args.format),
            "run": lambda: _run(settings, args),
            "chat": lambda: _chat(settings, args),
            "state": lambda: _state(settings, args),
            "memories": lambda: _memories(settings, args),
            "tools": lambda: _tools(settings, args.format),
            "skills": lambda: _skills(settings, args),
            "research": lambda: _research(settings, args),
        }
        return commands[args.command]()
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        if args.command == "research" and args.research_command in {"run", "resume"}:
            thread_id = args.thread or f"research:{args.research_id}"
            print(f"Thread: {thread_id}", file=sys.stderr)
            print("Checkpoint preserved.", file=sys.stderr)
            print("Resume with the same --thread.", file=sys.stderr)
        elif (
            args.command == "research"
            and args.research_command == "review"
            and args.review_command in {"start", "resume", "synthesize", "canary"}
        ):
            print(f"Thread: {args.thread}", file=sys.stderr)
            print("Checkpoint and completed batch artifacts preserved.", file=sys.stderr)
            print(
                "Resume with: research review resume "
                f"{args.research_id} --thread {args.thread}",
                file=sys.stderr,
            )
        return 130
    except (
        FileNotFoundError,
        KeyError,
        ValueError,
        RuntimeError,
        OSError,
        yaml.YAMLError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
