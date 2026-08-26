"""Build deterministic, rebuildable indexes from Wiki Markdown."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .models import Diagnostic, Edge, Entity, ResolvedLink, json_safe, listify
from .parser import discover_markdown, parse_wiki
from .resolver import Resolver
from .schema import WikiSchema


@dataclass
class WikiIndex:
    wiki_root: Path
    meta_root: Path
    schema: WikiSchema
    entities: List[Entity]
    resolver: Resolver
    edges: List[Edge]
    links: List[ResolvedLink]
    source_hash: str

    def unique_entities(self) -> Dict[str, Entity]:
        result: Dict[str, Entity] = {}
        for entity in self.entities:
            entity_id = entity.entity_id
            if entity_id and entity_id not in result:
                result[entity_id] = entity
        return result

    def entity_record(self, entity: Entity) -> Dict[str, Any]:
        return {
            "id": entity.entity_id,
            "type": entity.entity_type,
            "title": entity.title,
            "aliases": entity.aliases,
            "status": entity.metadata.get("status"),
            "schema_version": entity.schema_version or "legacy",
            "path": entity.relative_path,
            "metadata": json_safe(entity.metadata),
        }

    def backlinks(self) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
        result: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
            entity_id: {"structured": [], "navigational": []}
            for entity_id in sorted(self.unique_entities())
        }
        for edge in self.edges:
            if edge.target not in result:
                continue
            rule = self.schema.relations.get(edge.relation)
            result[edge.target]["structured"].append(
                {
                    "source": edge.source,
                    "relation": edge.relation,
                    "inverse": rule.inverse if rule else None,
                    "origin": edge.origin,
                    "origin_path": edge.origin_path,
                }
            )
        for link in self.links:
            if not link.target or link.target not in result:
                continue
            result[link.target]["navigational"].append(
                {
                    "source": link.source,
                    "path": link.path,
                    "line": link.line,
                    "raw_target": link.raw_target,
                    "resolution": link.resolution,
                }
            )
        for value in result.values():
            value["structured"] = sorted(
                value["structured"],
                key=lambda item: (
                    str(item["source"]),
                    str(item["relation"]),
                    str(item["origin_path"]),
                ),
            )
            value["navigational"] = sorted(
                value["navigational"],
                key=lambda item: (
                    str(item["source"]),
                    str(item["path"]),
                    int(item["line"]),
                ),
            )
        return result

    def stats(self, diagnostics: Sequence[Diagnostic] = ()) -> Dict[str, Any]:
        unique = self.unique_entities()
        by_type = Counter(
            str(entity.entity_type or "unknown") for entity in unique.values()
        )
        by_status = Counter(
            str(entity.metadata.get("status") or "unknown") for entity in unique.values()
        )
        by_schema = Counter(
            str(entity.schema_version or "legacy") for entity in unique.values()
        )
        by_relation = Counter(edge.relation for edge in self.edges)
        by_severity = Counter(diagnostic.severity for diagnostic in diagnostics)
        unresolved_links = sum(link.target is None for link in self.links)
        return {
            "source_hash": self.source_hash,
            "page_files": len(self.entities),
            "unique_entities": len(unique),
            "duplicate_ids": len(self.resolver.duplicate_ids()),
            "entities_by_type": dict(sorted(by_type.items())),
            "entities_by_status": dict(sorted(by_status.items())),
            "entities_by_schema": dict(sorted(by_schema.items())),
            "structured_edges": len(self.edges),
            "edges_by_relation": dict(sorted(by_relation.items())),
            "navigational_links": len(self.links),
            "unresolved_navigational_links": unresolved_links,
            "diagnostics_by_severity": {
                severity: by_severity.get(severity, 0)
                for severity in ("ERROR", "WARNING", "INFO")
            },
        }

    def artifacts(
        self,
        diagnostics: Sequence[Diagnostic] = (),
    ) -> Dict[str, Any]:
        unique = self.unique_entities()
        entities_payload = {
            "schema_version": self.schema.version,
            "source_hash": self.source_hash,
            "entities": {
                entity_id: self.entity_record(unique[entity_id])
                for entity_id in sorted(unique)
            },
        }
        aliases_payload = {
            "schema_version": self.schema.version,
            "source_hash": self.source_hash,
            "aliases": self.resolver.alias_map(),
        }
        edges_payload = {
            "schema_version": self.schema.version,
            "source_hash": self.source_hash,
            "edges": [
                edge.as_dict(
                    inverse=(
                        self.schema.relations[edge.relation].inverse
                        if edge.relation in self.schema.relations
                        else None
                    )
                )
                for edge in self.edges
            ],
        }
        backlinks_payload = {
            "schema_version": self.schema.version,
            "source_hash": self.source_hash,
            "backlinks": self.backlinks(),
        }
        diagnostics_payload = {
            "schema_version": self.schema.version,
            "source_hash": self.source_hash,
            "diagnostics": [
                diagnostic.as_dict()
                for diagnostic in sorted(diagnostics, key=lambda item: item.sort_key)
            ],
        }
        stats_payload = {
            "schema_version": self.schema.version,
            **self.stats(diagnostics),
        }
        return {
            "entities.json": entities_payload,
            "aliases.json": aliases_payload,
            "edges.json": edges_payload,
            "backlinks.json": backlinks_payload,
            "diagnostics.json": diagnostics_payload,
            "stats.json": stats_payload,
        }


def _source_hash(wiki_root: Path, meta_root: Path) -> str:
    digest = hashlib.sha256()
    inputs: List[Tuple[str, Path]] = [
        (path.relative_to(wiki_root).as_posix(), path)
        for path in discover_markdown(wiki_root)
    ]
    for name in ("schema.yaml", "relation-types.yaml"):
        path = meta_root / name
        inputs.append((f"_meta/{name}", path))
    for label, path in sorted(inputs, key=lambda item: item[0]):
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _relation_edges(entity: Entity, schema: WikiSchema) -> Iterable[Edge]:
    entity_id = entity.entity_id
    if not entity_id:
        return

    edges: List[Edge] = []
    relations = entity.metadata.get("relations", {})
    if isinstance(relations, Mapping):
        for relation, raw_targets in relations.items():
            for raw_target in listify(raw_targets):
                edges.append(
                    Edge(
                        source=entity_id,
                        relation=str(relation),
                        target=str(raw_target),
                        origin="relation",
                        origin_path=entity.relative_path,
                        origin_field=f"relations.{relation}",
                    )
                )

    entity_type = entity.entity_type or ""
    for field_name, field_rule in schema.relation_fields(entity_type).items():
        if not isinstance(field_rule, Mapping):
            continue
        raw_value = entity.metadata.get(field_name)
        relation = str(field_rule.get("relation") or "")
        direction = str(field_rule.get("direction") or "outbound")
        for raw_target in listify(raw_value):
            target = str(raw_target)
            if direction == "inbound":
                source_id, target_id = target, entity_id
            else:
                source_id, target_id = entity_id, target
            edges.append(
                Edge(
                    source=source_id,
                    relation=relation,
                    target=target_id,
                    origin="relation-field",
                    origin_path=entity.relative_path,
                    origin_field=str(field_name),
                )
            )
    return edges


def _deduplicate_edges(edges: Iterable[Edge]) -> List[Edge]:
    by_key: Dict[Tuple[str, str, str], Edge] = {}
    for edge in edges:
        by_key.setdefault(edge.key, edge)
    return sorted(
        by_key.values(),
        key=lambda item: (
            item.source,
            item.relation,
            item.target,
            item.origin_path,
            item.origin,
        ),
    )


def _resolve_links(entities: Sequence[Entity], resolver: Resolver) -> List[ResolvedLink]:
    resolved: List[ResolvedLink] = []
    for entity in entities:
        source = entity.entity_id or f"@path:{entity.relative_path}"
        for link in entity.links:
            resolution, target, candidates = resolver.resolve_reference(link.target)
            resolved.append(
                ResolvedLink(
                    source=source,
                    target=target,
                    raw_target=link.target,
                    label=link.label,
                    line=link.line,
                    path=entity.relative_path,
                    resolution=resolution,
                    candidates=candidates,
                )
            )
    return sorted(
        resolved,
        key=lambda item: (
            item.path,
            item.line,
            item.raw_target,
            item.source,
        ),
    )


def build_index(wiki_root: Path, meta_root: Optional[Path] = None) -> WikiIndex:
    wiki_root = wiki_root.resolve()
    meta_root = (meta_root or wiki_root / "_meta").resolve()
    if not wiki_root.is_dir():
        raise FileNotFoundError(f"Wiki root does not exist: {wiki_root}")
    schema = WikiSchema.load(meta_root)
    entities = parse_wiki(wiki_root)
    resolver = Resolver(entities, schema)
    edges = _deduplicate_edges(
        edge
        for entity in entities
        for edge in _relation_edges(entity, schema)
    )
    links = _resolve_links(entities, resolver)
    return WikiIndex(
        wiki_root=wiki_root,
        meta_root=meta_root,
        schema=schema,
        entities=entities,
        resolver=resolver,
        edges=edges,
        links=links,
        source_hash=_source_hash(wiki_root, meta_root),
    )


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(
        json_safe(value),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    temporary_path: Optional[Path] = None
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
            temporary_path = Path(handle.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def write_artifacts(
    index: WikiIndex,
    diagnostics: Sequence[Diagnostic],
    generated_root: Optional[Path] = None,
) -> Dict[str, Path]:
    generated_root = (generated_root or index.wiki_root / "_generated").resolve()
    try:
        generated_root.relative_to(index.wiki_root)
    except ValueError as exc:
        raise ValueError("Generated artifacts must stay inside the Wiki root") from exc

    written: Dict[str, Path] = {}
    for filename, payload in index.artifacts(diagnostics).items():
        target = generated_root / filename
        _write_json_atomic(target, payload)
        written[filename] = target
    return written
