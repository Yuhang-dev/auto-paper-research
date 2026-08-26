"""Data models shared by the read-only Wiki engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple


@dataclass(frozen=True)
class WikiLink:
    raw: str
    target: str
    label: Optional[str]
    line: int


@dataclass
class Entity:
    path: Path
    relative_path: str
    metadata: Dict[str, Any]
    body: str
    links: List[WikiLink]
    parse_errors: List[str] = field(default_factory=list)

    @property
    def entity_id(self) -> Optional[str]:
        value = self.metadata.get("id")
        return str(value) if value not in (None, "") else None

    @property
    def entity_type(self) -> Optional[str]:
        value = self.metadata.get("type")
        return str(value) if value not in (None, "") else None

    @property
    def title(self) -> Optional[str]:
        value = self.metadata.get("title")
        return str(value) if value not in (None, "") else None

    @property
    def aliases(self) -> List[str]:
        value = self.metadata.get("aliases", [])
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if item not in (None, "")]

    @property
    def schema_version(self) -> Optional[str]:
        value = self.metadata.get("schema_version")
        return str(value) if value not in (None, "") else None


@dataclass(frozen=True)
class Edge:
    source: str
    relation: str
    target: str
    origin: str
    origin_path: str
    origin_field: Optional[str] = None

    @property
    def key(self) -> Tuple[str, str, str]:
        return self.source, self.relation, self.target

    def as_dict(self, inverse: Optional[str] = None) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "source": self.source,
            "relation": self.relation,
            "target": self.target,
            "origin": self.origin,
            "origin_path": self.origin_path,
        }
        if self.origin_field:
            result["origin_field"] = self.origin_field
        if inverse:
            result["inverse"] = inverse
        return result


@dataclass(frozen=True)
class ResolvedLink:
    source: str
    target: Optional[str]
    raw_target: str
    label: Optional[str]
    line: int
    path: str
    resolution: str
    candidates: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "raw_target": self.raw_target,
            "label": self.label,
            "line": self.line,
            "path": self.path,
            "resolution": self.resolution,
            "candidates": list(self.candidates),
        }


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    message: str
    path: Optional[str] = None
    entity_id: Optional[str] = None
    field: Optional[str] = None
    line: Optional[int] = None

    @property
    def sort_key(self) -> Tuple[Any, ...]:
        severity_rank = {"ERROR": 0, "WARNING": 1, "INFO": 2}
        return (
            severity_rank.get(self.severity, 9),
            self.path or "",
            self.line or 0,
            self.code,
            self.entity_id or "",
            self.field or "",
        )

    def as_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.path is not None:
            result["path"] = self.path
        if self.entity_id is not None:
            result["entity_id"] = self.entity_id
        if self.field is not None:
            result["field"] = self.field
        if self.line is not None:
            result["line"] = self.line
        return result


def listify(value: Any) -> List[Any]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        return value
    return [value]


def json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if hasattr(value, "isoformat") and callable(value.isoformat):
        return value.isoformat()
    return value
