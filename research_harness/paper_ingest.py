"""Skill-driven paper extraction with deterministic Wiki publication."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from tools.wiki.indexer import WikiIndex, build_index
from tools.wiki.models import Diagnostic, Entity
from tools.wiki.resolver import normalize_lookup
from tools.wiki.writer import WikiSourceWriter, render_wiki_page

from .artifacts import SemanticArtifactContext, SemanticArtifactRecorder
from .config import HarnessSettings
from .ingest_models import (
    BenchmarkDraft,
    ClaimDraft,
    EvidenceLocator,
    ExperimentDraft,
    IngestCandidate,
    MethodDraft,
    ModelDraft,
    PaperDocument,
    PaperExcerpt,
    PaperIngestDraft,
    PaperIngestResult,
    PdfPageText,
)
from .skill_registry import SkillRegistry, SkillSpec


MAX_PDF_BYTES = 200 * 1024 * 1024
DEFAULT_EXCERPT_CHARS = 70_000
DEFAULT_EXCERPT_PAGES = 16
PDF_KEYWORDS = (
    "experiment",
    "evaluation",
    "results",
    "table",
    "figure",
    "benchmark",
    "context length",
    "long context",
    "latency",
    "throughput",
    "memory",
    "kv cache",
    "limitation",
    "ablation",
    "perplexity",
    "accuracy",
    "sparsity",
)
PLACEHOLDER_PATTERN = re.compile(r"\{\{[^{}]+\}\}")


class PaperIngestError(RuntimeError):
    """Raised when ingestion cannot preserve the evidence or Wiki contract."""


class PaperDraftExtractor(Protocol):
    requires_network: bool

    def extract(
        self,
        *,
        candidate: IngestCandidate,
        document: PaperDocument,
        excerpt: PaperExcerpt,
        skill: SkillSpec,
        wiki_catalog: str,
    ) -> PaperIngestDraft: ...


@dataclass(frozen=True)
class CompiledWikiDraft:
    paper_id: str
    pages: Mapping[str, str]
    created_entity_ids: Tuple[str, ...]
    reused_entity_ids: Tuple[str, ...]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _find_pdftotext() -> Optional[Path]:
    configured = os.getenv("PDFTOTEXT_PATH")
    candidates = [Path(configured)] if configured else []
    discovered = shutil.which("pdftotext")
    if discovered:
        candidates.append(Path(discovered))
    texlive_root = Path("D:/texlive")
    if texlive_root.is_dir():
        candidates.extend(
            sorted(
                texlive_root.glob("*/bin/windows/pdftotext.exe"),
                reverse=True,
            )
        )
    return next((path.resolve() for path in candidates if path.is_file()), None)


def _extract_with_pypdf(path: Path) -> Sequence[str]:
    from pypdf import PdfReader  # type: ignore[import-not-found]

    reader = PdfReader(str(path))
    return [page.extract_text() or "" for page in reader.pages]


def _extract_with_pdftotext(
    path: Path, executable: Path, timeout_seconds: int
) -> Sequence[str]:
    try:
        completed = subprocess.run(
            [str(executable), "-layout", "-enc", "UTF-8", str(path), "-"],
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PaperIngestError(
            f"pdftotext timed out after {timeout_seconds} seconds"
        ) from exc
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise PaperIngestError(
            f"pdftotext failed with exit code {completed.returncode}: {message}"
        )
    text = completed.stdout.decode("utf-8", errors="replace")
    pages = text.split("\f")
    while pages and not pages[-1].strip():
        pages.pop()
    return pages


def extract_pdf_document(
    source_path: Path,
    repository_root: Path,
    *,
    timeout_seconds: int = 90,
) -> PaperDocument:
    """Extract page-aware PDF text while confining source access to the repository."""

    root = repository_root.resolve()
    path = source_path.resolve()
    if not _is_within(path, root):
        raise PaperIngestError("PDF source must stay inside the repository root")
    if path.suffix.casefold() != ".pdf" or not path.is_file():
        raise PaperIngestError(f"PDF source does not exist or is not a PDF: {path}")
    size = path.stat().st_size
    if size <= 0 or size > MAX_PDF_BYTES:
        raise PaperIngestError(
            f"PDF source size must be between 1 and {MAX_PDF_BYTES} bytes"
        )
    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be positive")

    if importlib.util.find_spec("pypdf") is not None:
        raw_pages = _extract_with_pypdf(path)
    else:
        executable = _find_pdftotext()
        if executable is None:
            raise PaperIngestError(
                "No PDF text backend is available. Install pypdf or configure PDFTOTEXT_PATH."
            )
        raw_pages = _extract_with_pdftotext(path, executable, timeout_seconds)

    pages = tuple(
        PdfPageText(pdf_page=index, text=str(text).replace("\x00", "").strip())
        for index, text in enumerate(raw_pages, start=1)
    )
    if not pages or not any(page.text for page in pages):
        raise PaperIngestError("PDF extraction returned no readable text")
    return PaperDocument(
        source_path=path.relative_to(root).as_posix(),
        sha256=_sha256(path),
        pages=pages,
    )


def select_paper_excerpt(
    document: PaperDocument,
    *,
    max_pages: int = DEFAULT_EXCERPT_PAGES,
    max_chars: int = DEFAULT_EXCERPT_CHARS,
) -> PaperExcerpt:
    """Select deterministic overview/evidence pages and retain PDF page markers."""

    if max_pages < 4:
        raise ValueError("max_pages must be at least 4")
    if max_chars < 4_000:
        raise ValueError("max_chars must be at least 4000")
    total_pages = len(document.pages)
    mandatory = set(range(1, min(3, total_pages) + 1))
    mandatory.update(range(max(1, total_pages - 1), total_pages + 1))
    scored = []
    for page in document.pages:
        folded = page.text.casefold()
        score = sum(folded.count(keyword) for keyword in PDF_KEYWORDS)
        scored.append((score, page.pdf_page))
    selected = set(mandatory)
    for _, page_number in sorted(scored, key=lambda item: (-item[0], item[1])):
        if len(selected) >= min(max_pages, total_pages):
            break
        selected.add(page_number)

    parts: list[str] = []
    retained_pages = []
    truncated = len(selected) < total_pages
    for page in document.pages:
        if page.pdf_page not in selected:
            continue
        header = f"--- PDF p. {page.pdf_page} ---\n"
        current_length = len("\n".join(parts))
        separator_length = 1 if parts else 0
        available = max_chars - current_length - separator_length
        if available <= len(header) + 32:
            truncated = True
            break
        page_text = page.text
        body_limit = available - len(header)
        marker = "\n[page text truncated]"
        if len(page_text) > body_limit:
            page_text = page_text[: max(0, body_limit - len(marker))].rstrip() + marker
            truncated = True
        parts.append(header + page_text)
        retained_pages.append(page.pdf_page)
        if len("\n".join(parts)) >= max_chars:
            truncated = True
            break
    return PaperExcerpt(
        text="\n".join(parts).strip(),
        selected_pages=tuple(retained_pages),
        truncated=truncated,
    )


def render_wiki_catalog(index: WikiIndex, *, max_chars: int = 24_000) -> str:
    records = []
    for entity_id, entity in sorted(index.unique_entities().items()):
        record: Dict[str, Any] = {
            "id": entity_id,
            "type": entity.entity_type,
            "title": entity.title,
            "aliases": entity.aliases,
            "status": entity.metadata.get("status"),
        }
        if entity.entity_type == "paper":
            record["identifiers"] = entity.metadata.get("identifiers", {})
        records.append(record)
    rendered = json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True)
    if len(rendered) > max_chars:
        return rendered[:max_chars] + "\n[wiki catalog truncated]"
    return rendered


class LangChainPaperDraftExtractor:
    """Use a chat model for semantic extraction, constrained by Pydantic output."""

    requires_network = True

    def __init__(self, model: BaseChatModel):
        self.model = model

    def extract(
        self,
        *,
        candidate: IngestCandidate,
        document: PaperDocument,
        excerpt: PaperExcerpt,
        skill: SkillSpec,
        wiki_catalog: str,
    ) -> PaperIngestDraft:
        schema_reference = skill.read_reference("wiki-schema.md")
        evidence_policy = skill.read_reference("evidence-policy.md")
        structured_model = self.model.with_structured_output(
            PaperIngestDraft,
            method="json_mode",
        )
        system = SystemMessage(
            content=(
                "You execute the repository-local ingest-paper Skill. Return only one valid "
                "JSON object matching PaperIngestDraft. Do not invent "
                "metadata, values, page numbers, entities, or evidence. Use PDF viewer page "
                "markers exactly as supplied. Reuse a catalog entity by setting existing_id; "
                "otherwise propose a lowercase kebab-case slug. Every extracted experiment "
                "must preserve one atomic result and all material conditions.\n\n"
                f"SKILL INSTRUCTIONS\n{skill.instructions}\n\n"
                f"WIKI SCHEMA\n{schema_reference}\n\n"
                f"EVIDENCE POLICY\n{evidence_policy}\n\n"
                "OUTPUT JSON SCHEMA\n"
                + json.dumps(
                    PaperIngestDraft.model_json_schema(),
                    ensure_ascii=False,
                )
            )
        )
        human = HumanMessage(
            content=(
                "Candidate metadata:\n"
                + candidate.model_dump_json(indent=2)
                + "\n\nExisting Wiki catalog:\n"
                + wiki_catalog
                + "\n\nPDF source contract:\n"
                + json.dumps(
                    {
                        "source_path": document.source_path,
                        "sha256": document.sha256,
                        "page_count": len(document.pages),
                        "selected_pages": excerpt.selected_pages,
                        "excerpt_truncated": excerpt.truncated,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n\nPage-aware PDF excerpt:\n"
                + excerpt.text
            )
        )
        result = structured_model.invoke([system, human])
        if isinstance(result, PaperIngestDraft):
            return result
        return PaperIngestDraft.model_validate(result)


def _clean_mapping(mapping: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in mapping.items() if value is not None}


def _render_page(metadata: Mapping[str, Any], body: str) -> str:
    rendered = render_wiki_page(metadata, body)
    if PLACEHOLDER_PATTERN.search(rendered):
        raise PaperIngestError("Compiled Wiki page contains an unresolved placeholder")
    return rendered


def _slugify(value: str, *, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", errors="ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.casefold()).strip("-")
    if not slug:
        slug = fallback
    return slug[:110].rstrip("-")


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _entity_link(entity_id: str, label: Optional[str] = None) -> str:
    return f"[[{entity_id}|{label}]]" if label else f"[[{entity_id}]]"


def _bullet_lines(values: Iterable[str], *, empty: str = "- None recorded.") -> str:
    rendered = [f"- {value}" for value in values if str(value).strip()]
    return "\n".join(rendered) if rendered else empty


def _locator_mapping(locator: EvidenceLocator) -> Dict[str, Any]:
    return _clean_mapping(
        {
            "locator": locator.render(),
            "pdf_page": locator.pdf_page,
            "paper_page": locator.paper_page,
            "section": locator.section,
            "element": locator.element,
        }
    )


def _base_metadata(
    *,
    entity_id: str,
    entity_type: str,
    title: str,
    aliases: Sequence[str],
    status: str,
    facets: Sequence[str],
    timestamp: str,
    relations: Mapping[str, Sequence[str]],
) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "schema_version": "0.2",
        "id": entity_id,
        "type": entity_type,
        "title": title,
        "aliases": list(aliases),
        "status": status,
    }
    if facets:
        metadata["facets"] = list(facets)
    metadata.update(
        {
            "created_at": timestamp,
            "updated_at": timestamp,
            "relations": {
                key: list(values) for key, values in relations.items() if values
            },
        }
    )
    return metadata


def _paper_identifiers(entity: Entity) -> Mapping[str, Any]:
    value = entity.metadata.get("identifiers")
    return value if isinstance(value, Mapping) else {}


def _normalize_arxiv(value: Any) -> str:
    return re.sub(r"v\d+$", "", str(value or "").strip().casefold())


def _normalize_doi(value: Any) -> str:
    return re.sub(
        r"^https?://(?:dx\.)?doi\.org/", "", str(value or "").strip().casefold()
    )


class WikiDraftCompiler:
    """Resolve entity identities and render a V0.2 page set deterministically."""

    def __init__(
        self,
        wiki_root: Path,
        meta_root: Path,
        *,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self.wiki_root = wiki_root.resolve()
        self.meta_root = meta_root.resolve()
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _resolve_paper(self, index: WikiIndex, draft: PaperIngestDraft) -> str:
        arxiv = _normalize_arxiv(draft.paper.identifiers.arxiv)
        doi = _normalize_doi(draft.paper.identifiers.doi)
        title = normalize_lookup(draft.paper.title)
        for entity in index.unique_entities().values():
            if entity.entity_type != "paper" or not entity.entity_id:
                continue
            identifiers = _paper_identifiers(entity)
            if arxiv and _normalize_arxiv(identifiers.get("arxiv")) == arxiv:
                return entity.entity_id
            if doi and _normalize_doi(identifiers.get("doi")) == doi:
                return entity.entity_id
            if entity.title and normalize_lookup(entity.title) == title:
                return entity.entity_id
        fallback = _short_hash(draft.paper.title)
        slug = _slugify(draft.paper.title, fallback=fallback)
        proposed = f"paper:{slug}"
        if proposed in index.unique_entities():
            proposed = f"paper:{slug}-{fallback}"
        return proposed

    def _resolve_reusable(
        self,
        index: WikiIndex,
        entity_type: str,
        item: MethodDraft | BenchmarkDraft | ModelDraft,
        reserved: set[str],
    ) -> Tuple[str, bool]:
        if item.existing_id:
            entity = index.resolver.exact_entity(item.existing_id)
            if entity is None or entity.entity_type != entity_type:
                raise PaperIngestError(
                    f"{item.key} references missing or wrong-type entity {item.existing_id}"
                )
            return item.existing_id, True
        resolution, resolved_id, _ = index.resolver.resolve_reference(item.title)
        if resolution in {"canonical-id", "title-or-alias"} and resolved_id:
            entity = index.resolver.exact_entity(resolved_id)
            if entity is not None and entity.entity_type == entity_type:
                return resolved_id, True
        fallback = _short_hash(f"{entity_type}:{item.title}")
        slug = item.proposed_slug or _slugify(item.title, fallback=fallback)
        entity_id = f"{entity_type}:{slug}"
        existing = index.resolver.exact_entity(entity_id)
        if existing is not None:
            if existing.entity_type == entity_type and normalize_lookup(
                existing.title or ""
            ) == normalize_lookup(item.title):
                return entity_id, True
            entity_id = f"{entity_type}:{slug}-{fallback}"
        if entity_id in reserved:
            raise PaperIngestError(
                f"Multiple draft entities resolve to the same canonical ID: {entity_id}"
            )
        reserved.add(entity_id)
        return entity_id, False

    def compile(self, draft: PaperIngestDraft) -> CompiledWikiDraft:
        index = build_index(self.wiki_root, self.meta_root)
        existing = index.unique_entities()
        timestamp = self.now().astimezone(timezone.utc).isoformat(timespec="seconds")
        paper_id = self._resolve_paper(index, draft)
        paper_reused = paper_id in existing
        reserved = set(existing)
        if not paper_reused:
            reserved.add(paper_id)

        pages: Dict[str, str] = {}
        created: list[str] = []
        reused: list[str] = [paper_id] if paper_reused else []
        method_ids: Dict[str, str] = {}
        benchmark_ids: Dict[str, str] = {}
        model_ids: Dict[str, str] = {}

        for entity_type, values, destination in (
            ("method", draft.methods, method_ids),
            ("benchmark", draft.benchmarks, benchmark_ids),
            ("model", draft.models, model_ids),
        ):
            for item in values:
                entity_id, is_reused = self._resolve_reusable(
                    index, entity_type, item, reserved
                )
                destination[item.key] = entity_id
                (reused if is_reused else created).append(entity_id)

        paper_slug = paper_id.split(":", 1)[1]
        claim_ids: Dict[str, str] = {}
        for claim in draft.claims:
            stem = _slugify(claim.key, fallback="claim")
            entity_id = f"claim:{paper_slug}-{stem}-{_short_hash(claim.statement)}"
            claim_ids[claim.key] = entity_id
            if entity_id in existing:
                reused.append(entity_id)
            else:
                reserved.add(entity_id)
                created.append(entity_id)

        experiment_ids: Dict[str, str] = {}
        for experiment in draft.experiments:
            fingerprint = json.dumps(
                {
                    "key": experiment.key,
                    "context": experiment.context_length,
                    "metric": experiment.metric.name,
                    "value": experiment.result.value,
                    "benchmark": experiment.benchmark_key,
                },
                sort_keys=True,
                ensure_ascii=False,
            )
            stem = _slugify(experiment.key, fallback="experiment")
            entity_id = f"experiment:{paper_slug}-{stem}-{_short_hash(fingerprint)}"
            experiment_ids[experiment.key] = entity_id
            if entity_id in existing:
                reused.append(entity_id)
            else:
                reserved.add(entity_id)
                created.append(entity_id)

        if not paper_reused:
            metadata = _base_metadata(
                entity_id=paper_id,
                entity_type="paper",
                title=draft.paper.title,
                aliases=(),
                status=draft.paper.status,
                facets=draft.paper.facets,
                timestamp=timestamp,
                relations={
                    "proposes": tuple(method_ids.values()),
                    "reports": tuple(experiment_ids.values()),
                },
            )
            metadata.update(
                {
                    "authors": list(draft.paper.authors),
                    "year": draft.paper.year,
                    "venue": draft.paper.venue,
                    "identifiers": _clean_mapping(draft.paper.identifiers.model_dump()),
                    "urls": _clean_mapping(draft.paper.urls.model_dump()),
                }
            )
            reported = (
                f"{item.statement} Evidence: {item.evidence.render() if item.evidence else 'not located.'}"
                for item in draft.paper.reported_limitations
            )
            inferred = (
                f"{item.statement} Evidence: {item.evidence.render() if item.evidence else 'not located.'}"
                for item in draft.paper.inferred_limitations
            )
            body = f"""# {draft.paper.title}

