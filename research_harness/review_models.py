"""Strict contracts for the review-first research loop.

The review path deliberately keeps provisional reading notes separate from
citation-ready evidence.  These models are also the serialized contract for
the run artifacts under ``research/<id>/reviews/<run-id>``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Literal, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ReviewProfile = Literal["smoke", "standard"]
SourceType = Literal["paper", "project", "web"]
SourceRole = Literal[
    "survey",
    "primary-study",
    "benchmark",
    "reproduction",
    "project",
    "background",
]
ProviderName = Literal[
    "deepxiv",
    "semantic_scholar",
    "github",
    "tavily",
    "manual",
]
ReviewStage = Literal[
    "frame",
    "retrieval",
    "screening",
    "skim",
    "reasoning",
    "deep-read",
    "assessment",
    "synthesis",
]
FacetStatus = Literal["missing", "partial", "covered"]
EvidenceStatus = Literal["located", "cross-checked", "verified"]
AssessmentResult = Literal[
    "supported-consensus",
    "contested",
    "insufficient-evidence",
]
RelationKind = Literal[
    "possible-same-work",
    "alias-of",
    "variant-of",
    "extends",
    "implements",
    "evaluates",
    "supports",
    "opposes",
]
ReviewGapKind = Literal[
    "blocking-uncertainty",
    "missing-facet",
    "single-source-claim",
    "incomparable-evidence",
    "method-evidence",
    "orphan-concept",
    "stale-evidence",
]


class ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReviewRunConfig(ReviewModel):
    schema_version: Literal["0.1"] = "0.1"
    research_id: str
    run_id: str
    thread_id: str
    profile: ReviewProfile
    question: str
    title: str
    required_facets: Tuple[str, ...]
    candidate_hypotheses: Tuple[str, ...] = ()
    max_sources: int = Field(ge=1, le=500)
    max_skims: int = Field(ge=1, le=200)
    max_deep_reads: int = Field(ge=1, le=100)
    minimum_deep_read_papers: int = Field(default=2, ge=0, le=100)
    minimum_core_study_deep_reads: int = Field(default=0, ge=0, le=100)
    max_survey_deep_reads: int = Field(default=2, ge=0, le=100)
    max_nonpaper_deep_reads: int = Field(default=2, ge=0, le=100)
    source_role_targets: Dict[str, int] = Field(default_factory=dict)
    max_promotions: int = Field(ge=0, le=50)
    paper_source_quota: int = Field(ge=0)
    project_source_quota: int = Field(ge=0)
    web_source_quota: int = Field(ge=0)
    max_search_rounds: int = Field(default=3, ge=1, le=10)
    max_queries: int = Field(default=12, ge=1, le=100)
    network_concurrency: int = Field(default=4, ge=1, le=32)
    skim_concurrency: int = Field(default=2, ge=1, le=16)
    deep_read_concurrency: int = Field(default=2, ge=1, le=16)
    allow_network: bool = False
    allow_single_model_fallback: bool = False
    fast_model: Optional[str] = None
    fast_model_base_url: Optional[str] = None
    reasoning_model: Optional[str] = None
    reasoning_model_base_url: Optional[str] = None
    model_fingerprint: Optional[str] = None
    single_model_fallback_used: bool = False
    canary: bool = False
    stop_after: ReviewStage = "synthesis"
    created_at: str

    @model_validator(mode="after")
    def _validate_budget(self) -> "ReviewRunConfig":
        for name in ("research_id", "run_id", "thread_id", "question", "title"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} cannot be blank")
        if self.max_skims > self.max_sources:
            raise ValueError("max_skims cannot exceed max_sources")
        if self.max_deep_reads > self.max_skims:
            raise ValueError("max_deep_reads cannot exceed max_skims")
        if self.minimum_deep_read_papers > self.max_deep_reads:
            raise ValueError(
                "minimum_deep_read_papers cannot exceed max_deep_reads"
            )
        if self.minimum_core_study_deep_reads > self.max_deep_reads:
            raise ValueError(
                "minimum_core_study_deep_reads cannot exceed max_deep_reads"
            )
        if self.max_survey_deep_reads > self.max_deep_reads:
            raise ValueError("max_survey_deep_reads cannot exceed max_deep_reads")
        if self.max_nonpaper_deep_reads > self.max_deep_reads:
            raise ValueError("max_nonpaper_deep_reads cannot exceed max_deep_reads")
        allowed_roles = {
            "survey",
            "primary-study",
            "benchmark",
            "reproduction",
            "project",
            "background",
        }
        unknown_roles = set(self.source_role_targets) - allowed_roles
        if unknown_roles:
            raise ValueError(
                "source_role_targets contains unsupported roles: "
                + ", ".join(sorted(unknown_roles))
            )
        if any(value < 0 for value in self.source_role_targets.values()):
            raise ValueError("source_role_targets values cannot be negative")
        if sum(self.source_role_targets.values()) > self.max_skims:
            raise ValueError("source_role_targets cannot exceed max_skims")
        if self.max_promotions > self.max_deep_reads:
            raise ValueError("max_promotions cannot exceed max_deep_reads")
        quota_total = (
            self.paper_source_quota
            + self.project_source_quota
            + self.web_source_quota
        )
        if quota_total != self.max_sources:
            raise ValueError("source quotas must sum to max_sources")
        if len(set(self.required_facets)) != len(self.required_facets):
            raise ValueError("required_facets cannot contain duplicates")
        for role in ("fast", "reasoning"):
            model = getattr(self, f"{role}_model")
            base_url = getattr(self, f"{role}_model_base_url")
            if bool(model) != bool(base_url):
                raise ValueError(f"{role} model and base URL must be recorded together")
        if self.model_fingerprint and not re.fullmatch(
            r"[a-f0-9]{64}", self.model_fingerprint
        ):
            raise ValueError("model_fingerprint must be a lowercase SHA-256 digest")
        return self

    @classmethod
    def for_profile(
        cls,
        *,
        research_id: str,
        run_id: str,
        thread_id: str,
        profile: ReviewProfile,
        question: str,
        title: str,
        required_facets: Tuple[str, ...],
        candidate_hypotheses: Tuple[str, ...],
        allow_network: bool,
        allow_single_model_fallback: bool,
        canary: bool,
        stop_after: ReviewStage,
        created_at: str,
        fast_model: Optional[str] = None,
        fast_model_base_url: Optional[str] = None,
        reasoning_model: Optional[str] = None,
        reasoning_model_base_url: Optional[str] = None,
        model_fingerprint: Optional[str] = None,
        single_model_fallback_used: bool = False,
    ) -> "ReviewRunConfig":
        if profile == "smoke":
            budgets = {
                "max_sources": 8,
                "max_skims": 4,
                "max_deep_reads": 2,
                "minimum_deep_read_papers": 2,
                "minimum_core_study_deep_reads": 0,
                "max_survey_deep_reads": 2,
                "max_nonpaper_deep_reads": 2,
                "source_role_targets": {},
                "max_promotions": 0,
                "paper_source_quota": 5,
                "project_source_quota": 1,
                "web_source_quota": 2,
                "max_search_rounds": 1,
                "max_queries": 3,
            }
        else:
            budgets = {
                "max_sources": 50,
                "max_skims": 20,
                "max_deep_reads": 10,
                "minimum_deep_read_papers": 6,
                "minimum_core_study_deep_reads": 6,
                "max_survey_deep_reads": 2,
                "max_nonpaper_deep_reads": 2,
                "source_role_targets": {
                    "survey": 2,
                    "primary-study": 6,
                    "benchmark": 2,
                    "reproduction": 1,
                    "project": 2,
                },
                "max_promotions": 6,
                "paper_source_quota": 30,
                "project_source_quota": 10,
                "web_source_quota": 10,
                "max_search_rounds": 3,
                "max_queries": 12,
            }
        return cls(
            research_id=research_id,
            run_id=run_id,
            thread_id=thread_id,
            profile=profile,
            question=question,
            title=title,
            required_facets=required_facets,
            candidate_hypotheses=candidate_hypotheses,
            allow_network=allow_network,
            allow_single_model_fallback=allow_single_model_fallback,
            fast_model=fast_model,
            fast_model_base_url=fast_model_base_url,
            reasoning_model=reasoning_model,
            reasoning_model_base_url=reasoning_model_base_url,
            model_fingerprint=model_fingerprint,
            single_model_fallback_used=single_model_fallback_used,
            canary=canary,
            stop_after=stop_after,
            created_at=created_at,
            **budgets,
        )


class ReviewScope(ReviewModel):
    research_id: str
    title: str
    question: str
    required_facets: Tuple[str, ...]
    included_concepts: Tuple[str, ...] = ()
    excluded_concepts: Tuple[str, ...] = ()
    candidate_hypotheses: Tuple[str, ...] = ()
    seed_queries: Tuple[str, ...] = ()


class RetrievalQuery(ReviewModel):
    id: str
    round: int = Field(ge=1)
    provider: Literal["deepxiv", "github", "tavily"]
    text: str = Field(min_length=2, max_length=500)
    purpose: str = Field(min_length=2, max_length=800)
    target_facets: Tuple[str, ...] = ()
    uncertainty_id: Optional[str] = None
    disconfirming: bool = False

    @field_validator("text", "purpose")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        return " ".join(value.split())


class QueryPlan(ReviewModel):
    rationale: str = Field(min_length=2, max_length=2000)
    queries: Tuple[RetrievalQuery, ...] = Field(min_length=1, max_length=8)


class DiscoveryRecord(ReviewModel):
    query_id: str
    provider: ProviderName
    rank: int = Field(ge=1)
    retrieved_at: str
    provider_score: Optional[float] = None


class SourceRecord(ReviewModel):
    source_id: str
    source_type: SourceType
    provider: ProviderName
    title: str
    canonical_url: str
    authors: Tuple[str, ...] = ()
    year: Optional[int] = Field(default=None, ge=1800, le=2200)
    venue: Optional[str] = None
    abstract: Optional[str] = None
    snippet: Optional[str] = None
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    pdf_url: Optional[str] = None
    repository: Optional[str] = None
    license: Optional[str] = None
    stars: Optional[int] = Field(default=None, ge=0)
    version: Optional[str] = None
    updated_at: Optional[str] = None
    target_facets: Tuple[str, ...] = ()
    discoveries: Tuple[DiscoveryRecord, ...]
    local_path: Optional[str] = None
    content_sha256: Optional[str] = None
    content_preview: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_identity(self) -> "SourceRecord":
        for name in ("source_id", "title", "canonical_url"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} cannot be blank")
        if not self.discoveries:
            raise ValueError("a source must retain discovery provenance")
        if self.content_sha256 and not re.fullmatch(
            r"[a-f0-9]{64}", self.content_sha256
        ):
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        return self


class SourceMaterial(ReviewModel):
    source_id: str
    media_type: Literal["pdf-text", "repository-readme", "web-content"]
    sha256: str
    text: str
    local_path: Optional[str] = None
    page_count: Optional[int] = Field(default=None, ge=1)
    selected_pages: Tuple[int, ...] = ()
    acquired_at: str

    @model_validator(mode="after")
    def _validate_material(self) -> "SourceMaterial":
        if not re.fullmatch(r"[a-f0-9]{64}", self.sha256):
            raise ValueError("material sha256 must be a lowercase SHA-256 digest")
        if not self.text.strip():
            raise ValueError("source material cannot be blank")
        return self


class SourceScreening(ReviewModel):
    source_id: str
    source_role: SourceRole = "background"
    label: Literal["core", "adjacent", "background", "exclude"]
    relevance_score: float = Field(ge=0, le=1)
    evidence_potential: float = Field(ge=0, le=1)
    engineering_value: float = Field(ge=0, le=1)
    counterevidence_value: float = Field(ge=0, le=1)
    reason: str = Field(min_length=3, max_length=1000)
    target_facets: Tuple[str, ...] = ()

    @property
    def ranking_score(self) -> float:
        return (
            self.relevance_score * 0.45
            + self.evidence_potential * 0.25
            + self.engineering_value * 0.15
            + self.counterevidence_value * 0.15
        )


class SourceScreeningBatch(ReviewModel):
    screenings: Tuple[SourceScreening, ...] = Field(min_length=1, max_length=16)


class SourceRelationHint(ReviewModel):
    subject: str
    relation: Literal[
        "alias-of",
        "variant-of",
        "extends",
        "implements",
        "evaluates",
        "supports",
        "opposes",
    ]
    object: str
    rationale: str


class SourceSkim(ReviewModel):
    source_id: str
    source_type: SourceType
    source_role: SourceRole = "background"
    label: Literal["core", "adjacent", "background", "exclude"]
    relevance_score: float = Field(ge=0, le=1)
    why_relevant: str
    method_families: Tuple[str, ...] = ()
    key_findings: Tuple[str, ...] = ()
    context_lengths: Tuple[str, ...] = ()
    important_locations: Tuple[str, ...] = ()
    limitations: Tuple[str, ...] = ()
    questions_raised: Tuple[str, ...] = ()
    relation_hints: Tuple[SourceRelationHint, ...] = ()
    target_facets: Tuple[str, ...] = ()
    select_for_deep_read: bool = False
    basis: Literal["metadata", "abstract", "source-excerpt"]
    provisional: Literal[True] = True
    citation_eligible: Literal[False] = False


class ResearchUncertainty(ReviewModel):
    uncertainty_id: str
    question: str
    category: Literal[
        "taxonomy",
        "performance",
        "engineering",
        "nonconsensus",
        "replication",
        "scope",
        "coverage",
        "topology",
        "freshness",
    ]
    priority: float = Field(ge=0, le=1)
    blocking: bool = False
    status: Literal["open", "resolved", "insufficient-evidence"] = "open"
    supporting_card_ids: Tuple[str, ...] = ()
    opposing_card_ids: Tuple[str, ...] = ()
    next_queries: Tuple[str, ...] = ()
    resolution: Optional[str] = None
    origin: Literal["semantic", "deterministic"] = "semantic"
    target_facets: Tuple[str, ...] = ()
    target_source_roles: Tuple[SourceRole, ...] = ()
    report_critical: bool = False


class SourceRelationCandidate(ReviewModel):
    relation_id: str
    subject: str
    relation: RelationKind
    object: str
    confidence: float = Field(ge=0, le=1)
    basis: str
    source_ids: Tuple[str, ...]
    status: Literal["candidate", "confirmed", "rejected"] = "candidate"
    provisional: Literal[True] = True
    citation_eligible: Literal[False] = False


class FacetCoverage(ReviewModel):
    facet: str
    status: FacetStatus
    independent_source_ids: Tuple[str, ...] = ()
    evidence_card_ids: Tuple[str, ...] = ()


class ReviewCoverageMatrix(ReviewModel):
    schema_version: Literal["0.1"] = "0.1"
    facets: Tuple[FacetCoverage, ...]
    source_role_counts: Dict[str, int] = Field(default_factory=dict)
    evidence_source_ids: Tuple[str, ...] = ()


class ReviewGap(ReviewModel):
    gap_id: str
    kind: ReviewGapKind
    question: str
    priority: float = Field(ge=0, le=1)
    blocking: bool = False
    report_critical: bool = False
    target_facets: Tuple[str, ...] = ()
    target_source_roles: Tuple[SourceRole, ...] = ()
    source_ids: Tuple[str, ...] = ()
    claim_ids: Tuple[str, ...] = ()
    next_queries: Tuple[str, ...] = ()
    status: Literal["open", "resolved"] = "open"


class UnderstandingClaim(ReviewModel):
    claim_id: str
    statement: str
    scope: Tuple[str, ...]
    confidence: float = Field(ge=0, le=1)
    supporting_card_ids: Tuple[str, ...] = ()
    opposing_card_ids: Tuple[str, ...] = ()
    alternative_explanations: Tuple[str, ...] = ()
    status: Literal["provisional", "supported", "contested", "insufficient"]

    @model_validator(mode="after")
    def _validate_status(self) -> "UnderstandingClaim":
        if not self.scope:
            raise ValueError("understanding claim scope cannot be empty")
        if self.status in {"supported", "contested"} and not (
            self.supporting_card_ids or self.opposing_card_ids
        ):
            raise ValueError("evidence-backed claim status requires an EvidenceCard")
        return self


class EvidenceLocator(ReviewModel):
    kind: Literal[
        "pdf-page",
        "table",
        "figure",
        "section",
        "url",
        "repository",
    ]
    value: str
    detail: Optional[str] = None

    @field_validator("value")
    @classmethod
    def _nonempty_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence locator value cannot be blank")
        return value.strip()


class EvidenceCard(ReviewModel):
    card_id: str
    source_id: str
    source_url: str
    source_version: str
    source_sha256: str
    statement: str
    attribution: Literal["author", "agent-analysis", "repository-metadata"]
    evidence_type: Literal[
        "experiment",
        "author-discussion",
        "project-metadata",
        "implementation",
        "documentation",
    ]
    status: EvidenceStatus
    method: Optional[str] = None
    model: Optional[str] = None
    benchmark: Optional[str] = None
    task: Optional[str] = None
    context_length: Optional[str] = None
    metric: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    conditions: Dict[str, str] = Field(default_factory=dict)
    locator: EvidenceLocator
    supports_claim_ids: Tuple[str, ...] = ()
    opposes_claim_ids: Tuple[str, ...] = ()
    target_facets: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_evidence(self) -> "EvidenceCard":
        if not re.fullmatch(r"[a-f0-9]{64}", self.source_sha256):
            raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
        if not self.source_url.strip() or not self.source_version.strip():
            raise ValueError("evidence source URL and version cannot be blank")
        if not re.match(r"^https?://", self.source_url, re.IGNORECASE):
            raise ValueError("evidence source URL must be absolute HTTP(S)")
        if not self.statement.strip():
            raise ValueError("evidence statement cannot be blank")
        if self.metric and self.value is None:
            raise ValueError("metric evidence requires a value")
        if self.value is not None and not self.conditions:
            raise ValueError("quantitative evidence requires experimental conditions")
        return self


class EvidenceExtraction(ReviewModel):
    source_id: str
    cards: Tuple[EvidenceCard, ...] = Field(default=(), max_length=8)
    source_limitations: Tuple[str, ...] = ()
    unresolved_questions: Tuple[str, ...] = ()


class NonConsensusAssessment(ReviewModel):
    assessment_id: str
    question: str
    result: AssessmentResult
    comparable: bool
    independent_source_ids: Tuple[str, ...]
    supporting_card_ids: Tuple[str, ...] = ()
    opposing_card_ids: Tuple[str, ...] = ()
    rationale: str

    @model_validator(mode="after")
    def _validate_independence(self) -> "NonConsensusAssessment":
        if len(set(self.independent_source_ids)) != len(self.independent_source_ids):
            raise ValueError("independent_source_ids cannot contain duplicates")
        if self.result in {"supported-consensus", "contested"}:
            if not self.comparable or len(self.independent_source_ids) < 2:
                raise ValueError(
                    "consensus or contested assessments require two comparable independent sources"
                )
        return self


class ReasoningUpdate(ReviewModel):
    summary: str
    claims: Tuple[UnderstandingClaim, ...] = ()
    uncertainties: Tuple[ResearchUncertainty, ...] = ()
    assessments: Tuple[NonConsensusAssessment, ...] = ()
    new_method_families: Tuple[str, ...] = ()
    resolved_uncertainty_ids: Tuple[str, ...] = ()
    found_independent_counterevidence: bool = False


class ReviewReadiness(ReviewModel):
    facet_statuses: Dict[str, FacetStatus]
    citation_ready_cards: int = Field(ge=0)
    evidenced_claims: int = Field(ge=0)
    independent_sources: int = Field(ge=0)
    unresolved_blocking_ids: Tuple[str, ...] = ()
    nonconsensus_review_complete: bool
    saturated: bool
    ready: bool
    reasons: Tuple[str, ...] = ()


class SynthesisStatement(ReviewModel):
    statement_id: str
    statement: str
    evidence_card_ids: Tuple[str, ...]
    claim_kind: Literal[
        "single-source-observation",
        "comparison",
        "consensus",
        "contradiction",
    ] = "single-source-observation"
    scope: Tuple[str, ...] = ()
    confidence: Literal["high", "medium", "low"] = "medium"
    limitation: Optional[str] = None

    @model_validator(mode="after")
    def _require_evidence(self) -> "SynthesisStatement":
        if not self.evidence_card_ids:
            raise ValueError("a synthesis statement requires at least one EvidenceCard")
        return self


class TaxonomyEntry(ReviewModel):
    name: str
    definition: str
    parent: Optional[str] = None
    evidence_card_ids: Tuple[str, ...]

    @model_validator(mode="after")
    def _require_evidence(self) -> "TaxonomyEntry":
        if not self.evidence_card_ids:
            raise ValueError("a taxonomy entry requires EvidenceCard support")
        return self


class ProjectFinding(ReviewModel):
    name: str
    repository_url: str
    maturity: Literal["prototype", "research-code", "maintained", "unknown"]
    statement: str
    evidence_card_ids: Tuple[str, ...]

    @model_validator(mode="after")
    def _require_evidence(self) -> "ProjectFinding":
        if not self.evidence_card_ids:
            raise ValueError("a project finding requires EvidenceCard support")
        return self


class ReviewSynthesisDraft(ReviewModel):
    title: str
    scope_summary: str
    core_findings: Tuple[SynthesisStatement, ...] = ()
    taxonomy: Tuple[TaxonomyEntry, ...] = ()
    task_and_performance: Tuple[SynthesisStatement, ...] = ()
    engineering_bottlenecks: Tuple[SynthesisStatement, ...] = ()
    projects: Tuple[ProjectFinding, ...] = ()
    open_questions: Tuple[str, ...] = ()
    limitations: Tuple[str, ...] = ()


class PromotionItem(ReviewModel):
    source_id: str
    evidence_card_ids: Tuple[str, ...]
    rationale: str
    approved: bool = False
    status: Literal["suggested", "approved", "staged", "failed"] = "suggested"
    stage_id: Optional[str] = None
    error: Optional[str] = None

    @model_validator(mode="after")
    def _validate_status(self) -> "PromotionItem":
        if self.status == "staged" and not self.stage_id:
            raise ValueError("a staged promotion item requires stage_id")
        return self


class PromotionManifest(ReviewModel):
    schema_version: Literal["0.1"] = "0.1"
    research_id: str
    run_id: str
    max_promotions: int = Field(ge=0, le=50)
    items: Tuple[PromotionItem, ...]
    created_at: str

    @model_validator(mode="after")
    def _validate_manifest(self) -> "PromotionManifest":
        if len(self.items) > self.max_promotions:
            raise ValueError("promotion manifest exceeds max_promotions")
        ids = [item.source_id for item in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("promotion manifest cannot repeat a source")
        return self


class TrajectoryEvent(ReviewModel):
    sequence: int = Field(ge=1)
    timestamp: str
    stage: ReviewStage
    question: str
    action: str
    evidence_gained: Tuple[str, ...] = ()
    understanding_change: Optional[str] = None
    next_pivot: Optional[str] = None
    stop_reason: Optional[str] = None


class ReviewErrorEvent(ReviewModel):
    schema_version: Literal["0.1"] = "0.1"
    run_id: str
    research_id: str
    stage: ReviewStage
    recurrence_key: str
    observed: str
    source_id: Optional[str] = None
    artifact_id: Optional[str] = None
    timestamp: str


class ErrorBookSummary(ReviewModel):
    recurrence_key: str
    distinct_run_ids: Tuple[str, ...]
    occurrence_count: int = Field(ge=2)
    affected_stages: Tuple[ReviewStage, ...]
    observed_examples: Tuple[str, ...]
    proposed_change: str


def validate_synthesis_references(
    draft: ReviewSynthesisDraft,
    cards: Mapping[str, EvidenceCard],
) -> None:
    """Reject unknown EvidenceCard references before Markdown rendering."""

    referenced = []
    for item in (
        *draft.core_findings,
        *draft.task_and_performance,
        *draft.engineering_bottlenecks,
    ):
        referenced.extend(item.evidence_card_ids)
    for item in draft.taxonomy:
        referenced.extend(item.evidence_card_ids)
    for item in draft.projects:
        referenced.extend(item.evidence_card_ids)
    unknown = sorted(set(referenced) - set(cards))
    if unknown:
        raise ValueError(
            "synthesis references unknown EvidenceCards: " + ", ".join(unknown)
        )
    for statement in (
        *draft.core_findings,
        *draft.task_and_performance,
        *draft.engineering_bottlenecks,
    ):
        if statement.claim_kind == "single-source-observation":
            continue
        source_ids = {
            cards[card_id].source_id for card_id in statement.evidence_card_ids
        }
        if len(source_ids) < 2:
            raise ValueError(
                f"{statement.claim_kind} statement {statement.statement_id} "
                "requires two independent sources"
            )


__all__ = [
    "AssessmentResult",
    "DiscoveryRecord",
    "ErrorBookSummary",
    "EvidenceCard",
    "EvidenceExtraction",
    "EvidenceLocator",
    "NonConsensusAssessment",
    "ProjectFinding",
    "PromotionItem",
    "PromotionManifest",
    "QueryPlan",
    "ReasoningUpdate",
    "ResearchUncertainty",
    "RetrievalQuery",
    "ReviewCoverageMatrix",
    "ReviewErrorEvent",
    "ReviewGap",
    "ReviewReadiness",
    "ReviewRunConfig",
    "ReviewScope",
    "ReviewSynthesisDraft",
    "SourceRecord",
    "SourceRelationCandidate",
    "SourceRelationHint",
    "SourceRole",
    "SourceMaterial",
    "SourceScreening",
    "SourceScreeningBatch",
    "SourceSkim",
    "SourceType",
    "FacetCoverage",
    "SynthesisStatement",
    "TaxonomyEntry",
    "TrajectoryEvent",
    "UnderstandingClaim",
    "validate_synthesis_references",
]
