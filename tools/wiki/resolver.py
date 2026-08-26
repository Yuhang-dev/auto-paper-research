"""Resolve stable IDs, aliases, titles, and legacy path links."""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Sequence, Tuple

from .models import Entity
from .schema import WikiSchema


def normalize_lookup(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_legacy_path(value: str) -> Optional[str]:
    text = value.strip().replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None
    path = PurePosixPath(text)
    if ".." in path.parts:
        return None
    normalized = path.as_posix()
    if normalized.endswith(".md"):
        normalized = normalized[:-3]
    return normalized.strip("/")


class Resolver:
    def __init__(self, entities: Sequence[Entity], schema: WikiSchema):
        self.schema = schema
        self.by_id: Dict[str, List[Entity]] = {}
        self.by_lookup: Dict[str, List[str]] = {}
        self.by_path: Dict[str, List[str]] = {}

        for entity in entities:
            entity_id = entity.entity_id
            if not entity_id:
                continue
            self.by_id.setdefault(entity_id, []).append(entity)

            relative = entity.relative_path
            if relative.endswith(".md"):
                relative = relative[:-3]
            self.by_path.setdefault(relative.casefold(), []).append(entity_id)

            for value in [entity.title, *entity.aliases]:
                if not value:
                    continue
                key = normalize_lookup(str(value))
                ids = self.by_lookup.setdefault(key, [])
                if entity_id not in ids:
                    ids.append(entity_id)

        for mapping in (self.by_lookup, self.by_path):
            for key in mapping:
                mapping[key] = sorted(mapping[key])

    def exact_entity(self, entity_id: str) -> Optional[Entity]:
        candidates = self.by_id.get(entity_id, [])
        return candidates[0] if len(candidates) == 1 else None

    def duplicate_ids(self) -> Dict[str, List[Entity]]:
        return {
            entity_id: entities
            for entity_id, entities in self.by_id.items()
            if len(entities) > 1
        }

    def alias_map(self) -> Dict[str, List[str]]:
        return {key: list(values) for key, values in sorted(self.by_lookup.items())}

    def resolve_reference(self, reference: str) -> Tuple[str, Optional[str], Tuple[str, ...]]:
        reference = reference.strip()
        if not reference:
            return "unresolved", None, ()

        exact = self.by_id.get(reference, [])
        if len(exact) == 1:
            return "canonical-id", reference, (reference,)
        if len(exact) > 1:
            return "ambiguous-id", None, tuple(reference for _ in exact)

        if "/" in reference or "\\" in reference or reference.endswith(".md"):
            legacy_path = normalize_legacy_path(reference)
            if legacy_path is None:
                return "illegal-path", None, ()
            candidates = self.by_path.get(legacy_path.casefold(), [])
            if len(candidates) == 1:
                return "legacy-path", candidates[0], tuple(candidates)
            if len(candidates) > 1:
                return "ambiguous-path", None, tuple(candidates)
            return "unresolved", None, ()

        lookup = self.by_lookup.get(normalize_lookup(reference), [])
        if len(lookup) == 1:
            return "title-or-alias", lookup[0], tuple(lookup)
        if len(lookup) > 1:
            return "ambiguous-alias", None, tuple(lookup)
        return "unresolved", None, ()