## Problem

{draft.paper.problem}

## Motivation

{draft.paper.motivation}

## Assumptions and scope

{draft.paper.assumptions_and_scope}

## Method overview

{draft.paper.method_overview}

## Structured entities

### Methods

{_bullet_lines(_entity_link(entity_id) for entity_id in method_ids.values())}

### Claims

{_bullet_lines(_entity_link(entity_id) for entity_id in claim_ids.values())}

### Experiments

{_bullet_lines(_entity_link(entity_id) for entity_id in experiment_ids.values())}

## Limitations

### Reported by the paper

{_bullet_lines(reported)}

### Agent analysis

{_bullet_lines(inferred)}

## Related papers

{_bullet_lines(_entity_link(entity_id) for entity_id in draft.paper.related_paper_ids)}

## Open questions

{_bullet_lines(draft.paper.open_questions)}
"""
            pages[f"papers/{paper_slug}.md"] = _render_page(metadata, body)
            created.append(paper_id)

        for method in draft.methods:
            entity_id = method_ids[method.key]
            if entity_id in existing:
                continue
            metadata = _base_metadata(
                entity_id=entity_id,
                entity_type="method",
                title=method.title,
                aliases=method.aliases,
                status=draft.paper.status,
                facets=method.facets,
                timestamp=timestamp,
                relations={},
            )
            metadata.update(
                {
                    "definition": method.definition,
                    "sparsity": method.sparsity,
                    "implementations": list(method.implementations),
                }
            )
            body = f"""# {method.title}

