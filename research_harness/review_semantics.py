"""Skill-bound semantic operations and deterministic review rendering."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any, Mapping, Optional, Protocol, Sequence, Type, TypeVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from .model_client import ReviewModelBundle, create_profile_chat_model
from .review_logic import (
    sanitize_provisional_skim,
    source_authority,
    source_evidence_eligible,
    stable_id,
    validate_nonconsensus_assessment,
)
from .review_models import (
    EvidenceCard,
    EvidenceExtraction,
    NonConsensusAssessment,
    PromotionItem,
    PromotionManifest,
    QueryPlan,
    ReasoningUpdate,
    ResearchUncertainty,
    RetrievalQuery,
    ReviewReadiness,
    ReviewRunConfig,
    ReviewScope,
    ReviewSynthesisDraft,
    SourceMaterial,
    SourceRecord,
    SourceScreeningBatch,
    SourceSkim,
    SynthesisStatement,
    UnderstandingClaim,
    validate_synthesis_references,
)
from .skill_registry import SkillRegistry, SkillSpec


T = TypeVar("T", bound=BaseModel)
MATERIAL_CARD_LIMITS = {
    "pdf-text": 20,
    "repository-readme": 8,
    "web-content": 5,
}


def _locator_occurs_in_material(value: str, material: SourceMaterial) -> bool:
    needle = " ".join(value.casefold().split())
    haystack = " ".join(material.text.casefold().split())
    return bool(needle and needle in haystack)


def _web_card_supported(card: EvidenceCard, material: SourceMaterial) -> bool:
    if material.media_type != "web-content":
        return True
    locator = card.locator
    return not (
        card.evidence_type == "experiment"
        or card.value is not None
        or locator.kind not in {"section", "url"}
        or (
            locator.kind == "section"
            and not _locator_occurs_in_material(locator.value, material)
        )
    )


class ReviewSemanticEngine(Protocol):
    requires_network: bool
    model_fingerprint: str

    def plan_queries(
        self,
        *,
        scope: ReviewScope,
        config: ReviewRunConfig,
        round_number: int,
        uncertainties: Sequence[ResearchUncertainty],
        prior_queries: Sequence[RetrievalQuery],
        enabled_providers: Sequence[str],
    ) -> QueryPlan: ...

    def screen_batch(
        self,
        *,
        scope: ReviewScope,
        sources: Sequence[SourceRecord],
    ) -> SourceScreeningBatch: ...

    def skim_source(
        self,
        *,
        scope: ReviewScope,
        source: SourceRecord,
    ) -> SourceSkim: ...

    def reason(
        self,
        *,
        scope: ReviewScope,
        skims: Sequence[SourceSkim],
        cards: Sequence[EvidenceCard],
        claims: Sequence[UnderstandingClaim],
        uncertainties: Sequence[ResearchUncertainty],
    ) -> ReasoningUpdate: ...

    def extract_evidence(
        self,
        *,
        scope: ReviewScope,
        source: SourceRecord,
        material: SourceMaterial,
        claims: Sequence[UnderstandingClaim],
    ) -> EvidenceExtraction: ...

    def synthesize(
        self,
        *,
        scope: ReviewScope,
        config: ReviewRunConfig,
        sources: Sequence[SourceRecord],
        cards: Sequence[EvidenceCard],
        claims: Sequence[UnderstandingClaim],
        uncertainties: Sequence[ResearchUncertainty],
        assessments: Sequence[NonConsensusAssessment],
        readiness: ReviewReadiness,
    ) -> ReviewSynthesisDraft: ...


def _payload(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_payload(item) for item in value]
    return value


def _invoke_structured(
    model: BaseChatModel,
    schema: Type[T],
    *,
    system: str,
    payload: Mapping[str, Any],
) -> T:
    structured = model.with_structured_output(schema, method="json_mode")
    result = structured.invoke(
        [
            SystemMessage(content=system),
            HumanMessage(
                content=json.dumps(
                    {**payload, "output_json_schema": schema.model_json_schema()},
                    ensure_ascii=False,
                    indent=2,
                )
            ),
        ]
    )
    return result if isinstance(result, schema) else schema.model_validate(result)


def _skill_system(skill: SkillSpec, task: str) -> str:
    return (
        f"You execute the repository-local {skill.name} Skill. {task} "
        "Return only the requested structured object. Do not invent papers, "
        "measurements, source locations, repository facts, or cross-paper agreement.\n\n"
        f"SKILL INSTRUCTIONS\n{skill.instructions}"
    )


class LangChainReviewSemanticEngine:
    requires_network = True

    def __init__(self, bundle: ReviewModelBundle, skills_root):
        self.bundle = bundle
        self.fast_model = create_profile_chat_model(bundle.fast)
        self.reasoning_model = create_profile_chat_model(bundle.reasoning)
        self.registry = SkillRegistry(skills_root)
        self.model_fingerprint = bundle.fingerprint

    def plan_queries(
        self,
        *,
        scope: ReviewScope,
        config: ReviewRunConfig,
        round_number: int,
        uncertainties: Sequence[ResearchUncertainty],
        prior_queries: Sequence[RetrievalQuery],
        enabled_providers: Sequence[str],
    ) -> QueryPlan:
        skill = self.registry.get("search-paper")
        remaining = max(1, config.max_queries - len(prior_queries))
        draft = _invoke_structured(
            self.fast_model,
            QueryPlan,
            system=_skill_system(
                skill,
                "Plan a small multi-source query matrix around the highest-priority "
                "unresolved scientific uncertainty.",
            ),
            payload={
                "task": "Plan the next review retrieval round.",
                "scope": _payload(scope),
                "round": round_number,
                "open_uncertainties": _payload(
                    sorted(
                        (item for item in uncertainties if item.status == "open"),
                        key=lambda item: (-item.priority, item.uncertainty_id),
                    )[:8]
                ),
                "prior_queries": _payload(prior_queries),
                "enabled_providers": list(enabled_providers),
                "constraints": {
                    "maximum_queries": min(6, remaining),
                    "write_search_queries_in_English": True,
                    "include_at_least_one_disconfirming_query_after_round_one": True,
                    "providers": list(enabled_providers),
                },
            },
        )
        normalized = []
        allowed_facets = set(scope.required_facets)
        for position, item in enumerate(draft.queries[:remaining], start=1):
            if item.provider not in enabled_providers:
                continue
            facets = tuple(value for value in item.target_facets if value in allowed_facets)
            if not facets:
                facets = scope.required_facets[:2]
            normalized.append(
                item.model_copy(
                    update={
                        "id": f"R{round_number:02d}Q{position:02d}",
                        "round": round_number,
                        "target_facets": facets,
                    }
                )
            )
        if round_number == 1:
            provider_counts = Counter(item.provider for item in normalized)
            base_query = scope.seed_queries[0] if scope.seed_queries else scope.question
            suffixes = {
                "deepxiv": "research paper",
                "github": "GitHub repository implementation",
                "tavily": "official documentation counter evidence",
            }
            for provider in enabled_providers:
                if provider_counts[provider] > 0:
                    continue
                fallback = RetrievalQuery(
                    id="pending",
                    round=round_number,
                    provider=provider,
                    text=f"{base_query} {suffixes[provider]}",
                    purpose=(
                        "Ensure first-round multi-source coverage before "
                        "uncertainty-driven provider reallocation."
                    ),
                    target_facets=scope.required_facets[:2],
                )
                if len(normalized) < min(8, remaining):
                    normalized.append(fallback)
                else:
                    replace_at = next(
                        (
                            index
                            for index in range(len(normalized) - 1, -1, -1)
                            if provider_counts[normalized[index].provider] > 1
                        ),
                        len(normalized) - 1,
                    )
                    removed = normalized[replace_at].provider
                    provider_counts[removed] -= 1
                    normalized[replace_at] = fallback
                provider_counts[provider] += 1
        elif normalized and not any(item.disconfirming for item in normalized):
            first = normalized[0]
            normalized[0] = first.model_copy(
                update={
                    "text": first.text + " limitations failure counter evidence",
                    "purpose": (
                        "Actively search for independent limitations, failed settings, "
                        "or evidence against the leading provisional claim."
                    ),
                    "disconfirming": True,
                }
            )
        normalized = [
            item.model_copy(update={"id": f"R{round_number:02d}Q{position:02d}"})
            for position, item in enumerate(normalized, start=1)
        ]
        if not normalized:
            raise ValueError("query planner returned no enabled, in-scope query")
        return QueryPlan(rationale=draft.rationale, queries=tuple(normalized))

    def screen_batch(
        self,
        *,
        scope: ReviewScope,
        sources: Sequence[SourceRecord],
    ) -> SourceScreeningBatch:
        skill = self.registry.get("source-skim")
        result = _invoke_structured(
            self.fast_model,
            SourceScreeningBatch,
            system=_skill_system(
                skill,
                "Screen metadata for relevance and evidence potential. This is not "
                "full-paper evidence verification.",
            ),
            payload={
                "task": "Screen every supplied source exactly once.",
                "scope": _payload(scope),
                "sources": [
                    {
                        "source_id": item.source_id,
                        "source_type": item.source_type,
                        "canonical_url": item.canonical_url,
                        "source_authority": source_authority(item),
                        "title": item.title,
                        "abstract": item.abstract,
                        "snippet": item.snippet,
                        "repository": item.repository,
                        "target_facets": item.target_facets,
                    }
                    for item in sources
                ],
                "constraints": {
                    "return_exact_source_ids": True,
                    "do_not_infer_results": True,
                },
            },
        )
        expected = {item.source_id for item in sources}
        returned = {item.source_id for item in result.screenings}
        if expected != returned or len(returned) != len(result.screenings):
            raise ValueError(
                "source screener must return every requested source exactly once"
            )
        return result

    def skim_source(
        self,
        *,
        scope: ReviewScope,
        source: SourceRecord,
    ) -> SourceSkim:
        skill_name = "project-audit" if source.source_type == "project" else "source-skim"
        skill = self.registry.get(skill_name)
        result = _invoke_structured(
            self.fast_model,
            SourceSkim,
            system=_skill_system(
                skill,
                "Produce a compact provisional skim. Findings from this step are never "
                "citation-eligible.",
            ),
            payload={
                "task": "Skim one source for review navigation.",
                "scope": _payload(scope),
                "source": {
                    "source_id": source.source_id,
                    "source_type": source.source_type,
                    "canonical_url": source.canonical_url,
                    "source_authority": source_authority(source),
                    "title": source.title,
                    "authors": source.authors,
                    "year": source.year,
                    "abstract": source.abstract,
                    "snippet": source.snippet,
                    "content_preview": (
                        source.content_preview[:4_000]
                        if source.content_preview
                        else None
                    ),
                    "repository": source.repository,
                    "license": source.license,
                    "stars": source.stars,
                    "target_facets": source.target_facets,
                },
                "constraints": {
                    "source_id": source.source_id,
                    "source_type": source.source_type,
                    "provisional": True,
                    "citation_eligible": False,
                },
            },
        )
        safe_findings, safe_questions = sanitize_provisional_skim(
            result.key_findings,
            result.questions_raised,
        )
        return result.model_copy(
            update={
                "source_id": source.source_id,
                "source_type": source.source_type,
                "provisional": True,
                "citation_eligible": False,
                "select_for_deep_read": (
                    result.select_for_deep_read
                    and source_evidence_eligible(source)
                ),
                "key_findings": safe_findings,
                "questions_raised": safe_questions,
                "basis": (
                    "source-excerpt"
                    if source.content_preview
                    else "abstract"
                    if source.abstract
                    else "metadata"
                ),
                "target_facets": tuple(
                    value for value in result.target_facets if value in scope.required_facets
                ),
            }
        )

    def reason(
        self,
        *,
        scope: ReviewScope,
        skims: Sequence[SourceSkim],
        cards: Sequence[EvidenceCard],
        claims: Sequence[UnderstandingClaim],
        uncertainties: Sequence[ResearchUncertainty],
    ) -> ReasoningUpdate:
        skill = self.registry.get("review-synthesize")
        result = _invoke_structured(
            self.reasoning_model,
            ReasoningUpdate,
            system=_skill_system(
                skill,
                "Update the current understanding and prioritize uncertainties. Treat "
                "SourceSkim as provisional navigation only and EvidenceCard as the only "
                "citation-ready evidence.",
            ),
            payload={
                "task": "Update the review's current understanding.",
                "scope": _payload(scope),
                "existing_claims": _payload(claims),
                "existing_uncertainties": _payload(uncertainties),
                "source_skims": _payload(skims),
                "evidence_cards": _payload(cards),
                "constraints": {
                    "reuse_existing_ids": True,
                    "new_claim_id_prefix": "understanding-claim-",
                    "new_uncertainty_id_prefix": "uncertainty-",
                    "same_paper_configurations_are_not_independent_sources": True,
                    "unsupported_disagreement_result": "insufficient-evidence",
                },
            },
        )
        known_cards = {item.card_id: item for item in cards}
        for assessment in result.assessments:
            validate_nonconsensus_assessment(assessment, known_cards)
        return result

    def extract_evidence(
        self,
        *,
        scope: ReviewScope,
        source: SourceRecord,
        material: SourceMaterial,
        claims: Sequence[UnderstandingClaim],
    ) -> EvidenceExtraction:
        if not source_evidence_eligible(source):
            raise ValueError(
                "citation-ready evidence requires a primary paper, repository, "
                "or deterministically recognized official Web source"
            )
        skill_name = "project-audit" if source.source_type == "project" else "evidence-extract"
        skill = self.registry.get(skill_name)
        maximum_cards = MATERIAL_CARD_LIMITS[material.media_type]
        result = _invoke_structured(
            self.reasoning_model,
            EvidenceExtraction,
            system=_skill_system(
                skill,
                "Extract only atomic, located evidence relevant to the review. Omit any "
                "claim whose location or material conditions are unavailable.",
            ),
            payload={
                "task": "Extract citation-ready EvidenceCards from one deep-read source.",
                "scope": _payload(scope),
                "source": _payload(source),
                "material": {
                    "source_id": material.source_id,
                    "media_type": material.media_type,
                    "sha256": material.sha256,
                    "local_path": material.local_path,
                    "selected_pages": material.selected_pages,
                    "text": material.text[:60_000],
                },
                "current_understanding_claims": _payload(claims),
                "constraints": {
                    "source_id": source.source_id,
                    "source_url": source.canonical_url,
                    "source_version": (
                        source.version
                        or source.updated_at
                        or f"captured:{material.acquired_at}"
                    ),
                    "source_sha256": material.sha256,
                    "one_material_result_per_card": True,
                    "numeric_results_require_conditions": True,
                    "locator_required": True,
                    "maximum_cards": maximum_cards,
                },
            },
        )
        if result.source_id != source.source_id:
            raise ValueError("evidence extractor returned the wrong source_id")
        known_claims = {item.claim_id for item in claims}
        cards = []
        omitted_cards = 0
        for item in result.cards:
            locator = item.locator
            if material.media_type == "web-content":
                # Static page extraction cannot establish table/figure results,
                # and page-level author summaries are not experimental evidence.
                if not _web_card_supported(item, material):
                    omitted_cards += 1
                    continue
            if locator.kind == "pdf-page":
                if material.media_type != "pdf-text":
                    raise ValueError("pdf-page locator returned for a non-PDF source")
                match = re.search(r"\d+", locator.value)
                if not match:
                    raise ValueError("pdf-page locator must contain a page number")
                page = int(match.group(0))
                if material.page_count and page > material.page_count:
                    raise ValueError("evidence locator exceeds the source PDF page count")
                if material.selected_pages and page not in material.selected_pages:
                    raise ValueError("evidence locator page was not present in the excerpt")
            elif locator.kind == "url":
                locator = locator.model_copy(update={"value": source.canonical_url})
            elif locator.kind == "repository":
                if not source.repository:
                    raise ValueError("repository locator returned for a non-project source")
                locator = locator.model_copy(update={"value": source.repository})
            card_id = stable_id(
                "evidence-card",
                source.source_id,
                item.statement,
                locator.kind,
                locator.value,
            )
            cards.append(
                item.model_copy(
                    update={
                        "card_id": card_id,
                        "source_id": source.source_id,
                        "source_url": source.canonical_url,
                        "source_version": (
                            source.version
                            or source.updated_at
                            or f"captured:{material.acquired_at}"
                        ),
                        "source_sha256": material.sha256,
                        # Extraction establishes a locator, not independent
                        # verification. Later deterministic or human checks may
                        # promote the status.
                        "status": "located",
                        "locator": locator,
                        "supports_claim_ids": tuple(
                            value
                            for value in item.supports_claim_ids
                            if value in known_claims
                        ),
                        "opposes_claim_ids": tuple(
                            value
                            for value in item.opposes_claim_ids
                            if value in known_claims
                        ),
                        "target_facets": tuple(
                            value
                            for value in item.target_facets
                            if value in scope.required_facets
                        ),
                    }
                )
            )
        if len(cards) > maximum_cards:
            omitted_cards += len(cards) - maximum_cards
            cards = cards[:maximum_cards]
        limitations = list(result.source_limitations)
        if omitted_cards:
            limitations.append(
                f"Deterministic evidence guards omitted {omitted_cards} cards "
                "that lacked an eligible material/locator combination."
            )
        return EvidenceExtraction(
            source_id=source.source_id,
            cards=tuple(cards),
            source_limitations=tuple(dict.fromkeys(limitations)),
            unresolved_questions=result.unresolved_questions,
        )

    def synthesize(
        self,
        *,
        scope: ReviewScope,
        config: ReviewRunConfig,
        sources: Sequence[SourceRecord],
        cards: Sequence[EvidenceCard],
        claims: Sequence[UnderstandingClaim],
        uncertainties: Sequence[ResearchUncertainty],
        assessments: Sequence[NonConsensusAssessment],
        readiness: ReviewReadiness,
    ) -> ReviewSynthesisDraft:
        skill = self.registry.get("review-synthesize")
        draft = _invoke_structured(
            self.reasoning_model,
            ReviewSynthesisDraft,
            system=_skill_system(
                skill,
                "Build a structured scientific review draft. Every factual taxonomy, "
                "performance, engineering, and project statement must cite supplied "
                "EvidenceCard IDs.",
            ),
            payload={
                "task": "Produce the structured review synthesis.",
                "scope": _payload(scope),
                "run_config": _payload(config),
                "readiness": _payload(readiness),
                "sources": [
                    {
                        "source_id": item.source_id,
                        "source_type": item.source_type,
                        "title": item.title,
                        "canonical_url": item.canonical_url,
                        "year": item.year,
                        "venue": item.venue,
                        "repository": item.repository,
                        "license": item.license,
                        "stars": item.stars,
                        "version": item.version,
                        "content_sha256": item.content_sha256,
                    }
                    for item in sources
                ],
                "evidence_cards": _payload(cards),
                "understanding_claims": _payload(claims),
                "nonconsensus_assessments": _payload(assessments),
                "open_uncertainties": _payload(
                    [item for item in uncertainties if item.status != "resolved"]
                ),
                "constraints": {
                    "allowed_evidence_card_ids": [item.card_id for item in cards],
                    "comparison_consensus_or_contradiction_requires_two_independent_sources": True,
                    "single_source_claim_kind": "single-source-observation",
                    "do_not_write_markdown": True,
                },
            },
        )
        normalized = _downgrade_unsupported_synthesis(draft, cards)
        validate_synthesis_references(
            normalized, {item.card_id: item for item in cards}
        )
        return normalized


def _downgrade_statement(
    item: SynthesisStatement,
    cards: Mapping[str, EvidenceCard],
) -> Optional[SynthesisStatement]:
    known_ids = tuple(card_id for card_id in item.evidence_card_ids if card_id in cards)
    if not known_ids:
        return None
    source_ids = {cards[card_id].source_id for card_id in known_ids}
    update: dict[str, Any] = {"evidence_card_ids": known_ids}
    if item.claim_kind != "single-source-observation" and len(source_ids) < 2:
        limitation = "仅有一个独立来源，已降级为单篇观察，不能视为共识或矛盾。"
        update.update(
            {
                "claim_kind": "single-source-observation",
                "confidence": "low",
                "limitation": (
                    f"{item.limitation} {limitation}".strip()
                    if item.limitation
                    else limitation
                ),
            }
        )
    return item.model_copy(update=update)


def _downgrade_unsupported_synthesis(
    draft: ReviewSynthesisDraft,
    cards: Sequence[EvidenceCard],
) -> ReviewSynthesisDraft:
    by_id = {item.card_id: item for item in cards}

    def statements(values: Sequence[SynthesisStatement]) -> tuple[SynthesisStatement, ...]:
        return tuple(
            normalized
            for value in values
            if (normalized := _downgrade_statement(value, by_id)) is not None
        )

    taxonomy = tuple(
        item.model_copy(
            update={
                "evidence_card_ids": tuple(
                    card_id for card_id in item.evidence_card_ids if card_id in by_id
                )
            }
        )
        for item in draft.taxonomy
        if any(card_id in by_id for card_id in item.evidence_card_ids)
    )
    projects = tuple(
        item.model_copy(
            update={
                "evidence_card_ids": tuple(
                    card_id for card_id in item.evidence_card_ids if card_id in by_id
                )
            }
        )
        for item in draft.projects
        if any(card_id in by_id for card_id in item.evidence_card_ids)
    )
    return draft.model_copy(
        update={
            "core_findings": statements(draft.core_findings),
            "taxonomy": taxonomy,
            "task_and_performance": statements(draft.task_and_performance),
            "engineering_bottlenecks": statements(draft.engineering_bottlenecks),
            "projects": projects,
        }
    )


def _citation_suffix(card_ids: Sequence[str], card_numbers: Mapping[str, int]) -> str:
    links = [f"[E{card_numbers[item]}](#e{card_numbers[item]})" for item in card_ids]
    return " " + " ".join(links) if links else ""


def render_review_markdown(
    *,
    scope: ReviewScope,
    config: ReviewRunConfig,
    draft: ReviewSynthesisDraft,
    sources: Sequence[SourceRecord],
    cards: Sequence[EvidenceCard],
    uncertainties: Sequence[ResearchUncertainty],
    assessments: Sequence[NonConsensusAssessment],
    readiness: ReviewReadiness,
    trajectory_path: str,
) -> str:
    """Render a review without letting the language model write source files."""

    by_source = {item.source_id: item for item in sources}
    ordered_cards = sorted(cards, key=lambda item: item.card_id)
    card_numbers = {item.card_id: index for index, item in enumerate(ordered_cards, 1)}

    def statement_lines(values: Sequence[SynthesisStatement]) -> list[str]:
        lines = []
        for item in values:
            suffix = _citation_suffix(item.evidence_card_ids, card_numbers)
            label = {
                "single-source-observation": "单篇观察",
                "comparison": "跨来源比较",
                "consensus": "共识判断",
                "contradiction": "非共识判断",
            }[item.claim_kind]
            lines.append(f"- {item.statement}{suffix}  ")
            lines.append(f"  范围：{'；'.join(item.scope) or '见证据卡'}；证据级别：{label}/{item.confidence}。")
            if item.limitation:
                lines.append(f"  限制：{item.limitation}")
        return lines or ["- 当前证据不足，未形成可引用结论。"]

    lines = [
        "---",
        f"research_id: {config.research_id}",
        f"run_id: {config.run_id}",
        f"profile: {config.profile}",
        f"status: {'ready' if readiness.ready else 'bounded-with-open-gaps'}",
        "---",
        "",
        f"# {draft.title or scope.title}",
        "",
        "> 本报告由 Fast Research Loop 生成。Skim 仅用于筛选；正文事实只引用 EvidenceCard。",
        "",
        "## 1. 核心结论",
        "",
        *statement_lines(draft.core_findings),
        "",
        "## 2. 调研范围与方法",
        "",
        draft.scope_summary or scope.question,
        "",
        f"- 候选来源：{len(sources)}",
        f"- 可引用 EvidenceCard：{len(cards)}",
        f"- 独立证据来源：{readiness.independent_sources}",
        f"- 调研状态：{'具备综合条件' if readiness.ready else '达到预算后有界综合'}",
        f"- 精简 trajectory：`{trajectory_path}`",
        "",
        "## 3. 技术谱系",
        "",
    ]
    if draft.taxonomy:
        for item in draft.taxonomy:
            suffix = _citation_suffix(item.evidence_card_ids, card_numbers)
            parent = f"（上位路线：{item.parent}）" if item.parent else ""
            lines.append(f"- **{item.name}**{parent}：{item.definition}{suffix}")
    else:
        lines.append("- 当前可定位证据不足，技术谱系仍为待验证草案。")
    lines.extend(["", "## 4. 长文任务、Benchmark 与性能", ""])
    lines.extend(statement_lines(draft.task_and_performance))
    lines.extend(["", "## 5. 工程实现与瓶颈", ""])
    lines.extend(statement_lines(draft.engineering_bottlenecks))
    lines.extend(["", "## 6. 代表性开源项目", ""])
    if draft.projects:
        lines.extend(
            f"- **[{item.name}]({item.repository_url})**（{item.maturity}）："
            f"{item.statement}{_citation_suffix(item.evidence_card_ids, card_numbers)}"
            for item in draft.projects
        )
    else:
        lines.append("- 尚无经过定位的项目实现证据。")
    lines.extend(["", "## 7. 非共识结论与边界", ""])
    if assessments:
        for item in assessments:
            cards_for_item = (*item.supporting_card_ids, *item.opposing_card_ids)
            result_label = {
                "supported-consensus": "支持共识",
                "contested": "存在争议",
                "insufficient-evidence": "证据不足",
            }[item.result]
            lines.append(
                f"- **{result_label}**：{item.question}——{item.rationale}"
                f"{_citation_suffix(cards_for_item, card_numbers)}"
            )
    else:
        lines.append("- 尚未形成满足独立来源条件的非共识判断。")
    lines.extend(["", "## 8. 未解决问题", ""])
    open_questions = list(draft.open_questions)
    open_questions.extend(
        item.question for item in uncertainties if item.status != "resolved"
    )
    for question in dict.fromkeys(open_questions):
        lines.append(f"- {question}")
    if not open_questions:
        lines.append("- 当前没有记录到开放问题。")
    lines.extend(["", "## 9. 调研局限", ""])
    limitations = [*draft.limitations, *readiness.reasons]
    for item in dict.fromkeys(limitations):
        lines.append(f"- {item}")
    if not limitations:
        lines.append("- 未记录额外局限。")
    lines.extend(["", "## 10. 证据索引", ""])
    for card in ordered_cards:
        number = card_numbers[card.card_id]
        source = by_source.get(card.source_id)
        source_label = source.title if source else card.source_id
        source_url = source.canonical_url if source else ""
        locator = card.locator.value
        if card.locator.detail:
            locator += f"；{card.locator.detail}"
        lines.extend(
            [
                f"<a id=\"e{number}\"></a>",
                f"- **E{number} · {card.status}**：{card.statement}",
                f"  来源：[{source_label}]({source_url})；版本：{card.source_version}；"
                f"SHA-256：`{card.source_sha256}`；定位：{locator}；卡片：`{card.card_id}`。",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def build_technology_map(
    *,
    skims: Sequence[SourceSkim],
    cards: Sequence[EvidenceCard],
) -> dict[str, Any]:
    families: dict[str, set[str]] = defaultdict(set)
    for skim in skims:
        for family in skim.method_families:
            families[family].add(skim.source_id)
    facets: dict[str, set[str]] = defaultdict(set)
    evidenced_methods: dict[str, set[str]] = defaultdict(set)
    for card in cards:
        for facet in card.target_facets:
            facets[facet].add(card.card_id)
        if card.method:
            evidenced_methods[card.method].add(card.card_id)
    return {
        "schema_version": "0.1",
        "method_families": [
            {
                "name": name,
                "source_ids": sorted(source_ids),
                "provisional": True,
                "citation_eligible": False,
            }
            for name, source_ids in sorted(families.items())
        ],
        "evidenced_methods": [
            {"name": name, "evidence_card_ids": sorted(card_ids)}
            for name, card_ids in sorted(evidenced_methods.items())
        ],
        "evidence_facets": [
            {"name": name, "evidence_card_ids": sorted(card_ids)}
            for name, card_ids in sorted(facets.items())
        ],
    }


def build_promotion_manifest(
    *,
    config: ReviewRunConfig,
    sources: Sequence[SourceRecord],
    cards: Sequence[EvidenceCard],
    created_at: str,
) -> PromotionManifest:
    if config.max_promotions == 0:
        return PromotionManifest(
            research_id=config.research_id,
            run_id=config.run_id,
            max_promotions=0,
            items=(),
            created_at=created_at,
        )
    by_source = {item.source_id: item for item in sources}
    cards_by_source: dict[str, list[EvidenceCard]] = defaultdict(list)
    for card in cards:
        cards_by_source[card.source_id].append(card)
    ranked = []
    for source_id, source_cards in cards_by_source.items():
        source = by_source.get(source_id)
        if source is None or source.source_type != "paper" or not source.local_path:
            continue
        verified_bonus = sum(item.status == "verified" for item in source_cards)
        cross_bonus = sum(item.status == "cross-checked" for item in source_cards)
        score = len(source_cards) * 10 + verified_bonus * 3 + cross_bonus
        ranked.append((score, source, source_cards))
    ranked.sort(key=lambda item: (-item[0], item[1].source_id))
    items = []
    for _, source, source_cards in ranked[: config.max_promotions]:
        facets = sorted({facet for card in source_cards for facet in card.target_facets})
        items.append(
            PromotionItem(
                source_id=source.source_id,
                evidence_card_ids=tuple(sorted(item.card_id for item in source_cards)),
                rationale=(
                    f"Report-critical paper with {len(source_cards)} located evidence "
                    f"cards across: {', '.join(facets) or 'unclassified facets'}."
                ),
            )
        )
    return PromotionManifest(
        research_id=config.research_id,
        run_id=config.run_id,
        max_promotions=config.max_promotions,
        items=tuple(items),
        created_at=created_at,
    )


__all__ = [
    "LangChainReviewSemanticEngine",
    "ReviewSemanticEngine",
    "build_promotion_manifest",
    "build_technology_map",
    "render_review_markdown",
]
