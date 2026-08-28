"""Structured contracts for Skill-driven paper ingestion."""

from __future__ import annotations

import re
from typing import Dict, Literal, Mapping, Optional, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


JsonScalar = Union[str, int, float, bool, None]
IngestStatus = Literal["draft", "needs-review"]
KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,119}$")


class IngestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_text(value: str, field_name: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field_name} cannot be blank")
    return clean


def _validate_key(value: str) -> str:
    clean = value.strip()
    if not KEY_PATTERN.fullmatch(clean):
        raise ValueError(
            "local keys must start with a letter and use at most 32 letters, "
            "digits, underscores, or hyphens"
        )
    return clean


def _unique_text(values: Tuple[str, ...], field_name: str) -> Tuple[str, ...]:
    cleaned = tuple(_require_text(str(value), field_name) for value in values)
    if len(set(cleaned)) != len(cleaned):
        raise ValueError(f"{field_name} cannot contain duplicates")
    return cleaned


def _normalized_name(value: str) -> str:
    return " ".join(value.casefold().split())


class EvidenceLocator(IngestModel):
    """A page-aware location that another reader can reproduce."""

    pdf_page: int = Field(ge=1)
    paper_page: Optional[str] = None
    section: Optional[str] = None
    element: Optional[str] = None
    description: str

    @model_validator(mode="after")
    def _validate_locator(self) -> "EvidenceLocator":
        _require_text(self.description, "description")
        for field_name in ("paper_page", "section", "element"):
            value = getattr(self, field_name)
            if value is not None and not value.strip():
                raise ValueError(f"{field_name} cannot be blank when provided")
        return self

    def render(self) -> str:
        parts = [f"PDF p. {self.pdf_page}"]
        if self.paper_page:
            parts.append(f"paper p. {self.paper_page}")
        if self.section:
            parts.append(f"Section {self.section}")
        if self.element:
            parts.append(self.element)
        parts.append(self.description.strip())
        return ", ".join(parts)


class LocatedStatement(IngestModel):
    statement: str
    evidence: Optional[EvidenceLocator] = None

    @field_validator("statement")
    @classmethod
    def _statement_not_blank(cls, value: str) -> str:
        return _require_text(value, "statement")


class PaperIdentifiers(IngestModel):
    arxiv: Optional[str] = None
    doi: Optional[str] = None

    @model_validator(mode="after")
    def _clean_identifiers(self) -> "PaperIdentifiers":
        for field_name in ("arxiv", "doi"):
            value = getattr(self, field_name)
            if value is not None and not value.strip():
                raise ValueError(f"{field_name} cannot be blank when provided")
        return self


class PaperUrls(IngestModel):
    paper: Optional[str] = None
    pdf: Optional[str] = None

    @model_validator(mode="after")
    def _clean_urls(self) -> "PaperUrls":
        for field_name in ("paper", "pdf"):
            value = getattr(self, field_name)
            if value is not None and not value.strip():
                raise ValueError(f"{field_name} cannot be blank when provided")
        return self


class PaperDraft(IngestModel):
    title: str
    authors: Tuple[str, ...] = ()
    year: Optional[int] = Field(default=None, ge=1800, le=2200)
    venue: Optional[str] = None
    identifiers: PaperIdentifiers = Field(default_factory=PaperIdentifiers)
    urls: PaperUrls = Field(default_factory=PaperUrls)
    status: IngestStatus = "draft"
    facets: Tuple[str, ...] = ()
    problem: str
    motivation: str
    assumptions_and_scope: str
    method_overview: str
    reported_limitations: Tuple[LocatedStatement, ...] = ()
    inferred_limitations: Tuple[LocatedStatement, ...] = ()
    related_paper_ids: Tuple[str, ...] = ()
    open_questions: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_paper(self) -> "PaperDraft":
        for field_name in (
            "title",
            "problem",
            "motivation",
            "assumptions_and_scope",
            "method_overview",
        ):
            _require_text(str(getattr(self, field_name)), field_name)
        _unique_text(self.authors, "authors")
        _unique_text(self.facets, "facets")
        _unique_text(self.related_paper_ids, "related_paper_ids")
        _unique_text(self.open_questions, "open_questions")
        if any(not value.startswith("paper:") for value in self.related_paper_ids):
            raise ValueError("related_paper_ids must use the paper: prefix")
        return self