## Definition

{method.definition}

## Provenance

- Paper: {_entity_link(paper_id)}
- Evidence: {method.evidence.render()}

## Implementations

{_bullet_lines(method.implementations)}
"""
            pages[f"methods/{entity_id.split(':', 1)[1]}.md"] = _render_page(
                metadata, body
            )

        for benchmark in draft.benchmarks:
            entity_id = benchmark_ids[benchmark.key]
            if entity_id in existing:
                continue
            metadata = _base_metadata(
                entity_id=entity_id,
                entity_type="benchmark",
                title=benchmark.title,
                aliases=benchmark.aliases,
                status=draft.paper.status,
                facets=benchmark.facets,
                timestamp=timestamp,
                relations={},
            )
            metadata.update(
                {
                    "task": benchmark.task,
                    "metrics": list(benchmark.metrics),
                    "source": (
                        {"url": benchmark.source_url}
                        if benchmark.source_url
                        else {"paper": paper_id}
                    ),
                }
            )
            body = f"""# {benchmark.title}

## Task

{benchmark.task}

## Metrics

{_bullet_lines(benchmark.metrics)}

## Provenance

- Paper: {_entity_link(paper_id)}
- Evidence: {benchmark.evidence.render()}
"""
            pages[f"benchmarks/{entity_id.split(':', 1)[1]}.md"] = _render_page(
                metadata, body
            )

        for model in draft.models:
            entity_id = model_ids[model.key]
            if entity_id in existing:
                continue
            metadata = _base_metadata(
                entity_id=entity_id,
                entity_type="model",
                title=model.title,
                aliases=model.aliases,
                status=draft.paper.status,
                facets=model.facets,
                timestamp=timestamp,
                relations={},
            )
            metadata.update(
                {
                    "family": model.family,
                    "parameters": model.parameters,
                    "source": (
                        {"url": model.source_url}
                        if model.source_url
                        else {"paper": paper_id}
                    ),
                }
            )
            body = f"""# {model.title}

