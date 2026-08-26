"""Read-only search and graph queries over an in-memory Wiki index."""

from __future__ import annotations

from collections import deque
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .indexer import WikiIndex
from .models import Entity, json_safe, listify
from .resolver import normalize_lookup
from .schema import dotted_get


def entity_payload(entity: Entity, include_body: bool = False) -> Dict[str, Any]:
    result = {
        "id": entity.entity_id,
        "type": entity.entity_type,
        "title": entity.title,
        "aliases": entity.aliases,
        "status": entity.metadata.get("status"),
        "schema_version": entity.schema_version or "legacy",
        "path": entity.relative_path,
        "metadata": json_safe(entity.metadata),
    }
    if include_body:
        result["body"] = entity.body
    return result


def resolve_entity(index: WikiIndex, reference: str) -> Tuple[str, Optional[Entity], Sequence[str]]:
    resolution, entity_id, candidates = index.resolver.resolve_reference(reference)
    if entity_id:
        return resolution, index.resolver.exact_entity(entity_id), candidates
    return resolution, None, candidates


def _flatten_strings(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _flatten_strings(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _flatten_strings(item)
    elif value not in (None, ""):
        yield str(value)


def search_entities(
    index: WikiIndex,
    text: str,
    *,
    entity_type: Optional[str] = None,
    status: Optional[str] = None,
    year: Optional[int] = None,
) -> List[Dict[str, Any]]:
    needle = normalize_lookup(text)
    results: List[Tuple[int, str, Entity]] = []
    for entity_id, entity in index.unique_entities().items():
        if entity_type and entity.entity_type != entity_type:
            continue
        if status and entity.metadata.get("status") != status:
            continue
        if year is not None and entity.metadata.get("year") != year:
            continue

        score = 0
        if normalize_lookup(entity_id) == needle:
            score = max(score, 100)
        title = normalize_lookup(entity.title or "")
        aliases = [normalize_lookup(alias) for alias in entity.aliases]
        if title == needle:
            score = max(score, 90)
        if needle in aliases:
            score = max(score, 80)
        if needle and needle in title:
            score = max(score, 60)
        if needle and any(needle in alias for alias in aliases):
            score = max(score, 50)

        body = normalize_lookup(entity.body)
        if needle and needle in body:
            score = max(score, 30)
        metadata_text = normalize_lookup(" ".join(_flatten_strings(entity.metadata)))
        if needle and needle in metadata_text:
            score = max(score, 20)
        if score:
            results.append((score, entity_id, entity))

    return [
        {"score": score, **entity_payload(entity)}
        for score, _, entity in sorted(results, key=lambda item: (-item[0], item[1]))
    ]


def backlinks_for(index: WikiIndex, entity_id: str) -> Dict[str, Any]:
    return index.backlinks().get(
        entity_id,
        {"structured": [], "navigational": []},
    )


def neighbors_for(index: WikiIndex, entity_id: str) -> Dict[str, Any]:
    structured_outgoing: List[Dict[str, Any]] = []
    structured_incoming: List[Dict[str, Any]] = []
    navigational_outgoing: List[Dict[str, Any]] = []

    for edge in index.edges:
        rule = index.schema.relations.get(edge.relation)
        if edge.source == entity_id:
            structured_outgoing.append(
                {
                    "relation": edge.relation,
                    "target": edge.target,
                    "inverse": rule.inverse if rule else None,
                }
            )
        if edge.target == entity_id:
            structured_incoming.append(
                {
                    "relation": edge.relation,
                    "inverse": rule.inverse if rule else None,
                    "source": edge.source,
                }
            )

    for link in index.links:
        if link.source == entity_id and link.target:
            navigational_outgoing.append(
                {
                    "target": link.target,
                    "path": link.path,
                    "line": link.line,
                    "resolution": link.resolution,
                }
            )

    backlinks = backlinks_for(index, entity_id)
    return {
        "entity": entity_id,
        "structured_outgoing": sorted(
            structured_outgoing,
            key=lambda item: (item["relation"], item["target"]),
        ),
        "structured_incoming": sorted(
            structured_incoming,
            key=lambda item: (item["relation"], item["source"]),
        ),
        "navigational_outgoing": sorted(
            navigational_outgoing,
            key=lambda item: (item["target"], item["path"], item["line"]),
        ),
        "navigational_incoming": backlinks["navigational"],
    }


def related_entities(index: WikiIndex, entity_id: str, depth: int = 2) -> List[Dict[str, Any]]:
    adjacency: Dict[str, List[Tuple[str, str, str]]] = {}
    for edge in index.edges:
        rule = index.schema.relations.get(edge.relation)
        inverse = rule.inverse if rule else edge.relation
        adjacency.setdefault(edge.source, []).append(
            (edge.target, edge.relation, "outgoing")
        )
        adjacency.setdefault(edge.target, []).append(
            (edge.source, inverse, "incoming")
        )

    queue = deque([(entity_id, 0)])
    visited: Set[str] = {entity_id}
    records: List[Dict[str, Any]] = []
    while queue:
        current, distance = queue.popleft()
        if distance >= depth:
            continue
        for neighbor, relation, direction in sorted(adjacency.get(current, [])):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            next_distance = distance + 1
            entity = index.resolver.exact_entity(neighbor)
            if entity:
                records.append(
                    {
                        "distance": next_distance,
                        "via": current,
                        "relation": relation,
                        "direction": direction,
                        **entity_payload(entity),
                    }
                )
                queue.append((neighbor, next_distance))
    return sorted(records, key=lambda item: (item["distance"], str(item["id"])))


def _resolve_filter_id(index: WikiIndex, value: Optional[str], expected_type: str) -> Optional[str]:
    if value is None:
        return None
    resolution, entity, _ = resolve_entity(index, value)
    if entity and entity.entity_type == expected_type:
        return entity.entity_id
    if ":" not in value:
        candidate = f"{expected_type}:{value}"
        entity = index.resolver.exact_entity(candidate)
        if entity:
            return candidate
    return value


def _reference_matches(value: Any, expected: Optional[str]) -> bool:
    if expected is None:
        return True
    return expected in [str(item) for item in listify(value)]


def structured_query(
    index: WikiIndex,
    *,
    entity_type: Optional[str] = None,
    status: Optional[str] = None,
    benchmark: Optional[str] = None,
    method: Optional[str] = None,
    model: Optional[str] = None,
    min_context: Optional[int] = None,
    max_context: Optional[int] = None,
    sparsity_target: Optional[str] = None,
    min_sparsity: Optional[float] = None,
    max_sparsity: Optional[float] = None,
) -> List[Dict[str, Any]]:
    benchmark_id = _resolve_filter_id(index, benchmark, "benchmark")
    method_id = _resolve_filter_id(index, method, "method")
    model_id = _resolve_filter_id(index, model, "model")
    results: List[Dict[str, Any]] = []

    for entity_id, entity in sorted(index.unique_entities().items()):
        metadata = entity.metadata
        if entity_type and entity.entity_type != entity_type:
            continue
        if status and metadata.get("status") != status:
            continue
        if not _reference_matches(metadata.get("benchmark"), benchmark_id):
            continue
        if not _reference_matches(metadata.get("method"), method_id):
            continue
        if not _reference_matches(metadata.get("model"), model_id):
            continue

        context_length = metadata.get("context_length")
        if min_context is not None and (
            not isinstance(context_length, (int, float)) or context_length < min_context
        ):
            continue
        if max_context is not None and (
            not isinstance(context_length, (int, float)) or context_length > max_context
        ):
            continue

        target = dotted_get(metadata, "sparsity.target")
        if sparsity_target and normalize_lookup(str(target or "")) != normalize_lookup(sparsity_target):
            continue
        ratio = dotted_get(metadata, "sparsity.ratio")
        if min_sparsity is not None and (
            not isinstance(ratio, (int, float)) or ratio < min_sparsity
        ):
            continue
        if max_sparsity is not None and (
            not isinstance(ratio, (int, float)) or ratio > max_sparsity
        ):
            continue
        results.append(entity_payload(entity))
    return results