class ReusableEntityDraft(IngestModel):
    key: str
    existing_id: Optional[str] = None
    proposed_slug: Optional[str] = None
    title: str
    aliases: Tuple[str, ...] = ()
    facets: Tuple[str, ...] = ()
    evidence: EvidenceLocator

    @model_validator(mode="after")
    def _validate_reusable_entity(self) -> "ReusableEntityDraft":
        _validate_key(self.key)
        _require_text(self.title, "title")
        _unique_text(self.aliases, "aliases")
        _unique_text(self.facets, "facets")
        if self.existing_id is not None and not self.existing_id.strip():
            raise ValueError("existing_id cannot be blank when provided")
        if self.proposed_slug is not None and not SLUG_PATTERN.fullmatch(
            self.proposed_slug
        ):
            raise ValueError("proposed_slug must use lowercase kebab-case")
        return self


class MethodDraft(ReusableEntityDraft):
    definition: str
    sparsity: Optional[Dict[str, JsonScalar]] = None
    implementations: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_method(self) -> "MethodDraft":
        _require_text(self.definition, "definition")
        _unique_text(self.implementations, "implementations")
        if self.existing_id and not self.existing_id.startswith("method:"):
            raise ValueError("method existing_id must use the method: prefix")
        return self


class BenchmarkDraft(ReusableEntityDraft):
    task: str
    metrics: Tuple[str, ...]
    source_url: Optional[str] = None

    @model_validator(mode="after")
    def _validate_benchmark(self) -> "BenchmarkDraft":
        _require_text(self.task, "task")
        if not self.metrics:
            raise ValueError("benchmark metrics cannot be empty")
        _unique_text(self.metrics, "metrics")
        if self.existing_id and not self.existing_id.startswith("benchmark:"):
            raise ValueError("benchmark existing_id must use the benchmark: prefix")
        return self


class ModelDraft(ReusableEntityDraft):
    family: str
    parameters: Union[str, int, float, None] = None
    source_url: Optional[str] = None

    @model_validator(mode="after")
    def _validate_model(self) -> "ModelDraft":
        _require_text(self.family, "family")
        if self.existing_id and not self.existing_id.startswith("model:"):
            raise ValueError("model existing_id must use the model: prefix")
        return self


class ClaimDraft(IngestModel):
    key: str
    statement: str
    attribution: Literal["author", "agent-analysis"]
    evidence_type: Literal["author-stated", "experiment-supported", "inferred"]
    evidence_status: Literal["located", "partial", "unlocated"]
    evidence: Optional[EvidenceLocator] = None
    scope: Dict[str, JsonScalar]
    facets: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_claim(self) -> "ClaimDraft":
        _validate_key(self.key)
        _require_text(self.statement, "statement")
        _unique_text(self.facets, "facets")
        if self.evidence_status == "located" and self.evidence is None:
            raise ValueError("a located claim requires an evidence locator")
        if self.evidence_type == "experiment-supported" and self.evidence is None:
            raise ValueError(
                "an experiment-supported claim requires an evidence locator"
            )
        if not self.scope:
            raise ValueError("claim scope cannot be empty")
        return self


class MetricDraft(IngestModel):
    name: str
    direction: Optional[Literal["higher-is-better", "lower-is-better"]] = None
    unit: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        return _require_text(value, "metric name")


class ResultDraft(IngestModel):
    value: Union[str, int, float]
    unit: Optional[str] = None
    baseline: Optional[str] = None
    comparison: Optional[str] = None

    @model_validator(mode="after")
    def _validate_result(self) -> "ResultDraft":
        if isinstance(self.value, str):
            _require_text(self.value, "result value")
        return self


class ExperimentDraft(IngestModel):
    key: str
    method_keys: Tuple[str, ...]
    model_keys: Tuple[str, ...]
    benchmark_key: str
    context_length: int = Field(gt=0)
    sparsity: Dict[str, JsonScalar]
    metric: MetricDraft
    result: ResultDraft
    evidence: EvidenceLocator
    supports_claim_keys: Tuple[str, ...] = ()
    contradicts_claim_keys: Tuple[str, ...] = ()
    facets: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_experiment(self) -> "ExperimentDraft":
        _validate_key(self.key)
        _validate_key(self.benchmark_key)
        if not self.method_keys:
            raise ValueError("experiment method_keys cannot be empty")
        if not self.model_keys:
            raise ValueError("experiment model_keys cannot be empty")
        for field_name in (
            "method_keys",
            "model_keys",
            "supports_claim_keys",
            "contradicts_claim_keys",
        ):
            values = tuple(getattr(self, field_name))
            _unique_text(values, field_name)
            for value in values:
                _validate_key(value)
        if set(self.supports_claim_keys) & set(self.contradicts_claim_keys):
            raise ValueError(
                "an experiment cannot both support and contradict the same claim"
            )
        _unique_text(self.facets, "facets")
        if not self.sparsity:
            raise ValueError("experiment sparsity cannot be empty")
        return self