## Model family

{model.family}

## Provenance

- Paper: {_entity_link(paper_id)}
- Evidence: {model.evidence.render()}
"""
            pages[f"models/{entity_id.split(':', 1)[1]}.md"] = _render_page(
                metadata, body
            )

        for claim in draft.claims:
            entity_id = claim_ids[claim.key]
            if entity_id in existing:
                continue
            scope = dict(claim.scope)
            scope.update(
                {
                    "attribution": claim.attribution,
                    "evidence_type": claim.evidence_type,
                    "evidence_status": claim.evidence_status,
                }
            )
            if claim.evidence:
                scope["evidence_locator"] = claim.evidence.render()
                scope["evidence_pdf_page"] = claim.evidence.pdf_page
            metadata = _base_metadata(
                entity_id=entity_id,
                entity_type="claim",
                title=claim.statement[:160],
                aliases=(),
                status=draft.paper.status,
                facets=claim.facets,
                timestamp=timestamp,
                relations={},
            )
            metadata.update(
                {
                    "statement": claim.statement,
                    "assessment": "open",
                    "scope": scope,
                }
            )
            evidence_text = (
                claim.evidence.render() if claim.evidence else "Not located."
            )
            body = f"""# {claim.statement}

## Statement

{claim.statement}

## Attribution and evidence

