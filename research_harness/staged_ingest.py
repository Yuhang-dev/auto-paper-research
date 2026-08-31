"""Durable first-pass paper drafts for deferred Wiki publication.

The staging queue is operational state under ``.harness``.  It deliberately
separates expensive semantic extraction from deterministic entity resolution
and Wiki publication so a research round can read several papers before it
mutates the Markdown source of truth.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ingest_models import IngestCandidate, PaperIngestDraft


RESEARCH_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
STAGE_ID_PATTERN = re.compile(r"^paper-stage-[a-f0-9]{20}$")


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(
        dict(payload),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
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


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StagedPublication(_StrictModel):
    target_id: str
    paper_id: str
    changed_paths: Tuple[str, ...] = ()
    published_at: str

    @model_validator(mode="after")
    def _validate_text(self) -> "StagedPublication":
        if not self.target_id.strip():
            raise ValueError("publication target_id cannot be blank")
        if not self.paper_id.startswith("paper:"):
            raise ValueError("publication paper_id must use the paper: prefix")
        return self


class StagedPaperRecord(_StrictModel):
    schema_version: Literal["0.1"] = "0.1"
    stage_id: str
    research_id: str
    candidate: IngestCandidate
    draft: PaperIngestDraft
    pdf_sha256: str
    pdf_pages: int = Field(ge=1)
    selected_pages: Tuple[int, ...]
    model_calls: int = Field(default=1, ge=1)
    schema_repair_applied: bool = False
    semantic_artifact_ids: Tuple[str, ...] = ()
    publication_artifact_ids: Tuple[str, ...] = ()
    created_at: str
    publications: Tuple[StagedPublication, ...] = ()

    @model_validator(mode="after")
    def _validate_identity(self) -> "StagedPaperRecord":
        if not STAGE_ID_PATTERN.fullmatch(self.stage_id):
            raise ValueError("invalid staged-paper ID")
        if not RESEARCH_ID_PATTERN.fullmatch(self.research_id):
            raise ValueError("invalid research_id in staged-paper record")
        if self.candidate.candidate_id != self.draft.candidate_id:
            raise ValueError("staged candidate and draft IDs must match")
        if not re.fullmatch(r"[a-f0-9]{64}", self.pdf_sha256):
            raise ValueError("pdf_sha256 must be a lowercase SHA-256 digest")
        targets = [item.target_id for item in self.publications]
        if len(set(targets)) != len(targets):
            raise ValueError("a staged paper cannot repeat a publication target")
        return self

    def published_to(self, target_id: str) -> bool:
        return any(item.target_id == target_id for item in self.publications)


class StagedPaperStore:
    """Content-addressed, atomically updated staging queue."""

    def __init__(self, repository_root: Path, root: Optional[Path] = None):
        self.repository_root = repository_root.resolve()
        self.root = (root or self.repository_root / ".harness" / "staged-ingest").resolve()
        if not _is_within(self.root, self.repository_root):
            raise ValueError("Staged-paper root must stay inside the repository")

    def _research_root(self, research_id: str) -> Path:
        if not RESEARCH_ID_PATTERN.fullmatch(research_id):
            raise ValueError("research_id must use safe ASCII filename characters")
        return self.root / research_id

    @staticmethod
    def _identity(
        *,
        research_id: str,
        candidate: IngestCandidate,
        draft: PaperIngestDraft,
        pdf_sha256: str,
    ) -> Mapping[str, object]:
        return {
            "research_id": research_id,
            "candidate_id": candidate.candidate_id,
            "candidate": candidate.model_dump(mode="json"),
            "draft": draft.model_dump(mode="json"),
            "pdf_sha256": pdf_sha256,
        }

    def stage(
        self,
        *,
        research_id: str,
        candidate: IngestCandidate,
        draft: PaperIngestDraft,
        pdf_sha256: str,
        pdf_pages: int,
        selected_pages: Tuple[int, ...],
        model_calls: int,
        schema_repair_applied: bool,
        semantic_artifact_ids: Tuple[str, ...] = (),
        publication_artifact_ids: Tuple[str, ...] = (),
    ) -> tuple[StagedPaperRecord, str]:
        identity = self._identity(
            research_id=research_id,
            candidate=candidate,
            draft=draft,
            pdf_sha256=pdf_sha256,
        )
        digest = hashlib.sha256(_canonical_json(identity)).hexdigest()
        stage_id = f"paper-stage-{digest[:20]}"
        path = self._research_root(research_id) / f"{stage_id}.json"
        if path.is_file():
            record = self.load(path)
            expected = self._identity(
                research_id=record.research_id,
                candidate=record.candidate,
                draft=record.draft,
                pdf_sha256=record.pdf_sha256,
            )
            if _canonical_json(expected) != _canonical_json(identity):
                raise ValueError("Existing staged-paper content does not match its ID")
            return record, path.relative_to(self.repository_root).as_posix()
        record = StagedPaperRecord(
            stage_id=stage_id,
            research_id=research_id,
            candidate=candidate,
            draft=draft,
            pdf_sha256=pdf_sha256,
            pdf_pages=pdf_pages,
            selected_pages=selected_pages,
            model_calls=model_calls,
            schema_repair_applied=schema_repair_applied,
            semantic_artifact_ids=semantic_artifact_ids,
            publication_artifact_ids=publication_artifact_ids,
            created_at=_utc_now(),
        )
        _atomic_json(path, record.model_dump(mode="json"))
        return record, path.relative_to(self.repository_root).as_posix()

    def load(self, path: Path) -> StagedPaperRecord:
        resolved = path.resolve()
        if not _is_within(resolved, self.root) or not resolved.is_file():
            raise ValueError("Staged-paper path is unavailable or outside its store")
        payload = json.loads(resolved.read_text(encoding="utf-8-sig"))
        return StagedPaperRecord.model_validate(payload)

    def load_id(self, research_id: str, stage_id: str) -> StagedPaperRecord:
        if not STAGE_ID_PATTERN.fullmatch(stage_id):
            raise ValueError("invalid staged-paper ID")
        return self.load(self._research_root(research_id) / f"{stage_id}.json")

    def records(self, research_id: str) -> Tuple[StagedPaperRecord, ...]:
        root = self._research_root(research_id)
        if not root.is_dir():
            return ()
        return tuple(self.load(path) for path in sorted(root.glob("paper-stage-*.json")))

    def pending(
        self,
        research_id: str,
        *,
        target_id: str,
    ) -> Tuple[StagedPaperRecord, ...]:
        return tuple(
            record
            for record in self.records(research_id)
            if not record.published_to(target_id)
        )

    def record_publication(
        self,
        record: StagedPaperRecord,
        *,
        target_id: str,
        paper_id: str,
        changed_paths: Tuple[str, ...],
    ) -> StagedPaperRecord:
        if record.published_to(target_id):
            return record
        updated = record.model_copy(
            update={
                "publications": (
                    *record.publications,
                    StagedPublication(
                        target_id=target_id,
                        paper_id=paper_id,
                        changed_paths=changed_paths,
                        published_at=_utc_now(),
                    ),
                )
            }
        )
        path = self._research_root(record.research_id) / f"{record.stage_id}.json"
        _atomic_json(path, updated.model_dump(mode="json"))
        return updated


__all__ = [
    "StagedPaperRecord",
    "StagedPaperStore",
    "StagedPublication",
]
