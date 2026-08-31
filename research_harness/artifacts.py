"""Immutable semantic artifacts and a mutable publication manifest.

Semantic artifacts are evaluation records, not operational state. Validated
outputs are written before deterministic compilation; rejected structured
outputs may also be retained with ``schema_valid: false`` for diagnosis and
LoopEngineer feedback. Publication paths are linked later through a separate
manifest so the content-addressed artifact itself never changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from pydantic import BaseModel

from .skill_registry import SkillSpec


ARTIFACT_KIND_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = (
        json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
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


def _redact(value: Any) -> Any:
    secrets = tuple(
        secret
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
        )
        if (secret := os.getenv(name, ""))
    )
    if isinstance(value, BaseModel):
        return _redact(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if str(key).casefold()
                in {"token", "api_key", "apikey", "authorization"}
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_redact(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, str):
        result = value
        for secret in secrets:
            result = result.replace(secret, "[REDACTED]")
        return result
    return value


@dataclass(frozen=True)
class SemanticArtifactContext:
    """Stable input identity attached to one validated semantic proposal."""

    research_id: str
    action_id: str
    snapshot_id: str
    wiki_source_hash: str
    search_run_sha256: Optional[str] = None
    pdf_sha256: Optional[str] = None
    source_ids: tuple[str, ...] = ()

    def with_updates(self, **updates: Any) -> "SemanticArtifactContext":
        return replace(self, **updates)

    def as_dict(self) -> dict[str, Any]:
        return {
            "research_id": self.research_id,
            "snapshot_id": self.snapshot_id,
            "wiki_source_hash": self.wiki_source_hash,
            "search_run_sha256": self.search_run_sha256,
            "pdf_sha256": self.pdf_sha256,
            "source_ids": list(self.source_ids),
        }


@dataclass(frozen=True)
class SemanticArtifactRef:
    artifact_id: str
    relative_path: str


class SemanticArtifactRecorder:
    """Write content-addressed semantic outputs and publication associations."""

    def __init__(
        self,
        repository_root: Path,
        artifact_root: Path,
        *,
        model_name: Optional[str] = None,
        model_base_url: Optional[str] = None,
    ):
        self.repository_root = repository_root.resolve()
        self.artifact_root = artifact_root.resolve()
        try:
            self.artifact_root.relative_to(self.repository_root)
        except ValueError as exc:
            raise ValueError(
                "Semantic artifact root must stay inside the repository"
            ) from exc
        self.model_name = model_name
        self.model_base_url = model_base_url.rstrip("/") if model_base_url else None

    @property
    def manifest_path(self) -> Path:
        return self.artifact_root / "semantic-manifest.json"

    @staticmethod
    def _skill_hash(skill: SkillSpec) -> str:
        return _sha256_bytes(skill.skill_file.read_bytes())

    @staticmethod
    def _schema_hash(skill: SkillSpec, resources: Sequence[str]) -> str:
        material = []
        for name in resources:
            content = skill.read_resource(name)
            material.append({"resource": name, "content": content})
        return _sha256_bytes(_canonical_json(material))

    def record(
        self,
        *,
        kind: str,
        context: SemanticArtifactContext,
        skill: SkillSpec,
        schema_resources: Sequence[str],
        output: Any,
        diagnostic_codes: Iterable[str] = (),
        schema_valid: bool = True,
        validation_details: Optional[Mapping[str, Any]] = None,
    ) -> SemanticArtifactRef:
        if not ARTIFACT_KIND_PATTERN.fullmatch(kind):
            raise ValueError("Semantic artifact kind must use lowercase kebab-case")
        validation: dict[str, Any] = {
            "schema_valid": schema_valid,
            "diagnostic_codes": sorted(set(str(code) for code in diagnostic_codes)),
        }
        if validation_details is not None:
            validation["details"] = _redact(validation_details)
        identity = {
            "schema_version": "0.2",
            "action_id": context.action_id,
            "kind": kind,
            "model": {
                "name": self.model_name,
                "base_url": self.model_base_url,
            },
            "skill": {
                "name": skill.name,
                "skill_sha256": self._skill_hash(skill),
                "schema_sha256": self._schema_hash(skill, schema_resources),
                "schema_resources": list(schema_resources),
            },
            "inputs": context.as_dict(),
            "output": _redact(output),
            "validation": validation,
        }
        digest = _sha256_bytes(_canonical_json(identity))
        artifact_id = f"semantic-{kind}-{digest[:20]}"
        relative = Path("semantic") / kind / f"{artifact_id}.json"
        path = self.artifact_root / relative
        if not path.exists():
            payload = {
                **identity,
                "artifact_id": artifact_id,
                "created_at": _utc_now(),
            }
            _atomic_json_write(path, payload)
        else:
            existing = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(existing, dict):
                raise ValueError(f"Semantic artifact is not a JSON object: {path}")
            existing_identity = {
                key: existing.get(key)
                for key in identity
            }
            if _canonical_json(existing_identity) != _canonical_json(identity):
                raise ValueError(
                    f"Existing semantic artifact content does not match its ID: {path}"
                )
        manifest = self._load_manifest()
        artifacts = manifest.setdefault("artifacts", {})
        artifacts.setdefault(
            artifact_id,
            {
                "kind": kind,
                "path": relative.as_posix(),
                "publications": [],
            },
        )
        self._write_manifest(manifest)
        return SemanticArtifactRef(
            artifact_id=artifact_id,
            relative_path=path.relative_to(self.repository_root).as_posix(),
        )

    def link_publication(
        self,
        artifact_ids: Sequence[str],
        *,
        action_id: str,
        changed_sources: Sequence[str],
    ) -> None:
        if not artifact_ids:
            return
        manifest = self._load_manifest()
        artifacts = manifest.setdefault("artifacts", {})
        publication = {
            "action_id": action_id,
            "changed_sources": list(dict.fromkeys(changed_sources)),
            "recorded_at": _utc_now(),
        }
        for artifact_id in artifact_ids:
            record = artifacts.get(artifact_id)
            if not isinstance(record, dict):
                raise KeyError(f"Unknown semantic artifact: {artifact_id}")
            publications = record.setdefault("publications", [])
            comparable = {
                "action_id": publication["action_id"],
                "changed_sources": publication["changed_sources"],
            }
            if not any(
                isinstance(item, Mapping)
                and {
                    "action_id": item.get("action_id"),
                    "changed_sources": item.get("changed_sources"),
                }
                == comparable
                for item in publications
            ):
                publications.append(publication)
        self._write_manifest(manifest)

    def _load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.is_file():
            return {"schema_version": "0.1", "artifacts": {}}
        payload = json.loads(self.manifest_path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("Semantic artifact manifest must be a JSON object")
        return payload

    def _write_manifest(self, payload: Mapping[str, Any]) -> None:
        _atomic_json_write(self.manifest_path, payload)


__all__ = [
    "SemanticArtifactContext",
    "SemanticArtifactRecorder",
    "SemanticArtifactRef",
]
