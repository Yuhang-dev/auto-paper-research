"""Evidence-aware validation for the research Wiki."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from .indexer import WikiIndex
from .models import Diagnostic, Edge, Entity, ResolvedLink, listify
from .schema import (
    dotted_exists,
    dotted_get,
    has_meaningful_value,
    matches_declared_type,
    type_name,
)


ISO_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
LEGACY_STATUSES = {"draft", "needs-review", "verified"}


def _diagnostic(
    severity: str,
    code: str,
    message: str,
    *,
    entity: Optional[Entity] = None,
    path: Optional[str] = None,
    field: Optional[str] = None,
    line: Optional[int] = None,
) -> Diagnostic:
    return Diagnostic(
        severity=severity,
        code=code,
        message=message,
        path=path or (entity.relative_path if entity else None),
        entity_id=entity.entity_id if entity else None,
        field=field,
        line=line,
    )


def _validate_contract(index: WikiIndex) -> List[Diagnostic]:
    diagnostics: List[Diagnostic] = []
    schema = index.schema
    known_types = set(schema.entity_types)

    for relation, rule in schema.relations.items():
        if not rule.inverse:
            diagnostics.append(
                _diagnostic(
                    "ERROR",
                    "relation_missing_inverse",
                    f"Relation {relation} has no inverse name.",
                    path="_meta/relation-types.yaml",
                    field=f"relations.{relation}.inverse",
                )
            )
        for source_type in rule.source_types:
            if source_type not in known_types:
                diagnostics.append(
                    _diagnostic(
                        "ERROR",
                        "relation_unknown_source_type",
                        f"Relation {relation} references unknown source type {source_type}.",
                        path="_meta/relation-types.yaml",
                        field=f"relations.{relation}.source_types",
                    )
                )
        for target_type in rule.target_types:
            if target_type not in known_types:
                diagnostics.append(
                    _diagnostic(
                        "ERROR",
                        "relation_unknown_target_type",
                        f"Relation {relation} references unknown target type {target_type}.",
                        path="_meta/relation-types.yaml",
                        field=f"relations.{relation}.target_types",
                    )
                )

    for entity_type in schema.entity_types:
        for field_name, mapping in schema.relation_fields(entity_type).items():
            if not isinstance(mapping, Mapping):
                diagnostics.append(
                    _diagnostic(
                        "ERROR",
                        "invalid_relation_field_config",
                        f"Relation field {entity_type}.{field_name} must be a mapping.",
                        path="_meta/schema.yaml",
                        field=f"types.{entity_type}.relation_fields.{field_name}",
                    )
                )
                continue
            relation = str(mapping.get("relation") or "")
            if relation not in schema.relations:
                diagnostics.append(
                    _diagnostic(
                        "ERROR",
                        "unknown_relation_field_relation",
                        f"Relation field {entity_type}.{field_name} uses unknown relation {relation}.",
                        path="_meta/schema.yaml",
                        field=f"types.{entity_type}.relation_fields.{field_name}",
                    )
                )
            direction = str(mapping.get("direction") or "")
            if direction not in {"inbound", "outbound"}:
                diagnostics.append(
                    _diagnostic(
                        "ERROR",
                        "invalid_relation_field_direction",
                        f"Relation field {entity_type}.{field_name} has invalid direction {direction}.",
                        path="_meta/schema.yaml",
                        field=f"types.{entity_type}.relation_fields.{field_name}",
                    )
                )
    return diagnostics


def _validate_parse_errors(entity: Entity) -> List[Diagnostic]:
    return [
        _diagnostic(
            "ERROR",
            "markdown_parse_error",
            error,
            entity=entity,
        )
        for error in entity.parse_errors
    ]


def _validate_minimum_identity(entity: Entity, index: WikiIndex) -> List[Diagnostic]:
    diagnostics: List[Diagnostic] = []
    metadata = entity.metadata
    for field in ("id", "type", "title", "status"):
        if field not in metadata or metadata.get(field) in (None, ""):
            diagnostics.append(
                _diagnostic(
                    "ERROR",
                    "missing_identity_field",
                    f"Required identity field {field} is missing.",
                    entity=entity,
                    field=field,
                )
            )

    entity_id = entity.entity_id
    entity_type = entity.entity_type
    if entity_id and not index.schema.id_pattern.fullmatch(entity_id):
        diagnostics.append(
            _diagnostic(
                "ERROR",
                "invalid_entity_id",
                f"Entity ID {entity_id!r} does not match the canonical ID pattern.",
                entity=entity,
                field="id",
            )
        )
    if entity_type and entity_type not in index.schema.entity_types:
        diagnostics.append(
            _diagnostic(
                "ERROR",
                "unknown_entity_type",
                f"Unknown entity type {entity_type!r}.",
                entity=entity,
                field="type",
            )
        )
    if entity_id and entity_type and ":" in entity_id:
        prefix = entity_id.split(":", 1)[0]
        if prefix != entity_type:
            diagnostics.append(
                _diagnostic(
                    "ERROR",
                    "id_type_mismatch",
                    f"ID prefix {prefix!r} does not match type {entity_type!r}.",
                    entity=entity,
                    field="id",
                )
            )

    if entity_type in index.schema.entity_types:
        expected_directory = index.schema.directory_for(entity_type)
        actual_directory = entity.relative_path.split("/", 1)[0]
        if expected_directory and actual_directory != expected_directory:
            diagnostics.append(
                _diagnostic(
                    "ERROR",
                    "type_directory_mismatch",
                    f"Type {entity_type} belongs under {expected_directory}/.",
                    entity=entity,
                    field="type",
                )
            )
    return diagnostics


def _validate_current_schema(entity: Entity, index: WikiIndex) -> List[Diagnostic]:
    diagnostics: List[Diagnostic] = []
    metadata = entity.metadata
    entity_type = entity.entity_type or ""
    schema = index.schema

    for field in schema.required_fields(entity_type):
        if not dotted_exists(metadata, field):
            diagnostics.append(
                _diagnostic(
                    "ERROR",
                    "missing_required_field",
                    f"Required field {field} is missing.",
                    entity=entity,
                    field=field,
                )
            )

    for field, declaration in schema.field_types(entity_type).items():
        if not dotted_exists(metadata, field):
            continue
        value = dotted_get(metadata, field)
        if not matches_declared_type(value, declaration):
            diagnostics.append(
                _diagnostic(
                    "ERROR",
                    "invalid_field_type",
                    f"Field {field} has type {type_name(value)}, expected {declaration!r}.",
                    entity=entity,
                    field=field,
                )
            )

    status = metadata.get("status")
    if status not in schema.lifecycle_statuses:
        diagnostics.append(
            _diagnostic(
                "ERROR",
                "invalid_lifecycle_status",
                f"Unsupported lifecycle status {status!r}.",
                entity=entity,
                field="status",
            )
        )

    for timestamp_field in ("created_at", "updated_at"):
        value = metadata.get(timestamp_field)
        if isinstance(value, str) and not ISO_TIMESTAMP_PATTERN.fullmatch(value):
            diagnostics.append(
                _diagnostic(
                    "ERROR",
                    "invalid_timestamp",
                    f"{timestamp_field} must be an ISO 8601 timestamp with timezone.",
                    entity=entity,
                    field=timestamp_field,
                )
            )

    if entity_type == "claim":
        assessment = metadata.get("assessment")
        if assessment not in schema.claim_assessments:
            diagnostics.append(
                _diagnostic(
                    "ERROR",
                    "invalid_claim_assessment",
                    f"Unsupported claim assessment {assessment!r}.",
                    entity=entity,
                    field="assessment",
                )
            )

    if entity_type == "assessment":
        result = metadata.get("result")
        if result not in schema.nonconsensus_results:
            diagnostics.append(
                _diagnostic(
                    "ERROR",
                    "invalid_nonconsensus_result",
                    f"Unsupported non-consensus result {result!r}.",
                    entity=entity,
                    field="result",
                )
            )
        verified_flag = metadata.get("verified")
        if metadata.get("status") == "verified" and verified_flag is not True:
            diagnostics.append(
                _diagnostic(
                    "ERROR",
                    "verified_assessment_flag_required",
                    "A verified assessment must set verified: true.",
                    entity=entity,
                    field="verified",
                )
            )
        elif metadata.get("status") != "verified" and verified_flag is True:
            diagnostics.append(
                _diagnostic(
                    "WARNING",
                    "assessment_flag_status_mismatch",
                    "verified: true requires lifecycle status verified.",
                    entity=entity,
                    field="verified",
                )
            )

    facets = metadata.get("facets")
    if isinstance(facets, list):
        normalized_facets = []
        for facet in facets:
            if not isinstance(facet, str) or not facet.strip():
                diagnostics.append(
                    _diagnostic(
                        "ERROR",
                        "invalid_facet_name",
                        "Every facets entry must be a non-empty string.",
                        entity=entity,
                        field="facets",
                    )
                )
                continue
            normalized_facets.append(facet.strip().casefold())
        if len(normalized_facets) != len(set(normalized_facets)):
            diagnostics.append(
                _diagnostic(
                    "WARNING",
                    "duplicate_facets",
                    "facets contains duplicate names.",
                    entity=entity,
                    field="facets",
                )
            )

    relations = metadata.get("relations")
    if relations is not None and not isinstance(relations, Mapping):
        diagnostics.append(
            _diagnostic(
                "ERROR",
                "relations_not_mapping",
                "relations must be a mapping of relation names to canonical IDs.",
                entity=entity,
                field="relations",
            )
        )
    elif isinstance(relations, Mapping):
        for relation, targets in relations.items():
            for target in listify(targets):
                if not isinstance(target, str):
                    diagnostics.append(
                        _diagnostic(
                            "ERROR",
                            "relation_target_not_string",
                            f"Relation {relation} target must be a canonical ID string.",
                            entity=entity,
                            field=f"relations.{relation}",
                        )
                    )
    return diagnostics


def _validate_legacy(entity: Entity, index: WikiIndex) -> List[Diagnostic]:
    diagnostics = [
        _diagnostic(
            "WARNING",
            "legacy_schema_version",
            "Page has no schema_version and is being parsed in compatibility mode.",
            entity=entity,
            field="schema_version",
        )
    ]
    status = entity.metadata.get("status")
    if status not in LEGACY_STATUSES:
        diagnostics.append(
            _diagnostic(
                "WARNING",
                "legacy_unknown_status",
                f"Legacy page uses unrecognized status {status!r}.",
                entity=entity,
                field="status",
            )
        )
    if entity.entity_type == "concept" and entity.metadata.get("kind") == "method":
        diagnostics.append(
            _diagnostic(
                "WARNING",
                "legacy_method_as_concept",
                "Method is stored as concept(kind: method); migrate it to type method.",
                entity=entity,
                field="kind",
            )
        )
    return diagnostics


def _validate_entity(entity: Entity, index: WikiIndex) -> List[Diagnostic]:
    diagnostics: List[Diagnostic] = []
    diagnostics.extend(_validate_parse_errors(entity))
    diagnostics.extend(_validate_minimum_identity(entity, index))
    if entity.schema_version is None:
        diagnostics.extend(_validate_legacy(entity, index))
    elif entity.schema_version != index.schema.version:
        diagnostics.append(
            _diagnostic(
                "ERROR",
                "unsupported_schema_version",
                f"Page schema {entity.schema_version!r} is not {index.schema.version!r}.",
                entity=entity,
                field="schema_version",
            )
        )
    else:
        diagnostics.extend(_validate_current_schema(entity, index))

    aliases = entity.metadata.get("aliases")
    if isinstance(aliases, list):
        normalized = [str(item).casefold().strip() for item in aliases]
        duplicates = [
            alias for alias, count in Counter(normalized).items() if alias and count > 1
        ]
        for alias in duplicates:
            diagnostics.append(
                _diagnostic(
                    "WARNING",
                    "duplicate_local_alias",
                    f"Alias {alias!r} is repeated on the page.",
                    entity=entity,
                    field="aliases",
                )
            )
    return diagnostics


def _validate_duplicate_ids(index: WikiIndex) -> List[Diagnostic]:
    diagnostics: List[Diagnostic] = []
    for entity_id, entities in sorted(index.resolver.duplicate_ids().items()):
        paths = ", ".join(entity.relative_path for entity in entities)
        for entity in entities:
            diagnostics.append(
                _diagnostic(
                    "ERROR",
                    "duplicate_id",
                    f"Entity ID {entity_id} is duplicated across: {paths}.",
                    entity=entity,
                    field="id",
                )
            )
    return diagnostics


def _validate_aliases(index: WikiIndex) -> List[Diagnostic]:
    diagnostics: List[Diagnostic] = []
    for lookup, entity_ids in index.resolver.alias_map().items():
        if len(entity_ids) <= 1:
            continue
        diagnostics.append(
            _diagnostic(
                "WARNING",
                "ambiguous_alias",
                f"Lookup term {lookup!r} maps to {', '.join(entity_ids)}.",
                path="_generated/aliases.json",
            )
        )
    return diagnostics


def _validate_edges(index: WikiIndex) -> List[Diagnostic]:
    diagnostics: List[Diagnostic] = []
    entities_by_path = {entity.relative_path: entity for entity in index.entities}
    for edge in index.edges:
        rule = index.schema.relations.get(edge.relation)
        origin_entity = entities_by_path.get(edge.origin_path)
        if rule is None:
            diagnostics.append(
                _diagnostic(
                    "ERROR",
                    "unknown_relation",
                    f"Unknown structured relation {edge.relation!r}.",
                    entity=origin_entity,
                    path=edge.origin_path,
                    field=edge.origin_field,
                )
            )
            continue

        source_entity = index.resolver.exact_entity(edge.source)
        target_entity = index.resolver.exact_entity(edge.target)
        if source_entity is None:
            diagnostics.append(
                _diagnostic(
                    "ERROR",
                    "unresolved_relation_source",
                    f"Relation source {edge.source!r} is not a unique canonical entity ID.",
                    entity=origin_entity,
                    path=edge.origin_path,
                    field=edge.origin_field,
                )
            )
        if target_entity is None:
            diagnostics.append(
                _diagnostic(
                    "ERROR",
                    "unresolved_relation_target",
                    f"Relation target {edge.target!r} is not a unique canonical entity ID.",
                    entity=origin_entity,
                    path=edge.origin_path,
                    field=edge.origin_field,
                )
            )
        if source_entity and source_entity.entity_type not in rule.source_types:
            diagnostics.append(
                _diagnostic(
                    "ERROR",
                    "relation_source_type_mismatch",
                    f"{edge.relation} requires source type {list(rule.source_types)}, "
                    f"got {source_entity.entity_type}.",
                    path=edge.origin_path,
                    entity=origin_entity,
                    field=edge.origin_field,
                )
            )
        if target_entity and target_entity.entity_type not in rule.target_types:
            diagnostics.append(
                _diagnostic(
                    "ERROR",
                    "relation_target_type_mismatch",
                    f"{edge.relation} requires target type {list(rule.target_types)}, "
                    f"got {target_entity.entity_type}.",
                    path=edge.origin_path,
                    entity=origin_entity,
                    field=edge.origin_field,
                )
            )
        if edge.source == edge.target:
            diagnostics.append(
                _diagnostic(
                    "WARNING",
                    "self_relation",
                    f"Entity has a self relation through {edge.relation}.",
                    path=edge.origin_path,
                    entity=origin_entity,
                    field=edge.origin_field,
                )
            )
    return diagnostics


def _validate_link(link: ResolvedLink, index: WikiIndex) -> List[Diagnostic]:
    diagnostics: List[Diagnostic] = []
    source_entity = index.resolver.exact_entity(link.source)
    common = {
        "entity": source_entity,
        "path": link.path,
        "line": link.line,
    }
    if link.resolution == "illegal-path":
        diagnostics.append(
            _diagnostic(
                "ERROR",
                "illegal_wikilink_path",
                f"Wiki link target {link.raw_target!r} contains an illegal path.",
                **common,
            )
        )
    elif link.resolution == "legacy-path":
        diagnostics.append(
            _diagnostic(
                "WARNING",
                "legacy_path_link",
                f"Use canonical ID [[{link.target}]] instead of [[{link.raw_target}]].",
                **common,
            )
        )
    elif link.resolution == "title-or-alias":
        diagnostics.append(
            _diagnostic(
                "WARNING",
                "noncanonical_wikilink",
                f"Use canonical ID [[{link.target}]] instead of [[{link.raw_target}]].",
                **common,
            )
        )
    elif link.resolution.startswith("ambiguous"):
        diagnostics.append(
            _diagnostic(
                "WARNING",
                "ambiguous_wikilink",
                f"Wiki link {link.raw_target!r} is ambiguous: {list(link.candidates)}.",
                **common,
            )
        )
    elif link.resolution == "unresolved":
        diagnostics.append(
            _diagnostic(
                "WARNING",
                "unresolved_wikilink",
                f"Wiki link target {link.raw_target!r} does not resolve.",
                **common,
            )
        )
    if link.target and link.target == link.source:
        diagnostics.append(
            _diagnostic(
                "WARNING",
                "self_wikilink",
                "Page contains a navigational link to itself.",
                **common,
            )
        )
    return diagnostics


def _validate_links(index: WikiIndex) -> List[Diagnostic]:
    return [
        diagnostic for link in index.links for diagnostic in _validate_link(link, index)
    ]


def _validate_verified(entity: Entity, index: WikiIndex) -> List[Diagnostic]:
    if entity.metadata.get("status") != "verified":
        return []
    diagnostics: List[Diagnostic] = []
    entity_type = entity.entity_type or ""
    for field in index.schema.verified_required(entity_type):
        if not has_meaningful_value(entity.metadata, field):
            diagnostics.append(
                _diagnostic(
                    "ERROR",
                    "verified_missing_evidence_field",
                    f"Verified {entity_type} requires {field}.",
                    entity=entity,
                    field=field,
                )
            )
    for group in index.schema.verified_any(entity_type):
        if not any(has_meaningful_value(entity.metadata, field) for field in group):
            diagnostics.append(
                _diagnostic(
                    "ERROR",
                    "verified_missing_any_field",
                    f"Verified {entity_type} requires at least one of {group}.",
                    entity=entity,
                    field="|".join(group),
                )
            )

    minimum_edges = index.schema.verified_minimum_evidence_edges(entity_type)
    direct_author_claim = (
        entity_type == "claim"
        and entity.metadata.get("evidence_type") == "author-stated"
    )
    if minimum_edges and direct_author_claim:
        missing = [
            field
            for field in (
                "attribution",
                "evidence_status",
                "evidence.locator",
                "evidence.pdf_page",
                "source_paper",
            )
            if not has_meaningful_value(entity.metadata, field)
        ]
        source_edges = [
            edge
            for edge in index.edges
            if edge.target == entity.entity_id and edge.relation == "states"
        ]
        if (
            missing
            or entity.metadata.get("attribution") != "author"
            or entity.metadata.get("evidence_status") != "located"
            or not source_edges
        ):
            diagnostics.append(
                _diagnostic(
                    "ERROR",
                    "verified_claim_missing_direct_evidence",
                    "Verified author-stated claim requires located direct evidence and a structured source-paper edge.",
                    entity=entity,
                    field=",".join(missing) or "source_paper",
                )
            )
    elif minimum_edges:
        evidence_edges = [
            edge
            for edge in index.edges
            if edge.target == entity.entity_id
            and edge.relation in {"supports", "contradicts"}
        ]
        if len(evidence_edges) < minimum_edges:
            diagnostics.append(
                _diagnostic(
                    "ERROR",
                    "verified_claim_insufficient_evidence",
                    f"Verified claim requires at least {minimum_edges} experiment evidence edge(s).",
                    entity=entity,
                    field="relations",
                )
            )
    return diagnostics


def _validate_graph_advisories(index: WikiIndex) -> List[Diagnostic]:
    diagnostics: List[Diagnostic] = []
    unique = index.unique_entities()
    connected: Set[str] = set()
    for edge in index.edges:
        connected.add(edge.source)
        connected.add(edge.target)
    for link in index.links:
        connected.add(link.source)
        if link.target:
            connected.add(link.target)

    for entity_id, entity in unique.items():
        if entity_id not in connected:
            diagnostics.append(
                _diagnostic(
                    "WARNING",
                    "orphan_page",
                    "Entity has no structured or navigational relationships.",
                    entity=entity,
                )
            )
        if entity.entity_type == "paper":
            reports = [
                edge
                for edge in index.edges
                if edge.source == entity_id and edge.relation == "reports"
            ]
            if not reports:
                diagnostics.append(
                    _diagnostic(
                        "WARNING",
                        "paper_has_no_experiment",
                        "Paper has no structured experiment entity.",
                        entity=entity,
                    )
                )
        if entity.entity_type == "claim":
            evidence = [
                edge
                for edge in index.edges
                if edge.target == entity_id
                and edge.relation in {"supports", "contradicts"}
            ]
            direct_author_claim = (
                entity.metadata.get("evidence_type") == "author-stated"
                and entity.metadata.get("attribution") == "author"
                and entity.metadata.get("evidence_status") == "located"
                and has_meaningful_value(entity.metadata, "evidence.locator")
                and has_meaningful_value(entity.metadata, "evidence.pdf_page")
                and any(
                    edge.target == entity_id and edge.relation == "states"
                    for edge in index.edges
                )
            )
            if not evidence and not direct_author_claim:
                diagnostics.append(
                    _diagnostic(
                        "WARNING",
                        "claim_lacks_evidence",
                        "Claim has neither experiment evidence nor complete direct author evidence.",
                        entity=entity,
                    )
                )
    return diagnostics


def validate_index(index: WikiIndex) -> List[Diagnostic]:
    diagnostics: List[Diagnostic] = []
    diagnostics.extend(_validate_contract(index))
    diagnostics.extend(_validate_duplicate_ids(index))
    diagnostics.extend(_validate_aliases(index))
    for entity in index.entities:
        diagnostics.extend(_validate_entity(entity, index))
        diagnostics.extend(_validate_verified(entity, index))
    diagnostics.extend(_validate_edges(index))
    diagnostics.extend(_validate_links(index))
    diagnostics.extend(_validate_graph_advisories(index))
    return sorted(diagnostics, key=lambda item: item.sort_key)
