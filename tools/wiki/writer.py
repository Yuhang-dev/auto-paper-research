"""Transactional source-page publishing for the Markdown Wiki."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import yaml  # type: ignore[import-untyped]

from .indexer import build_index, write_artifacts
from .models import Diagnostic
from .validator import validate_index


class WikiWriteError(RuntimeError):
    """Raised when a proposed Wiki mutation cannot be safely published."""


class _NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def render_wiki_page(metadata: Mapping[str, Any], body: str) -> str:
    """Render canonical YAML frontmatter while preserving a Markdown body."""

    frontmatter = yaml.dump(
        dict(metadata),
        Dumper=_NoAliasDumper,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=1000,
    ).strip()
    return f"---\n{frontmatter}\n---\n\n{str(body).strip()}\n"


@dataclass(frozen=True)
class WikiPublishReport:
    changed_paths: Tuple[str, ...]
    diagnostics: Tuple[Diagnostic, ...]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _normalize_pages(pages: Mapping[str, str]) -> Dict[str, str]:
    normalized: Dict[str, str] = {}
    for raw_path, raw_content in pages.items():
        text_path = str(raw_path).strip().replace("\\", "/")
        relative = PurePosixPath(text_path)
        if (
            not text_path
            or relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
            or ":" in relative.parts[0]
            or relative.suffix.casefold() != ".md"
            or any(
                part.startswith(".") or part.startswith("_") for part in relative.parts
            )
        ):
            raise WikiWriteError(f"Unsafe Wiki source path: {raw_path!r}")
        canonical = relative.as_posix()
        if canonical.casefold() in {item.casefold() for item in normalized}:
            raise WikiWriteError(f"Duplicate Wiki source path: {canonical}")
        content = str(raw_content)
        if not content.strip():
            raise WikiWriteError(f"Wiki source page cannot be empty: {canonical}")
        if not content.endswith("\n"):
            content += "\n"
        normalized[canonical] = content
    return dict(sorted(normalized.items()))


def _stage_meta_root(
    wiki_root: Path,
    meta_root: Path,
    staged_wiki_root: Path,
) -> Path:
    try:
        relative = meta_root.relative_to(wiki_root)
    except ValueError:
        return meta_root
    return staged_wiki_root / relative


def _copy_source_wiki(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("_generated", ".*"),
    )


def _error_diagnostics(diagnostics: Sequence[Diagnostic]) -> Tuple[Diagnostic, ...]:
    return tuple(item for item in diagnostics if item.severity == "ERROR")


def _format_errors(diagnostics: Sequence[Diagnostic]) -> str:
    return "; ".join(
        f"{item.code} at {item.path or '<wiki>'}: {item.message}"
        for item in diagnostics[:8]
    )


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
            handle.write(content)
            temporary = Path(handle.name)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class WikiSourceWriter:
    """Validate proposed pages in a shadow Wiki, then publish transactionally."""

    def __init__(self, wiki_root: Path, meta_root: Optional[Path] = None):
        self.wiki_root = wiki_root.resolve()
        self.meta_root = (meta_root or self.wiki_root / "_meta").resolve()
        if not self.wiki_root.is_dir():
            raise FileNotFoundError(f"Wiki root does not exist: {self.wiki_root}")
        if not self.meta_root.is_dir():
            raise FileNotFoundError(
                f"Wiki metadata root does not exist: {self.meta_root}"
            )

    def _target(self, relative_path: str) -> Path:
        target = (self.wiki_root / PurePosixPath(relative_path)).resolve()
        if not _is_within(target, self.wiki_root):
            raise WikiWriteError(f"Wiki target escapes the Wiki root: {relative_path}")
        return target

    def _changed_pages(self, pages: Mapping[str, str]) -> Dict[str, str]:
        changed: Dict[str, str] = {}
        for relative_path, content in _normalize_pages(pages).items():
            target = self._target(relative_path)
            if target.is_file() and target.read_text(encoding="utf-8") == content:
                continue
            changed[relative_path] = content
        return changed

    def validate(self, pages: Mapping[str, str]) -> Tuple[Diagnostic, ...]:
        """Return shadow-Wiki diagnostics without touching source pages."""

        normalized = _normalize_pages(pages)
        with tempfile.TemporaryDirectory(
            prefix=".wiki-stage-",
            dir=str(self.wiki_root.parent),
        ) as temporary:
            staged_root = Path(temporary) / "wiki"
            _copy_source_wiki(self.wiki_root, staged_root)
            for relative_path, content in normalized.items():
                staged_target = staged_root / PurePosixPath(relative_path)
                staged_target.parent.mkdir(parents=True, exist_ok=True)
                staged_target.write_text(content, encoding="utf-8", newline="\n")
            staged_meta = _stage_meta_root(
                self.wiki_root,
                self.meta_root,
                staged_root,
            )
            return tuple(validate_index(build_index(staged_root, staged_meta)))

    def publish(
        self,
        pages: Mapping[str, str],
        *,
        allow_overwrite: bool = False,
    ) -> WikiPublishReport:
        """Publish valid pages; restore exact prior bytes if any step fails."""

        normalized = _normalize_pages(pages)
        changed = self._changed_pages(normalized)
        if not changed:
            diagnostics = tuple(
                validate_index(build_index(self.wiki_root, self.meta_root))
            )
            return WikiPublishReport(changed_paths=(), diagnostics=diagnostics)

        if not allow_overwrite:
            conflicts = [
                relative_path
                for relative_path in changed
                if self._target(relative_path).exists()
            ]
            if conflicts:
                raise WikiWriteError(
                    "Refusing to overwrite existing Wiki source pages: "
                    + ", ".join(conflicts)
                )

        staged_diagnostics = self.validate(normalized)
        staged_errors = _error_diagnostics(staged_diagnostics)
        if staged_errors:
            raise WikiWriteError(
                "Shadow Wiki validation failed: " + _format_errors(staged_errors)
            )

        backups: Dict[Path, Optional[bytes]] = {}
        try:
            for relative_path, content in changed.items():
                target = self._target(relative_path)
                backups[target] = target.read_bytes() if target.exists() else None
                _atomic_write(target, content)

            index = build_index(self.wiki_root, self.meta_root)
            diagnostics = tuple(validate_index(index))
            errors = _error_diagnostics(diagnostics)
            if errors:
                raise WikiWriteError(
                    "Published Wiki validation failed: " + _format_errors(errors)
                )
            write_artifacts(index, diagnostics)
        except BaseException:
            # A page batch is one logical transaction.  KeyboardInterrupt,
            # SystemExit, and GeneratorExit must restore the same source state
            # as an ordinary exception before the original interruption is
            # propagated to the caller.
            for target, previous in reversed(tuple(backups.items())):
                if previous is None:
                    if target.exists():
                        target.unlink()
                else:
                    target.write_bytes(previous)
            try:
                restored = build_index(self.wiki_root, self.meta_root)
                write_artifacts(restored, validate_index(restored))
            except Exception:
                pass
            raise

        return WikiPublishReport(
            changed_paths=tuple(changed),
            diagnostics=diagnostics,
        )
