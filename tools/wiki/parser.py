"""Parse Wiki Markdown without mutating source pages."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

import yaml

from .models import Entity, WikiLink


WIKILINK_PATTERN = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")
FENCE_PATTERN = re.compile(r"^\s*(`{3,}|~{3,})")


def discover_markdown(wiki_root: Path) -> List[Path]:
    files: List[Path] = []
    for path in wiki_root.rglob("*.md"):
        relative = path.relative_to(wiki_root)
        if any(part.startswith("_") or part.startswith(".") for part in relative.parts):
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(wiki_root).as_posix())


def split_frontmatter(text: str) -> Tuple[dict, str, int, List[str]]:
    lines = text.splitlines()
    errors: List[str] = []
    if not lines or lines[0].strip() != "---":
        return {}, text, 0, ["missing-frontmatter"]

    closing_index = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing_index = index
            break
    if closing_index is None:
        return {}, text, 0, ["unterminated-frontmatter"]

    frontmatter_text = "\n".join(lines[1:closing_index])
    try:
        metadata = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        return {}, "\n".join(lines[closing_index + 1 :]), closing_index + 1, [
            f"invalid-frontmatter: {exc}"
        ]
    if not isinstance(metadata, dict):
        errors.append("frontmatter-is-not-a-mapping")
        metadata = {}

    body = "\n".join(lines[closing_index + 1 :])
    return metadata, body, closing_index + 1, errors


def parse_wikilinks(body: str, body_start_line: int) -> List[WikiLink]:
    links: List[WikiLink] = []
    fence_character = None
    fence_length = 0

    for offset, line in enumerate(body.splitlines(), start=1):
        fence_match = FENCE_PATTERN.match(line)
        if fence_match:
            marker = fence_match.group(1)
            character = marker[0]
            if fence_character is None:
                fence_character = character
                fence_length = len(marker)
            elif character == fence_character and len(marker) >= fence_length:
                fence_character = None
                fence_length = 0
            continue
        if fence_character is not None:
            continue

        for match in WIKILINK_PATTERN.finditer(line):
            raw_target = match.group(1).strip()
            target = raw_target.split("#", 1)[0].strip()
            label = match.group(2).strip() if match.group(2) else None
            links.append(
                WikiLink(
                    raw=match.group(0),
                    target=target,
                    label=label,
                    line=body_start_line + offset,
                )
            )
    return links


def parse_page(path: Path, wiki_root: Path) -> Entity:
    text = path.read_text(encoding="utf-8")
    metadata, body, frontmatter_end_line, errors = split_frontmatter(text)
    links = parse_wikilinks(body, frontmatter_end_line)
    return Entity(
        path=path,
        relative_path=path.relative_to(wiki_root).as_posix(),
        metadata=metadata,
        body=body,
        links=links,
        parse_errors=errors,
    )


def parse_wiki(wiki_root: Path) -> List[Entity]:
    return [parse_page(path, wiki_root) for path in discover_markdown(wiki_root)]