- Attribution: {claim.attribution}
- Evidence type: {claim.evidence_type}
- Evidence status: {claim.evidence_status}
- Locator: {evidence_text}
- Source paper: {_entity_link(paper_id)}
"""
            pages[f"claims/{entity_id.split(':', 1)[1]}.md"] = _render_page(
                metadata, body
            )

        benchmark_titles = {item.key: item.title for item in draft.benchmarks}
        for experiment in draft.experiments:
            entity_id = experiment_ids[experiment.key]
            if entity_id in existing:
                continue
            support_ids = tuple(
                claim_ids[key] for key in experiment.supports_claim_keys
            )
            contradiction_ids = tuple(
                claim_ids[key] for key in experiment.contradicts_claim_keys
            )
            method_values = [method_ids[key] for key in experiment.method_keys]
            model_values = [model_ids[key] for key in experiment.model_keys]
            benchmark_id = benchmark_ids[experiment.benchmark_key]
            title = (
                f"{draft.paper.title}: {benchmark_titles[experiment.benchmark_key]} "
                f"at {experiment.context_length} tokens"
            )
            metadata = _base_metadata(
                entity_id=entity_id,
                entity_type="experiment",
                title=title,
                aliases=(),
                status=draft.paper.status,
                facets=experiment.facets,
                timestamp=timestamp,
                relations={
                    "supports": support_ids,
                    "contradicts": contradiction_ids,
                },
            )
            metadata.update(
                {
                    "paper": paper_id,
                    "method": method_values,
                    "model": model_values,
                    "benchmark": benchmark_id,
                    "context_length": experiment.context_length,
                    "sparsity": dict(experiment.sparsity),
                    "metric": _clean_mapping(experiment.metric.model_dump()),
                    "result": _clean_mapping(experiment.result.model_dump()),
                    "evidence": _locator_mapping(experiment.evidence),
                }
            )
            body = f"""# {title}

