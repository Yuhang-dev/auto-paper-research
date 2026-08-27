"""Command-line interface for the LangGraph research harness."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage

from tools.wiki.indexer import build_index
from tools.wiki.validator import validate_index

from .config import HarnessSettings
from .graph import ResearchHarness
from .memory import list_notes, recall_notes
from .persistence import HarnessPersistence
from .research_control import AutonomousResearchController, ResearchController
from .research_evaluation import (
    check_done,
    decide_next_action,
    evaluate_gaps,
    inspect_research,
    load_done_criteria,
)
from .skill_registry import RESOURCE_GROUPS, SkillRegistry, SkillSpec


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _add_format(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("text", "json"), default="text")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LangGraph orchestration for the LLM-Wiki research Harness."
    )
    parser.add_argument("--db", type=Path, help="SQLite path; C: is rejected.")
    parser.add_argument(
        "--model", help="LangChain model string, e.g. openai:deepseek-v4-flash."
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
    return parser.parse_args(argv)


def _settings(args: argparse.Namespace) -> HarnessSettings:
    return HarnessSettings.from_env(
        database_path=args.db,
        model=args.model,
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
    packages = {}
    for name in (
        "langchain",
        "langgraph",
        "langgraph-checkpoint-sqlite",
        "langchain-openai",
        "deepxiv-sdk",
        "pydantic",
    ):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    path = settings.database_path
    result = {
        "ok": diagnostic_counts["ERROR"] == 0,
        "repository_root": str(settings.repository_root),
        "database_path": str(path),
        "database_drive": path.drive,
        "database_on_c": path.drive.casefold() == "c:",
        "database_exists": path.exists(),
        "database_bytes": path.stat().st_size if path.exists() else 0,
        "checkpoint_counts": checkpoint_counts,
        "model": settings.model,
        "model_configured": bool(settings.model),
        "openai_key_configured": bool(os.getenv("OPENAI_API_KEY")),
        "deepxiv_token_configured": bool(os.getenv("DEEPXIV_TOKEN")),
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


def _research(settings: HarnessSettings, args: argparse.Namespace) -> int:
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
                "action_result": state.get("action_result"),
                "attempts_by_gap_action": state.get("attempts_by_gap_action", {}),
                "no_progress_rounds": state.get("no_progress_rounds", 0),
                "tool_calls": state.get("tool_calls", 0),
                "stop_reason": state.get("stop_reason", ""),
            },
            args.format,
        )
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
