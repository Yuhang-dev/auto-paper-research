"""Load and query the machine-readable Wiki schema."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import yaml  # type: ignore[import-untyped]


def load_yaml_mapping(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return data


def dotted_get(mapping: Mapping[str, Any], path: str) -> Any:
    current: Any = mapping
    for component in path.split("."):
        if not isinstance(current, Mapping) or component not in current:
            return None
        current = current[component]
    return current


def dotted_exists(mapping: Mapping[str, Any], path: str) -> bool:
    current: Any = mapping
    for component in path.split("."):
        if not isinstance(current, Mapping) or component not in current:
            return False
        current = current[component]
    return True


def has_meaningful_value(mapping: Mapping[str, Any], path: str) -> bool:
    if not dotted_exists(mapping, path):
        return False
    return dotted_get(mapping, path) not in (None, "", [], {})


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, Mapping):
        return "mapping"
    return type(value).__name__


def matches_declared_type(value: Any, declaration: Any) -> bool:
    allowed = declaration if isinstance(declaration, list) else [declaration]
    actual = type_name(value)
    if actual == "integer" and "number" in allowed:
        return True
    return actual in allowed


@dataclass(frozen=True)
class RelationRule:
    name: str
    source_types: Sequence[str]
    target_types: Sequence[str]
    inverse: str


class WikiSchema:
    def __init__(
        self, schema_data: Mapping[str, Any], relation_data: Mapping[str, Any]
    ):
        self.data = dict(schema_data)
        self.relation_data = dict(relation_data)
        self.version = str(self.data.get("schema_version"))
        identity = self.data.get("identity", {})
        self.id_pattern = re.compile(str(identity.get("id_pattern", r".+")))
        self.canonical_link_pattern = re.compile(
            str(identity.get("canonical_link_pattern", r".+"))
        )
        self.lifecycle_statuses = tuple(self.data.get("lifecycle_statuses", []))
        self.claim_assessments = tuple(self.data.get("claim_assessments", []))
        self.nonconsensus_results = tuple(self.data.get("nonconsensus_results", []))
        self.base = dict(self.data.get("base", {}))
        self.types = dict(self.data.get("types", {}))
        self.compatibility = dict(self.data.get("compatibility", {}))
        self.relations: Dict[str, RelationRule] = {}
        for name, rule in (self.relation_data.get("relations", {}) or {}).items():
            if not isinstance(rule, Mapping):
                continue
            self.relations[str(name)] = RelationRule(
                name=str(name),
                source_types=tuple(str(item) for item in rule.get("source_types", [])),
                target_types=tuple(str(item) for item in rule.get("target_types", [])),
                inverse=str(rule.get("inverse") or ""),
            )

    @classmethod
    def load(cls, meta_root: Path) -> "WikiSchema":
        schema_path = meta_root / "schema.yaml"
        relations_path = meta_root / "relation-types.yaml"
        schema_data = load_yaml_mapping(schema_path)
        relation_data = load_yaml_mapping(relations_path)
        relation_version = str(relation_data.get("schema_version"))
        schema_version = str(schema_data.get("schema_version"))
        if relation_version != schema_version:
            raise ValueError(
                f"Schema version {schema_version} and relation version "
                f"{relation_version} do not match"
            )
        return cls(schema_data, relation_data)

    @property
    def entity_types(self) -> Sequence[str]:
        return tuple(self.types)

    def type_config(self, entity_type: str) -> Mapping[str, Any]:
        value = self.types.get(entity_type, {})
        return value if isinstance(value, Mapping) else {}

    def directory_for(self, entity_type: str) -> Optional[str]:
        value = self.type_config(entity_type).get("directory")
        return str(value) if value else None

    def required_fields(self, entity_type: str) -> List[str]:
        result = list(self.base.get("required", []) or [])
        result.extend(self.type_config(entity_type).get("required", []) or [])
        return [str(item) for item in result]

    def field_types(self, entity_type: str) -> Dict[str, Any]:
        result = dict(self.base.get("field_types", {}) or {})
        result.update(self.type_config(entity_type).get("field_types", {}) or {})
        return result

    def verified_required(self, entity_type: str) -> List[str]:
        values = self.type_config(entity_type).get("verified_required", []) or []
        return [str(item) for item in values]

    def verified_any(self, entity_type: str) -> List[List[str]]:
        groups = self.type_config(entity_type).get("verified_any", []) or []
        return [[str(item) for item in group] for group in groups]

    def relation_fields(self, entity_type: str) -> Mapping[str, Any]:
        value = self.type_config(entity_type).get("relation_fields", {})
        return value if isinstance(value, Mapping) else {}

    def verified_minimum_evidence_edges(self, entity_type: str) -> int:
        value = self.type_config(entity_type).get("verified_minimum_evidence_edges", 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