## Experimental conditions

- Paper: {_entity_link(paper_id)}
- Method: {', '.join(_entity_link(value) for value in method_values)}
- Model: {', '.join(_entity_link(value) for value in model_values)}
- Benchmark: {_entity_link(benchmark_id)}
- Context length: {experiment.context_length}
- Sparsity: `{json.dumps(experiment.sparsity, ensure_ascii=False, sort_keys=True)}`

## Result

- Metric: {experiment.metric.name}
- Value: {experiment.result.value}
- Unit: {experiment.result.unit or experiment.metric.unit or 'not reported'}
- Baseline: {experiment.result.baseline or 'not reported'}
- Comparison: {experiment.result.comparison or 'not reported'}
- Evidence: {experiment.evidence.render()}

## Claim links

### Supports

{_bullet_lines(_entity_link(value) for value in support_ids)}

### Contradicts

{_bullet_lines(_entity_link(value) for value in contradiction_ids)}
"""
            pages[f"experiments/{entity_id.split(':', 1)[1]}.md"] = _render_page(
                metadata, body
            )

        return CompiledWikiDraft(
            paper_id=paper_id,
            pages=dict(sorted(pages.items())),
            created_entity_ids=tuple(dict.fromkeys(created)),
            reused_entity_ids=tuple(dict.fromkeys(reused)),
        )


class PaperIngestPipeline:
    """Run one selected candidate through extraction, staging, and publication."""

    def __init__(
        self,
        settings: HarnessSettings,
        *,
        extractor: Optional[PaperDraftExtractor] = None,
        now: Optional[Callable[[], datetime]] = None,
        artifact_recorder: Optional[SemanticArtifactRecorder] = None,
    ):
        self.settings = settings
        self.registry = SkillRegistry(settings.skills_root)
        self.skill = self.registry.get("ingest-paper")
        self.extractor = extractor or self._default_extractor(settings)
        self.compiler = WikiDraftCompiler(
            settings.wiki_root,
            settings.wiki_meta_root,
            now=now,
        )
        self.writer = WikiSourceWriter(settings.wiki_root, settings.wiki_meta_root)
        self.artifact_recorder = artifact_recorder

    @staticmethod
    def _default_extractor(settings: HarnessSettings) -> PaperDraftExtractor:
        if not settings.model:
            raise ValueError(
                "Paper ingestion needs an injected extractor or HARNESS_MODEL/--model."
            )
        return LangChainPaperDraftExtractor(init_chat_model(settings.model))

    @property
    def requires_network(self) -> bool:
        return bool(getattr(self.extractor, "requires_network", True))

    def _source_path(self, candidate: IngestCandidate) -> Path:
        raw = str(candidate.local_pdf_path or "").strip().replace("\\", "/")
        relative = PurePosixPath(raw)
        if (
            not raw
            or relative.is_absolute()
            or ".." in relative.parts
            or ":" in relative.parts[0]
        ):
            raise PaperIngestError(
                "Selected candidate requires a safe repository-relative local_pdf_path"
            )
        return self.settings.repository_root / relative

    def ingest(
        self,
        candidate: IngestCandidate,
        *,
        preview: bool = False,
        artifact_context: Optional[SemanticArtifactContext] = None,
    ) -> PaperIngestResult:
        document = extract_pdf_document(
            self._source_path(candidate),
            self.settings.repository_root,
        )
        excerpt = select_paper_excerpt(document)
        catalog = render_wiki_catalog(
            build_index(self.settings.wiki_root, self.settings.wiki_meta_root)
        )
        draft = self.extractor.extract(
            candidate=candidate,
            document=document,
            excerpt=excerpt,
            skill=self.skill,
            wiki_catalog=catalog,
        )
        if draft.candidate_id != candidate.candidate_id:
            raise PaperIngestError(
                "Structured extraction candidate_id does not match the selected candidate"
            )
        artifact_ids: list[str] = []
        if self.artifact_recorder is not None and artifact_context is not None:
            artifact = self.artifact_recorder.record(
                kind="paper-ingest",
                context=artifact_context.with_updates(
                    pdf_sha256=document.sha256,
                    source_ids=(candidate.candidate_id,),
                ),
                skill=self.skill,
                schema_resources=(
                    "references/wiki-schema.md",
                    "references/evidence-policy.md",
                    "references/ingest-draft-schema.md",
                    "assets/paper-template.md",
                ),
                output=draft,
            )
            artifact_ids.append(artifact.artifact_id)
        compiled = self.compiler.compile(draft)
        diagnostics: Tuple[Diagnostic, ...]
        changed_paths: Tuple[str, ...]
        status: Literal["published", "preview", "no-change"]
        if preview:
            diagnostics = self.writer.validate(compiled.pages)
            errors = tuple(item for item in diagnostics if item.severity == "ERROR")
            if errors:
                raise PaperIngestError(
                    "Preview contains Wiki schema errors: "
                    + ", ".join(item.code for item in errors)
                )
            changed_paths = tuple(compiled.pages)
            status = "preview"
        else:
            report = self.writer.publish(compiled.pages)
            diagnostics = report.diagnostics
            changed_paths = report.changed_paths
            status = "published" if changed_paths else "no-change"
        if self.artifact_recorder is not None and artifact_context is not None:
            self.artifact_recorder.link_publication(
                artifact_ids,
                action_id=artifact_context.action_id,
                changed_sources=tuple(f"wiki/{path}" for path in changed_paths),
            )
        return PaperIngestResult(
            candidate_id=candidate.candidate_id,
            paper_id=compiled.paper_id,
            status=status,
            created_entity_ids=compiled.created_entity_ids,
            reused_entity_ids=compiled.reused_entity_ids,
            changed_paths=changed_paths,
            diagnostic_codes=tuple(dict.fromkeys(item.code for item in diagnostics)),
            semantic_artifact_ids=tuple(artifact_ids),
            pdf_pages=len(document.pages),
            selected_pages=excerpt.selected_pages,
        )
