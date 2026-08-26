"""Command-line interface for the read-only research Wiki engine."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

from .indexer import WikiIndex, build_index, write_artifacts
from .models import Diagnostic, json_safe
from .query import (
    backlinks_for,
    entity_payload,
    neighbors_for,
    related_entities,
    resolve_entity,
    search_entities,
    structured_query,
)
from .validator import validate_index


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WIKI_ROOT = REPOSITORY_ROOT / "wiki"
DEFAULT_META_ROOT = DEFAULT_WIKI_ROOT / "_meta"


def _configure_utf8_stdio() -> None:
    """Keep Unicode entity names printable on Windows code pages."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _add_format(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only LLM research Wiki engine.")
    parser.add_argument("--wiki-root", type=Path, default=DEFAULT_WIKI_ROOT)
    parser.add_argument("--meta-root", type=Path, default=DEFAULT_META_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser("index", help="Build deterministic JSON indexes.")
    _add_format(index_parser)

    validate_parser = subparsers.add_parser("validate", help="Validate schema, links, and evidence.")
    validate_parser.add_argument("--strict", action="store_true", help="Fail on warnings.")
    _add_format(validate_parser)

    search_parser = subparsers.add_parser("search", help="Search IDs, titles, aliases, and text.")
    search_parser.add_argument("text")
    search_parser.add_argument("--type", dest="entity_type")
    search_parser.add_argument("--status")
    search_parser.add_argument("--year", type=int)
    _add_format(search_parser)

    show_parser = subparsers.add_parser("show", help="Show one entity.")
    show_parser.add_argument("reference")
    show_parser.add_argument("--body", action="store_true")
    _add_format(show_parser)

    backlinks_parser = subparsers.add_parser("backlinks", help="Show incoming links and relations.")
    backlinks_parser.add_argument("reference")
    _add_format(backlinks_parser)

    neighbors_parser = subparsers.add_parser("neighbors", help="Show one-hop graph neighbors.")
    neighbors_parser.add_argument("reference")
    _add_format(neighbors_parser)

    related_parser = subparsers.add_parser("related", help="Traverse typed relations.")
    related_parser.add_argument("reference")
    related_parser.add_argument("--depth", type=int, default=2)
    _add_format(related_parser)

    query_parser = subparsers.add_parser("query", help="Filter structured Wiki entities.")
    query_parser.add_argument("--type", dest="entity_type")
    query_parser.add_argument("--status")
    query_parser.add_argument("--benchmark")
    query_parser.add_argument("--method")
    query_parser.add_argument("--model")
    query_parser.add_argument("--min-context", type=int)
    query_parser.add_argument("--max-context", type=int)
    query_parser.add_argument("--sparsity-target")
    query_parser.add_argument("--min-sparsity", type=float)
    query_parser.add_argument("--max-sparsity", type=float)
    _add_format(query_parser)

    stats_parser = subparsers.add_parser("stats", help="Show corpus and diagnostic statistics.")
    _add_format(stats_parser)
    return parser.parse_args(argv)


def _load(args: argparse.Namespace) -> WikiIndex:
    wiki_root = args.wiki_root
    if not wiki_root.is_absolute():
        wiki_root = REPOSITORY_ROOT / wiki_root
    meta_root = args.meta_root
    if not meta_root.is_absolute():
        meta_root = REPOSITORY_ROOT / meta_root
    return build_index(wiki_root, meta_root)


def _emit_json(value: Any) -> None:
    print(json.dumps(json_safe(value), ensure_ascii=False, indent=2, sort_keys=True))


def _diagnostic_text(diagnostic: Diagnostic) -> str:
    location = diagnostic.path or "<wiki>"
    if diagnostic.line is not None:
        location += f":{diagnostic.line}"
    field = f" field={diagnostic.field}" if diagnostic.field else ""
    entity = f" entity={diagnostic.entity_id}" if diagnostic.entity_id else ""
    return (
        f"{diagnostic.severity} {diagnostic.code} {location}"
        f"{entity}{field}: {diagnostic.message}"
    )


def _entity_lines(records: Sequence[Dict[str, Any]]) -> None:
    if not records:
        print("No matching entities.")
        return
    for record in records:
        score = f" score={record['score']}" if "score" in record else ""
        print(
            f"{record.get('id')} [{record.get('type')}/{record.get('status')}]"
            f"{score} {record.get('title')}"
        )


def _resolve_required(index: WikiIndex, reference: str) -> tuple:
    resolution, entity, candidates = resolve_entity(index, reference)
    if entity is None:
        if candidates:
            print(
                f"ERROR: {reference!r} is ambiguous: {', '.join(candidates)}",
                file=sys.stderr,
            )
        else:
            print(f"ERROR: entity not found: {reference}", file=sys.stderr)
        return resolution, None
    return resolution, entity


def _command_index(index: WikiIndex, args: argparse.Namespace) -> int:
    diagnostics = validate_index(index)
    written = write_artifacts(index, diagnostics)
    errors = sum(item.severity == "ERROR" for item in diagnostics)
    if args.format == "json":
        _emit_json(
            {
                "written": {name: str(path) for name, path in sorted(written.items())},
                "stats": index.stats(diagnostics),
            }
        )
    else:
        print(f"Indexed {len(index.unique_entities())} unique entities.")
        print(f"Source hash: {index.source_hash}")
        for name, path in sorted(written.items()):
            print(f"{name}: {path}")
        print(f"Validation errors: {errors}")
    return 1 if errors else 0


def _command_validate(index: WikiIndex, args: argparse.Namespace) -> int:
    diagnostics = validate_index(index)
    write_artifacts(index, diagnostics)
    if args.format == "json":
        _emit_json([item.as_dict() for item in diagnostics])
    else:
        for diagnostic in diagnostics:
            print(_diagnostic_text(diagnostic))
        counts = {
            severity: sum(item.severity == severity for item in diagnostics)
            for severity in ("ERROR", "WARNING", "INFO")
        }
        print(
            "Validation: "
            f"{counts['ERROR']} error(s), "
            f"{counts['WARNING']} warning(s), "
            f"{counts['INFO']} info item(s)"
        )
    has_errors = any(item.severity == "ERROR" for item in diagnostics)
    has_warnings = any(item.severity == "WARNING" for item in diagnostics)
    return 1 if has_errors or (args.strict and has_warnings) else 0


def _command_search(index: WikiIndex, args: argparse.Namespace) -> int:
    records = search_entities(
        index,
        args.text,
        entity_type=args.entity_type,
        status=args.status,
        year=args.year,
    )
    if args.format == "json":
        _emit_json(records)
    else:
        _entity_lines(records)
    return 0


def _command_show(index: WikiIndex, args: argparse.Namespace) -> int:
    resolution, entity = _resolve_required(index, args.reference)
    if entity is None:
        return 2
    record = entity_payload(entity, include_body=args.body)
    record["resolution"] = resolution
    if args.format == "json":
        _emit_json(record)
    else:
        print(yaml.safe_dump(record["metadata"], allow_unicode=True, sort_keys=False).rstrip())
        print(f"path: {record['path']}")
        print(f"resolution: {resolution}")
        if args.body:
            print()
            print(entity.body)
    return 0


def _command_backlinks(index: WikiIndex, args: argparse.Namespace) -> int:
    _, entity = _resolve_required(index, args.reference)
    if entity is None:
        return 2
    value = backlinks_for(index, str(entity.entity_id))
    if args.format == "json":
        _emit_json(value)
    else:
        print(f"Structured backlinks for {entity.entity_id}:")
        for item in value["structured"]:
            print(f"- {item['source']} --{item['relation']}--> {entity.entity_id}")
        print(f"Navigational backlinks for {entity.entity_id}:")
        for item in value["navigational"]:
            print(f"- {item['source']} at {item['path']}:{item['line']}")
    return 0


def _command_neighbors(index: WikiIndex, args: argparse.Namespace) -> int:
    _, entity = _resolve_required(index, args.reference)
    if entity is None:
        return 2
    value = neighbors_for(index, str(entity.entity_id))
    if args.format == "json":
        _emit_json(value)
    else:
        for key in (
            "structured_outgoing",
            "structured_incoming",
            "navigational_outgoing",
            "navigational_incoming",
        ):
            print(f"{key}:")
            for item in value[key]:
                print(f"- {json.dumps(item, ensure_ascii=False, sort_keys=True)}")
    return 0


def _command_related(index: WikiIndex, args: argparse.Namespace) -> int:
    if args.depth < 1 or args.depth > 8:
        print("ERROR: depth must be between 1 and 8", file=sys.stderr)
        return 2
    _, entity = _resolve_required(index, args.reference)
    if entity is None:
        return 2
    records = related_entities(index, str(entity.entity_id), depth=args.depth)
    if args.format == "json":
        _emit_json(records)
    else:
        _entity_lines(records)
    return 0


def _command_query(index: WikiIndex, args: argparse.Namespace) -> int:
    records = structured_query(
        index,
        entity_type=args.entity_type,
        status=args.status,
        benchmark=args.benchmark,
        method=args.method,
        model=args.model,
        min_context=args.min_context,
        max_context=args.max_context,
        sparsity_target=args.sparsity_target,
        min_sparsity=args.min_sparsity,
        max_sparsity=args.max_sparsity,
    )
    if args.format == "json":
        _emit_json(records)
    else:
        _entity_lines(records)
    return 0


def _command_stats(index: WikiIndex, args: argparse.Namespace) -> int:
    diagnostics = validate_index(index)
    value = index.stats(diagnostics)
    if args.format == "json":
        _emit_json(value)
    else:
        print(yaml.safe_dump(value, allow_unicode=True, sort_keys=False).rstrip())
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_utf8_stdio()
    args = parse_args(argv)
    try:
        index = _load(args)
        commands = {
            "index": _command_index,
            "validate": _command_validate,
            "search": _command_search,
            "show": _command_show,
            "backlinks": _command_backlinks,
            "neighbors": _command_neighbors,
            "related": _command_related,
            "query": _command_query,
            "stats": _command_stats,
        }
        return commands[args.command](index, args)
    except (FileNotFoundError, ValueError, OSError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
