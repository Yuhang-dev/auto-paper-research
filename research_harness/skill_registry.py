"""Deterministic discovery and safe loading for repository-local Skills."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Dict, Mapping, Tuple, Union

import yaml


SKILL_FILENAME = "SKILL.md"
RESOURCE_GROUPS = ("references", "assets", "scripts", "agents")
IGNORED_RESOURCE_PARTS = {"__pycache__", ".pytest_cache"}
IGNORED_RESOURCE_SUFFIXES = {".pyc", ".pyo"}
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SKILL_DOCUMENT_BYTES = 512_000
DEFAULT_RESOURCE_CHAR_LIMIT = 100_000


class SkillRegistryError(ValueError):
    """Raised when a repository-local Skill violates the registry contract."""


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _read_skill_document(path: Path) -> str:
    size = path.stat().st_size
    if size > MAX_SKILL_DOCUMENT_BYTES:
        raise SkillRegistryError(
            f"Skill document exceeds {MAX_SKILL_DOCUMENT_BYTES} bytes: {path}"
        )
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SkillRegistryError(f"Skill document is not valid UTF-8: {path}") from exc


def _parse_skill_document(path: Path) -> Tuple[Mapping[str, Any], str]:
    document = _read_skill_document(path)
    lines = document.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillRegistryError(f"Skill document must start with YAML frontmatter: {path}")

    closing_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            closing_index = index
            break
    if closing_index is None:
        raise SkillRegistryError(f"Skill frontmatter is not closed: {path}")

    try:
        metadata = yaml.safe_load("\n".join(lines[1:closing_index]))
    except yaml.YAMLError as exc:
        raise SkillRegistryError(f"Invalid YAML frontmatter in {path}: {exc}") from exc
    if not isinstance(metadata, dict):
        raise SkillRegistryError(f"Skill frontmatter must be a YAML mapping: {path}")

    instructions = "\n".join(lines[closing_index + 1 :]).strip()
    if not instructions:
        raise SkillRegistryError(f"Skill instructions cannot be empty: {path}")
    return MappingProxyType(dict(metadata)), instructions


@dataclass(frozen=True)
class SkillResource:
    """One discovered file under a recognized Skill resource directory."""

    group: str
    relative_path: str
    path: Path
    size_bytes: int


@dataclass(frozen=True)
class SkillSpec:
    """Parsed Skill instructions plus lazily readable supporting resources."""

    name: str
    description: str
    instructions: str
    root: Path
    skill_file: Path
    metadata: Mapping[str, Any]
    resources: Tuple[SkillResource, ...]

    def resources_in(self, group: str) -> Tuple[SkillResource, ...]:
        normalized = group.strip().casefold()
        if normalized not in RESOURCE_GROUPS:
            raise ValueError(
                f"Unknown resource group {group!r}; expected one of {RESOURCE_GROUPS}"
            )
        return tuple(item for item in self.resources if item.group == normalized)

    @property
    def references(self) -> Tuple[Path, ...]:
        return tuple(item.path for item in self.resources_in("references"))

    @property
    def assets(self) -> Tuple[Path, ...]:
        return tuple(item.path for item in self.resources_in("assets"))

    @property
    def scripts(self) -> Tuple[Path, ...]:
        return tuple(item.path for item in self.resources_in("scripts"))

    def read_resource(
        self,
        relative_path: Union[str, Path],
        *,
        max_chars: int = DEFAULT_RESOURCE_CHAR_LIMIT,
    ) -> str:
        """Read one indexed UTF-8 resource without allowing path traversal."""

        if not 1 <= max_chars <= 1_000_000:
            raise ValueError("max_chars must be between 1 and 1000000")
        raw = str(relative_path).strip().replace("\\", "/")
        normalized = PurePosixPath(raw)
        if (
            not raw
            or not normalized.parts
            or normalized.is_absolute()
            or ".." in normalized.parts
            or ":" in normalized.parts[0]
        ):
            raise SkillRegistryError(f"Unsafe Skill resource path: {relative_path!r}")
        canonical = normalized.as_posix().casefold()
        resource = next(
            (
                item
                for item in self.resources
                if item.relative_path.casefold() == canonical
            ),
            None,
        )
        if resource is None:
            raise FileNotFoundError(
                f"Resource {normalized.as_posix()!r} is not registered for Skill {self.name!r}"
            )

        resolved = resource.path.resolve()
        if not _is_within(resolved, self.root):
            raise SkillRegistryError(
                f"Registered resource escapes Skill root: {resource.relative_path}"
            )
        try:
            with resolved.open("r", encoding="utf-8-sig") as handle:
                content = handle.read(max_chars + 1)
        except UnicodeDecodeError as exc:
            raise SkillRegistryError(
                f"Skill resource is not valid UTF-8: {resource.relative_path}"
            ) from exc
        if len(content) > max_chars:
            raise SkillRegistryError(
                f"Skill resource exceeds the {max_chars}-character read limit: "
                f"{resource.relative_path}"
            )
        return content

    def read_reference(
        self,
        name: Union[str, Path],
        *,
        max_chars: int = DEFAULT_RESOURCE_CHAR_LIMIT,
    ) -> str:
        """Convenience wrapper for an on-demand file under ``references/``."""

        raw = str(name).strip().replace("\\", "/")
        path = PurePosixPath(raw)
        if not path.parts or path.parts[0].casefold() != "references":
            raw = f"references/{raw}"
        return self.read_resource(raw, max_chars=max_chars)


class SkillRegistry:
    """Scan immediate ``skills/*/SKILL.md`` packages into stable Skill specs."""

    def __init__(self, skills_root: Union[str, Path]):
        self.skills_root = Path(skills_root).resolve()
        if not self.skills_root.is_dir():
            raise FileNotFoundError(f"Skills root does not exist: {self.skills_root}")
        self._skills = self._load()

    def _load(self) -> Dict[str, SkillSpec]:
        loaded: Dict[str, SkillSpec] = {}
        children = sorted(
            (item for item in self.skills_root.iterdir() if item.is_dir()),
            key=lambda item: item.name.casefold(),
        )
        for directory in children:
            skill_file = directory / SKILL_FILENAME
            if not skill_file.is_file():
                continue
            spec = self._load_one(directory, skill_file)
            key = spec.name.casefold()
            if key in loaded:
                raise SkillRegistryError(f"Duplicate Skill name: {spec.name}")
            loaded[key] = spec
        return loaded

    def _load_one(self, directory: Path, skill_file: Path) -> SkillSpec:
        root = directory.resolve()
        resolved_skill_file = skill_file.resolve()
        if not _is_within(resolved_skill_file, self.skills_root):
            raise SkillRegistryError(f"Skill package escapes Skills root: {directory}")
        metadata, instructions = _parse_skill_document(resolved_skill_file)

        name_value = metadata.get("name")
        description_value = metadata.get("description")
        name = name_value.strip() if isinstance(name_value, str) else ""
        description = (
            description_value.strip() if isinstance(description_value, str) else ""
        )
        if not SKILL_NAME_PATTERN.fullmatch(name):
            raise SkillRegistryError(
                f"Skill name must use lowercase kebab-case in {resolved_skill_file}: {name!r}"
            )
        if directory.name != name:
            raise SkillRegistryError(
                f"Skill directory {directory.name!r} must match frontmatter name {name!r}"
            )
        if not description:
            raise SkillRegistryError(
                f"Skill description cannot be empty: {resolved_skill_file}"
            )

        resources = []
        for group in RESOURCE_GROUPS:
            group_root = root / group
            if not group_root.is_dir():
                continue
            for candidate in sorted(
                (
                    item
                    for item in group_root.rglob("*")
                    if item.is_file()
                    and not any(
                        part in IGNORED_RESOURCE_PARTS
                        for part in item.relative_to(group_root).parts
                    )
                    and item.suffix.casefold() not in IGNORED_RESOURCE_SUFFIXES
                ),
                key=lambda item: item.as_posix().casefold(),
            ):
                resolved = candidate.resolve()
                if not _is_within(resolved, root):
                    raise SkillRegistryError(
                        f"Skill resource escapes package root: {candidate}"
                    )
                resources.append(
                    SkillResource(
                        group=group,
                        relative_path=resolved.relative_to(root).as_posix(),
                        path=resolved,
                        size_bytes=resolved.stat().st_size,
                    )
                )

        return SkillSpec(
            name=name,
            description=description,
            instructions=instructions,
            root=root,
            skill_file=resolved_skill_file,
            metadata=metadata,
            resources=tuple(resources),
        )

    def get(self, name: str) -> SkillSpec:
        key = name.strip().casefold()
        try:
            return self._skills[key]
        except KeyError as exc:
            available = ", ".join(self.names) or "<none>"
            raise KeyError(f"Unknown Skill {name!r}; available: {available}") from exc

    def list(self) -> Tuple[SkillSpec, ...]:
        return tuple(self._skills[key] for key in sorted(self._skills))

    @property
    def names(self) -> Tuple[str, ...]:
        return tuple(item.name for item in self.list())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name.strip().casefold() in self._skills

    def __len__(self) -> int:
        return len(self._skills)