class PaperIngestDraft(IngestModel):
    candidate_id: str
    paper: PaperDraft
    methods: Tuple[MethodDraft, ...] = ()
    benchmarks: Tuple[BenchmarkDraft, ...] = ()
    models: Tuple[ModelDraft, ...] = ()
    claims: Tuple[ClaimDraft, ...] = ()
    experiments: Tuple[ExperimentDraft, ...] = ()

    @model_validator(mode="after")
    def _validate_graph(self) -> "PaperIngestDraft":
        _require_text(self.candidate_id, "candidate_id")
        groups: Mapping[str, Tuple[IngestModel, ...]] = {
            "method": self.methods,
            "benchmark": self.benchmarks,
            "model": self.models,
            "claim": self.claims,
            "experiment": self.experiments,
        }
        keys: Dict[str, set[str]] = {}
        for group_name, values in groups.items():
            group_keys = [str(getattr(value, "key")) for value in values]
            if len(set(group_keys)) != len(group_keys):
                raise ValueError(f"duplicate {group_name} local key")
            keys[group_name] = set(group_keys)

        for group_name, values in (
            ("method", self.methods),
            ("benchmark", self.benchmarks),
            ("model", self.models),
        ):
            names = [_normalized_name(value.title) for value in values]
            existing_ids = [value.existing_id for value in values if value.existing_id]
            if len(set(names)) != len(names):
                raise ValueError(f"duplicate {group_name} canonical title")
            if len(set(existing_ids)) != len(existing_ids):
                raise ValueError(f"duplicate {group_name} existing_id")

        linked_claims: set[str] = set()
        for experiment in self.experiments:
            if not set(experiment.method_keys) <= keys["method"]:
                raise ValueError("experiment references an unknown method key")
            if not set(experiment.model_keys) <= keys["model"]:
                raise ValueError("experiment references an unknown model key")
            if experiment.benchmark_key not in keys["benchmark"]:
                raise ValueError("experiment references an unknown benchmark key")
            claim_keys = set(experiment.supports_claim_keys) | set(
                experiment.contradicts_claim_keys
            )
            if not claim_keys <= keys["claim"]:
                raise ValueError("experiment references an unknown claim key")
            linked_claims.update(claim_keys)

        for claim in self.claims:
            if (
                claim.evidence_type == "experiment-supported"
                and claim.key not in linked_claims
            ):
                raise ValueError(
                    "every experiment-supported claim must be linked from an experiment"
                )
        return self


class IngestCandidate(IngestModel):
    candidate_id: str
    title: str
    source: str
    source_id: str
    authors: Tuple[str, ...] = ()
    year: Optional[int] = Field(default=None, ge=1800, le=2200)
    venue: Optional[str] = None
    abstract: Optional[str] = None
    paper_url: Optional[str] = None
    pdf_url: Optional[str] = None
    doi: Optional[str] = None
    local_pdf_path: Optional[str] = None
    target_facets: Tuple[str, ...] = ()
    search_run_path: str

    @model_validator(mode="after")
    def _validate_candidate(self) -> "IngestCandidate":
        for field_name in (
            "candidate_id",
            "title",
            "source",
            "source_id",
            "search_run_path",
        ):
            _require_text(str(getattr(self, field_name)), field_name)
        _unique_text(self.authors, "authors")
        _unique_text(self.target_facets, "target_facets")
        return self


class PdfPageText(IngestModel):
    pdf_page: int = Field(ge=1)
    text: str


class PaperDocument(IngestModel):
    source_path: str
    sha256: str
    pages: Tuple[PdfPageText, ...]

    @model_validator(mode="after")
    def _validate_document(self) -> "PaperDocument":
        if not self.pages:
            raise ValueError("PDF extraction returned no pages")
        if [page.pdf_page for page in self.pages] != list(
            range(1, len(self.pages) + 1)
        ):
            raise ValueError("PDF pages must be consecutive and one-indexed")
        return self


class PaperExcerpt(IngestModel):
    text: str
    selected_pages: Tuple[int, ...]
    truncated: bool


class PaperIngestResult(IngestModel):
    candidate_id: str
    paper_id: str
    status: Literal["published", "preview", "no-change"]
    created_entity_ids: Tuple[str, ...] = ()
    reused_entity_ids: Tuple[str, ...] = ()
    changed_paths: Tuple[str, ...] = ()
    diagnostic_codes: Tuple[str, ...] = ()
    semantic_artifact_ids: Tuple[str, ...] = ()
    pdf_pages: int = Field(ge=1)
    selected_pages: Tuple[int, ...]
    model_calls: int = Field(default=1, ge=1)
    schema_repair_applied: bool = False
