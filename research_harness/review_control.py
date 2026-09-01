"""Checkpointed review-first LangGraph and lifecycle wrapper."""

from __future__ import annotations

import asyncio
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Dict, Mapping, Optional, Sequence, Type

from langgraph.graph import END, START, StateGraph

from .config import HarnessSettings
from .model_client import ReviewModelBundle
from .persistence import HarnessPersistence
from .progress import NullProgress, ProgressSink
from .review_logic import (
    analyze_review_gaps,
    build_review_coverage,
    formal_wiki_paper_identities,
    merge_gap_uncertainties,
    merge_sources,
    normalize_nonconsensus_assessment,
    review_readiness,
    search_saturated,
    select_for_deep_read,
    select_for_skim_round,
    stable_id,
)
from .review_errorbook import aggregate_review_error_book
from .review_models import (
    EvidenceCard,
    NonConsensusAssessment,
    ReasoningUpdate,
    ResearchUncertainty,
    ReviewErrorEvent,
    ReviewReadiness,
    ReviewRunConfig,
    ReviewScope,
    ReviewSynthesisDraft,
    SourceRecord,
    SourceScreening,
    SourceSkim,
    TrajectoryEvent,
    UnderstandingClaim,
)
from .review_providers import ReviewProviderRegistry
from .review_semantics import (
    LangChainReviewSemanticEngine,
    ReviewSemanticEngine,
    build_promotion_manifest,
    build_technology_map,
    render_review_markdown,
)
from .review_storage import ReviewArtifactStore, load_review_scope
from .state import ReviewState


REVIEW_CHECKPOINT_NAMESPACE = "review-loop-v0.1"
THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
STAGE_RANK = {
    "frame": 0,
    "retrieval": 1,
    "screening": 2,
    "skim": 3,
    "reasoning": 4,
    "deep-read": 5,
    "assessment": 6,
    "synthesis": 7,
}
PROVIDER_SOURCE_TYPE = {
    "deepxiv": "paper",
    "github": "project",
    "tavily": "web",
}


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _selected_thread(research_id: str, thread_id: Optional[str]) -> str:
    selected = thread_id or f"review:{research_id}"
    if not THREAD_ID_PATTERN.fullmatch(selected):
        raise ValueError(
            "thread_id must be 1-200 ASCII letters, digits, dots, colons, "
            "underscores, or hyphens"
        )
    return selected


def _checkpoint_config(
    research_id: str,
    thread_id: str,
    *,
    recursion_limit: Optional[int] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "configurable": {
            "thread_id": f"{REVIEW_CHECKPOINT_NAMESPACE}:{research_id}:{thread_id}"
        }
    }
    if recursion_limit is not None:
        result["recursion_limit"] = recursion_limit
    return result


def _record_error(
    store: ReviewArtifactStore,
    *,
    stage: str,
    recurrence_key: str,
    observed: str,
    source_id: Optional[str] = None,
) -> None:
    sanitized = observed
    for name in (
        "OPENAI_API_KEY",
        "HARNESS_FAST_API_KEY",
        "HARNESS_REASONING_API_KEY",
        "DEEPXIV_TOKEN",
        "SEMANTIC_SCHOLAR_API_KEY",
        "S2_API_KEY",
        "TAVILY_API_KEY",
        "GITHUB_TOKEN",
        "DEEPSEEK_API_KEY",
    ):
        secret = os.getenv(name, "")
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    store.append_error(
        ReviewErrorEvent(
            run_id=store.config.run_id,
            research_id=store.config.research_id,
            stage=stage,
            recurrence_key=recurrence_key,
            observed=sanitized[:2000],
            source_id=source_id,
            timestamp=_utc_now(),
        )
    )


def _append_trajectory(
    store: ReviewArtifactStore,
    state: ReviewState,
    *,
    stage: str,
    action: str,
    evidence_gained: Sequence[str] = (),
    understanding_change: Optional[str] = None,
    next_pivot: Optional[str] = None,
    stop_reason: Optional[str] = None,
) -> int:
    sequence = int(state.get("trajectory_sequence") or 0) + 1
    store.append_trajectory(
        TrajectoryEvent(
            sequence=sequence,
            timestamp=_utc_now(),
            stage=stage,
            question=store.config.question,
            action=action,
            evidence_gained=tuple(evidence_gained),
            understanding_change=understanding_change,
            next_pivot=next_pivot,
            stop_reason=stop_reason,
        )
    )
    return sequence


def _initial_uncertainties(scope: ReviewScope) -> tuple[ResearchUncertainty, ...]:
    values = [
        ResearchUncertainty(
            uncertainty_id=stable_id("uncertainty", scope.research_id, "primary"),
            question=scope.question,
            category="performance",
            priority=1.0,
            blocking=True,
            next_queries=scope.seed_queries[:2],
        )
    ]
    category_by_facet = {
        "technical-taxonomy": "taxonomy",
        "latency-throughput": "engineering",
        "memory-and-kv-cache": "engineering",
        "kernels-and-hardware": "engineering",
        "open-source-implementations": "replication",
        "limitations-and-counter-evidence": "nonconsensus",
    }
    for facet in scope.required_facets:
        values.append(
            ResearchUncertainty(
                uncertainty_id=stable_id("uncertainty", scope.research_id, facet),
                question=f"What evidence is required to explain {facet}?",
                category=category_by_facet.get(facet, "performance"),
                priority=0.72,
                blocking=False,
                origin="deterministic",
            )
        )
    for hypothesis in scope.candidate_hypotheses:
        values.append(
            ResearchUncertainty(
                uncertainty_id=stable_id("uncertainty", scope.research_id, hypothesis),
                question=hypothesis,
                category="nonconsensus",
                priority=0.85,
                blocking=False,
            )
        )
    return tuple(values)


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _query_signature(query) -> tuple[str, str, str]:
    return (
        query.provider,
        _normalized_text(query.text),
        str(query.uncertainty_id or ""),
    )


def _refresh_review_analysis(
    *,
    scope: ReviewScope,
    store: ReviewArtifactStore,
    claims: Sequence[UnderstandingClaim],
    uncertainties: Sequence[ResearchUncertainty],
    assessments: Sequence[NonConsensusAssessment],
) -> tuple[ResearchUncertainty, ...]:
    sources = store.sources()
    skims = store.skims()
    cards = store.cards()
    technology_map = build_technology_map(
        sources=sources,
        skims=skims,
        cards=cards,
    )
    prior_relation_status = {
        str(item.get("relation_id")): str(item.get("status"))
        for item in store.technology_map().get("relation_candidates", [])
        if isinstance(item, Mapping)
        and item.get("status") in {"confirmed", "rejected"}
    }
    for relation in technology_map.get("relation_candidates", []):
        relation_id = str(relation.get("relation_id") or "")
        if relation_id in prior_relation_status:
            relation["status"] = prior_relation_status[relation_id]
    coverage = build_review_coverage(
        required_facets=scope.required_facets,
        skims=skims,
        cards=cards,
    )
    gaps = analyze_review_gaps(
        scope_title=scope.title,
        sources=sources,
        skims=skims,
        cards=cards,
        claims=claims,
        uncertainties=uncertainties,
        assessments=assessments,
        coverage=coverage,
    )
    merged_uncertainties = merge_gap_uncertainties(uncertainties, gaps)
    store.write_technology_map(technology_map)
    store.write_coverage(coverage)
    store.write_gaps(gaps)
    store.write_uncertainties(merged_uncertainties)
    return merged_uncertainties


def _merge_reasoning(
    *,
    update: ReasoningUpdate,
    existing_claims: Sequence[UnderstandingClaim],
    existing_uncertainties: Sequence[ResearchUncertainty],
    existing_assessments: Sequence[NonConsensusAssessment],
    cards: Sequence[EvidenceCard],
) -> tuple[
    tuple[UnderstandingClaim, ...],
    tuple[ResearchUncertainty, ...],
    tuple[NonConsensusAssessment, ...],
]:
    cards_by_id = {item.card_id: item for item in cards}
    known_cards = set(cards_by_id)
    claims = {item.claim_id: item for item in existing_claims}
    claim_texts = {_normalized_text(item.statement): item.claim_id for item in existing_claims}
    for raw in update.claims:
        claim_id = raw.claim_id
        matching_id = claim_texts.get(_normalized_text(raw.statement))
        if matching_id:
            claim_id = matching_id
        supporting = tuple(item for item in raw.supporting_card_ids if item in known_cards)
        opposing = tuple(item for item in raw.opposing_card_ids if item in known_cards)
        status = raw.status
        if status in {"supported", "contested"} and not (supporting or opposing):
            status = "provisional"
        normalized = raw.model_copy(
            update={
                "claim_id": claim_id,
                "supporting_card_ids": supporting,
                "opposing_card_ids": opposing,
                "status": status,
            }
        )
        claims[claim_id] = normalized
        claim_texts[_normalized_text(normalized.statement)] = claim_id

    uncertainties = {item.uncertainty_id: item for item in existing_uncertainties}
    uncertainty_texts = {
        _normalized_text(item.question): item.uncertainty_id
        for item in existing_uncertainties
    }
    for raw in update.uncertainties:
        uncertainty_id = uncertainty_texts.get(
            _normalized_text(raw.question), raw.uncertainty_id
        )
        current = uncertainties.get(uncertainty_id)
        if current is not None and current.origin == "deterministic":
            continue
        supporting = tuple(item for item in raw.supporting_card_ids if item in known_cards)
        opposing = tuple(item for item in raw.opposing_card_ids if item in known_cards)
        normalized = raw.model_copy(
            update={
                "uncertainty_id": uncertainty_id,
                "supporting_card_ids": supporting,
                "opposing_card_ids": opposing,
            }
        )
        uncertainties[uncertainty_id] = normalized
        uncertainty_texts[_normalized_text(normalized.question)] = uncertainty_id
    for uncertainty_id in update.resolved_uncertainty_ids:
        current = uncertainties.get(uncertainty_id)
        if current is not None and current.origin != "deterministic":
            uncertainties[uncertainty_id] = current.model_copy(
                update={
                    "status": "resolved",
                    "resolution": current.resolution or update.summary,
                }
            )

    assessments = {item.assessment_id: item for item in existing_assessments}
    assessment_questions = {
        _normalized_text(item.question): item.assessment_id
        for item in existing_assessments
    }
    for raw in update.assessments:
        try:
            raw = normalize_nonconsensus_assessment(
                raw,
                cards_by_id,
                basis="evidence-pool" if cards else "skim",
            )
        except ValueError:
            continue
        assessment_id = assessment_questions.get(
            _normalized_text(raw.question), raw.assessment_id
        )
        normalized = raw.model_copy(update={"assessment_id": assessment_id})
        assessments[assessment_id] = normalized
        assessment_questions[_normalized_text(normalized.question)] = assessment_id
    return (
        tuple(sorted(claims.values(), key=lambda item: item.claim_id)),
        tuple(
            sorted(uncertainties.values(), key=lambda item: item.uncertainty_id)
        ),
        tuple(
            sorted(assessments.values(), key=lambda item: item.assessment_id)
        ),
    )


def _bounded_sources(
    values: Sequence[SourceRecord], config: ReviewRunConfig
) -> tuple[SourceRecord, ...]:
    merged = merge_sources(values)
    quotas = {
        "paper": config.paper_source_quota,
        "project": config.project_source_quota,
        "web": config.web_source_quota,
    }
    retained = []
    overflow = []
    for source_type in ("paper", "project", "web"):
        items = [item for item in merged if item.source_type == source_type]
        items.sort(
            key=lambda item: (
                min(discovery.rank for discovery in item.discoveries),
                item.source_id,
            )
        )
        retained.extend(items[: quotas[source_type]])
        overflow.extend(items[quotas[source_type] :])
    if len(retained) < config.max_sources:
        overflow.sort(
            key=lambda item: (
                min(discovery.rank for discovery in item.discoveries),
                item.source_id,
            )
        )
        retained.extend(overflow[: config.max_sources - len(retained)])
    return tuple(sorted(retained[: config.max_sources], key=lambda item: item.source_id))


def _provider_limits(
    config: ReviewRunConfig,
    sources: Sequence[SourceRecord],
    round_number: int,
    queries: Sequence[Any],
) -> dict[str, int]:
    counts = {
        source_type: sum(item.source_type == source_type for item in sources)
        for source_type in ("paper", "project", "web")
    }
    quotas = {
        "paper": config.paper_source_quota,
        "project": config.project_source_quota,
        "web": config.web_source_quota,
    }
    rounds_left = max(1, config.max_search_rounds - round_number + 1)
    query_counts = {
        provider: sum(item.provider == provider for item in queries)
        for provider in PROVIDER_SOURCE_TYPE
    }
    active_source_types = tuple(
        source_type
        for source_type in ("paper", "project", "web")
        if any(
            query_counts.get(provider, 0)
            for provider, mapped_type in PROVIDER_SOURCE_TYPE.items()
            if mapped_type == source_type
        )
    )
    global_remaining = max(0, config.max_sources - len(sources))
    reallocation_share = 0
    if round_number > 1 and active_source_types and global_remaining:
        # Source quotas are soft. Starting in round two, queried source types
        # share the remaining global budget and keep the funnel moving.
        reallocation_share = math.ceil(
            global_remaining / rounds_left / len(active_source_types)
        )
    limits = {}
    for provider, source_type in PROVIDER_SOURCE_TYPE.items():
        provider_queries = query_counts.get(provider, 0)
        if not provider_queries:
            limits[provider] = 0
            continue
        remaining = max(0, quotas[source_type] - counts[source_type])
        round_allowance = math.ceil(remaining / rounds_left) if remaining else 0
        source_type_queries = sum(
            query_counts.get(candidate, 0)
            for candidate, mapped_type in PROVIDER_SOURCE_TYPE.items()
            if mapped_type == source_type
        )
        source_type_allowance = max(round_allowance, reallocation_share)
        limits[provider] = (
            math.ceil(source_type_allowance / source_type_queries)
            if source_type_allowance and source_type_queries
            else 0
        )
    return limits


def _run_async(coro):
    return asyncio.run(coro)


def build_review_graph(
    *,
    config: ReviewRunConfig,
    scope: ReviewScope,
    store: ReviewArtifactStore,
    semantic_engine: ReviewSemanticEngine,
    providers: ReviewProviderRegistry,
    persistence: HarnessPersistence,
    progress: Optional[ProgressSink] = None,
):
    if persistence.checkpointer is None:
        raise RuntimeError("Harness persistence must be open before compiling the review graph")
    progress_sink = progress or NullProgress()

    def bootstrap(state: ReviewState) -> Dict[str, Any]:
        progress_sink.update(stage="bootstrap", detail="Initializing run artifacts")
        store.initialize()
        return {
            "research_id": config.research_id,
            "run_id": config.run_id,
            "thread_id": config.thread_id,
            "phase": "frame",
            "round_number": int(state.get("round_number") or 0),
            "allow_network": config.allow_network,
            "model_fingerprint": semantic_engine.model_fingerprint,
            "trajectory_sequence": int(state.get("trajectory_sequence") or 0),
            "stop_reason": "",
            "completed": False,
        }

    def frame(state: ReviewState) -> Dict[str, Any]:
        progress_sink.update(stage="frame", detail="Framing research questions and facets")
        uncertainties = store.uncertainties()
        if not uncertainties:
            uncertainties = _initial_uncertainties(scope)
            store.write_uncertainties(uncertainties)
        sequence = _append_trajectory(
            store,
            state,
            stage="frame",
            action="Frame the review question, evidence facets, and candidate non-consensus hypotheses.",
            next_pivot=uncertainties[0].question if uncertainties else None,
        )
        return {
            "phase": "retrieval",
            "uncertainty_ids": [item.uncertainty_id for item in uncertainties],
            "trajectory_sequence": sequence,
        }

    def retrieve(state: ReviewState) -> Dict[str, Any]:
        round_number = int(state.get("round_number") or 0) + 1
        progress_sink.update(
            stage="retrieval",
            detail=f"Planning search round {round_number}",
        )
        existing_sources = store.sources()
        prior_queries = store.queries()
        plan = semantic_engine.plan_queries(
            scope=scope,
            config=config,
            round_number=round_number,
            uncertainties=store.uncertainties(),
            prior_queries=prior_queries,
            enabled_providers=providers.names,
        )
        signatures = {_query_signature(item) for item in prior_queries}
        queries = tuple(
            item
            for item in plan.queries
            if _query_signature(item) not in signatures
        )
        remaining_query_budget = max(0, config.max_queries - len(prior_queries))
        queries = queries[:remaining_query_budget]
        if not queries:
            sequence = _append_trajectory(
                store,
                state,
                stage="retrieval",
                action="No novel provider query remained after deterministic deduplication.",
                stop_reason="query-saturation",
            )
            return {
                "phase": "synthesis",
                "round_number": round_number,
                "trajectory_sequence": sequence,
                "stop_reason": "query-saturation",
            }
        plan = plan.model_copy(update={"queries": queries})
        store.write_query_plan(plan)
        limits = _provider_limits(config, existing_sources, round_number, queries)
        progress_sink.update(
            stage="retrieval",
            detail=(
                f"Running round {round_number} queries across "
                f"{', '.join(sorted({item.provider for item in queries}))}"
            ),
            completed=0,
            total=len(queries),
        )
        batch = _run_async(providers.search(queries, limits=limits))
        progress_sink.update(
            stage="retrieval",
            detail=f"Retrieved {len(batch.sources)} source records",
            completed=len(queries),
            total=len(queries),
        )
        # A query becomes part of durable trajectory only after its provider
        # batch returns. Per-query successful results are cached by the provider
        # registry, so an interrupt retries only unfinished calls.
        store.write_queries((*prior_queries, *queries))
        query_providers = {item.id: item.provider for item in queries}
        for query_id, error in batch.errors:
            _record_error(
                store,
                stage="retrieval",
                recurrence_key=(
                    f"review-provider:{query_providers.get(query_id, 'unknown')}"
                ),
                observed=f"{query_id}: {error}",
            )
        sources = _bounded_sources((*existing_sources, *batch.sources), config)
        store.write_sources(sources)
        method_families = {
            family for skim in store.skims() for family in skim.method_families
        }
        blocking_open = {
            item.uncertainty_id
            for item in store.uncertainties()
            if item.blocking and item.status == "open"
        }
        coverage = store.coverage()
        covered_facets = {
            item.facet for item in coverage.facets if item.status == "covered"
        } if coverage else set()
        evidence_sources = {item.source_id for item in store.cards()}
        technology_map = store.technology_map()
        confirmed_relations = {
            str(item.get("relation_id"))
            for item in technology_map.get("relation_candidates", [])
            if isinstance(item, Mapping) and item.get("status") == "confirmed"
        }
        sequence = _append_trajectory(
            store,
            state,
            stage="retrieval",
            action=(
                f"Round {round_number}: executed {len(queries)} queries across "
                f"{', '.join(sorted({item.provider for item in queries}))}."
            ),
            evidence_gained=tuple(item.source_id for item in batch.sources),
            next_pivot=(
                max(
                    (item for item in store.uncertainties() if item.status == "open"),
                    key=lambda item: item.priority,
                    default=None,
                ).question
                if any(item.status == "open" for item in store.uncertainties())
                else None
            ),
        )
        return {
            "phase": "screening",
            "round_number": round_number,
            "source_ids": [item.source_id for item in sources],
            "round_start": {
                "cards": len(store.cards()),
                "method_families": sorted(method_families),
                "blocking_open": sorted(blocking_open),
                "evidence_sources": sorted(evidence_sources),
                "covered_facets": sorted(covered_facets),
                "confirmed_relations": sorted(confirmed_relations),
            },
            "trajectory_sequence": sequence,
        }

    def screen(state: ReviewState) -> Dict[str, Any]:
        sources = store.sources()
        existing = {item.source_id: item for item in store.screenings()}
        pending = [item for item in sources if item.source_id not in existing]
        batches = [pending[index : index + 16] for index in range(0, len(pending), 16)]
        processed = 0
        progress_sink.update(
            stage="screening",
            detail="Screening source metadata",
            completed=processed,
            total=len(pending),
        )
        if batches:
            with ThreadPoolExecutor(max_workers=config.skim_concurrency) as executor:
                futures = {
                    executor.submit(
                        semantic_engine.screen_batch,
                        scope=scope,
                        sources=batch,
                    ): batch
                    for batch in batches
                }
                for future in as_completed(futures):
                    batch = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        _record_error(
                            store,
                            stage="screening",
                            recurrence_key="review-screening:structured-output",
                            observed=f"{type(exc).__name__}: {exc}",
                            source_id=",".join(item.source_id for item in batch),
                        )
                    else:
                        existing.update({item.source_id: item for item in result.screenings})
                        store.write_screenings(tuple(existing.values()))
                    processed += len(batch)
                    progress_sink.update(
                        stage="screening",
                        detail="Screening source metadata",
                        completed=processed,
                        total=len(pending),
                    )
        selected = select_for_skim_round(
            sources,
            existing,
            tuple(item.source_id for item in store.skims()),
            config,
            round_number=int(state.get("round_number") or 1),
        )
        sequence = _append_trajectory(
            store,
            state,
            stage="screening",
            action=f"Screened {len(existing)} sources and selected {len(selected)} for skim.",
            evidence_gained=selected,
        )
        return {
            "phase": "skim",
            "screening_ids": sorted(existing),
            "selected_skim_ids": list(selected),
            "trajectory_sequence": sequence,
        }

    def skim(state: ReviewState) -> Dict[str, Any]:
        sources = {item.source_id: item for item in store.sources()}
        screenings = {item.source_id: item for item in store.screenings()}
        existing = {item.source_id: item for item in store.skims()}
        selected = tuple(state.get("selected_skim_ids") or ())
        pending = [sources[item] for item in selected if item in sources and item not in existing]
        processed = 0
        progress_sink.update(
            stage="skim",
            detail="Reading titles, abstracts, and source summaries",
            completed=processed,
            total=len(pending),
        )
        if pending:
            with ThreadPoolExecutor(max_workers=config.skim_concurrency) as executor:
                futures = {
                    executor.submit(
                        semantic_engine.skim_source,
                        scope=scope,
                        source=source,
                        source_role=(
                            screenings[source.source_id].source_role
                            if source.source_id in screenings
                            else "background"
                        ),
                    ): source
                    for source in pending
                }
                for future in as_completed(futures):
                    source = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        _record_error(
                            store,
                            stage="skim",
                            recurrence_key="review-skim:structured-output",
                            observed=f"{type(exc).__name__}: {exc}",
                            source_id=source.source_id,
                        )
                    else:
                        existing[result.source_id] = result
                        store.write_skims(tuple(existing.values()))
                    processed += 1
                    progress_sink.update(
                        stage="skim",
                        detail=source.title,
                        completed=processed,
                        total=len(pending),
                    )
        sequence = _append_trajectory(
            store,
            state,
            stage="skim",
            action=f"Completed {len(existing)} provisional skims; none are citation evidence.",
            evidence_gained=tuple(sorted(existing)),
        )
        uncertainties = _refresh_review_analysis(
            scope=scope,
            store=store,
            claims=store.claims(),
            uncertainties=store.uncertainties(),
            assessments=store.assessments(),
        )
        return {
            "phase": "reasoning",
            "skim_ids": sorted(existing),
            "uncertainty_ids": [item.uncertainty_id for item in uncertainties],
            "trajectory_sequence": sequence,
        }

    def reason_from_skims(state: ReviewState) -> Dict[str, Any]:
        progress_sink.update(
            stage="reasoning",
            detail="Updating provisional understanding and open questions",
        )
        selected = set(state.get("selected_skim_ids") or ())
        skims = tuple(item for item in store.skims() if item.source_id in selected)
        claims = store.claims()
        uncertainties = store.uncertainties()
        assessments = store.assessments()
        try:
            update = semantic_engine.reason(
                scope=scope,
                skims=skims,
                cards=store.cards(),
                claims=claims,
                uncertainties=uncertainties,
            )
            claims, uncertainties, assessments = _merge_reasoning(
                update=update,
                existing_claims=claims,
                existing_uncertainties=uncertainties,
                existing_assessments=assessments,
                cards=store.cards(),
            )
            store.write_claims(claims)
            store.write_uncertainties(uncertainties)
            store.write_assessments(assessments)
            summary = update.summary
        except Exception as exc:
            _record_error(
                store,
                stage="reasoning",
                recurrence_key="review-reasoning:structured-output",
                observed=f"{type(exc).__name__}: {exc}",
            )
            summary = "Understanding update failed; retained previous state."
        uncertainties = _refresh_review_analysis(
            scope=scope,
            store=store,
            claims=claims,
            uncertainties=uncertainties,
            assessments=assessments,
        )
        sequence = _append_trajectory(
            store,
            state,
            stage="reasoning",
            action="Update provisional understanding from skims and existing evidence.",
            understanding_change=summary,
            next_pivot=(
                max(
                    (item for item in uncertainties if item.status == "open"),
                    key=lambda item: item.priority,
                    default=None,
                ).question
                if any(item.status == "open" for item in uncertainties)
                else None
            ),
        )
        return {
            "phase": "deep-read",
            "claim_ids": [item.claim_id for item in claims],
            "uncertainty_ids": [item.uncertainty_id for item in uncertainties],
            "assessment_ids": [item.assessment_id for item in assessments],
            "trajectory_sequence": sequence,
        }

    async def _acquire_pending_materials(
        selected_sources: Sequence[SourceRecord],
    ) -> tuple[
        dict[str, Any],
        dict[str, SourceRecord],
        list[tuple[SourceRecord, Exception]],
        list[tuple[SourceRecord, Exception]],
    ]:
        semaphore = asyncio.Semaphore(config.network_concurrency)

        enrichment_errors = []
        try:
            enriched_sources = await providers.enrich_sources(selected_sources)
        except Exception as exc:
            enriched_sources = {item.source_id: item for item in selected_sources}
            if selected_sources:
                enrichment_errors.append((selected_sources[0], exc))

        async def one(source: SourceRecord):
            enriched = enriched_sources.get(source.source_id, source)
            material = store.material(source.source_id)
            if material is not None:
                return source, enriched, material, None
            try:
                async with semaphore:
                    material = await providers.acquire_material(enriched)
                return source, enriched, material, None
            except Exception as exc:
                return source, enriched, None, exc

        materials = {}
        acquisition_errors = []
        tasks = [asyncio.create_task(one(item)) for item in selected_sources]
        processed = 0
        for task in asyncio.as_completed(tasks):
            source, enriched, material, acquisition_error = await task
            processed += 1
            enriched_sources[source.source_id] = enriched
            if acquisition_error is not None:
                acquisition_errors.append((source, acquisition_error))
            elif material is not None:
                # Persist each completed material before waiting for the rest of
                # the batch so Ctrl+C only leaves unfinished sources pending.
                store.write_material(material)
                materials[source.source_id] = material
            progress_sink.update(
                stage="deep-read",
                detail=f"Acquiring source material: {source.title}",
                completed=processed,
                total=len(selected_sources),
            )
        return materials, enriched_sources, acquisition_errors, enrichment_errors

    def deep_read(state: ReviewState) -> Dict[str, Any]:
        sources = {item.source_id: item for item in store.sources()}
        skims = {item.source_id: item for item in store.skims()}
        ranked = select_for_deep_read(tuple(sources.values()), skims, config)
        target_total = min(
            config.max_deep_reads,
            math.ceil(config.max_deep_reads * int(state.get("round_number") or 1) / config.max_search_rounds),
        )
        selected = tuple(ranked[:target_total])
        selected_sources = [sources[item] for item in selected if item in sources]
        progress_sink.update(
            stage="deep-read",
            detail="Downloading and extracting selected source material",
            completed=0,
            total=len(selected_sources),
        )
        materials, enriched_sources, acquisition_errors, enrichment_errors = _run_async(
            _acquire_pending_materials(selected_sources)
        )
        for source, exc in enrichment_errors:
            _record_error(
                store,
                stage="deep-read",
                recurrence_key="review-provider:semantic-scholar-details",
                observed=f"{type(exc).__name__}: {exc}",
                source_id=source.source_id,
            )
        for source, exc in acquisition_errors:
            _record_error(
                store,
                stage="deep-read",
                recurrence_key="review-deep-read:source-acquisition",
                observed=f"{type(exc).__name__}: {exc}",
                source_id=source.source_id,
            )
        updated_sources = []
        for source in sources.values():
            source = enriched_sources.get(source.source_id, source)
            material = materials.get(source.source_id) or store.material(source.source_id)
            if material is None:
                updated_sources.append(source)
                continue
            updated_sources.append(
                source.model_copy(
                    update={
                        "local_path": material.local_path or source.local_path,
                        "content_sha256": material.sha256,
                        "content_preview": source.content_preview or material.text[:4000],
                    }
                )
            )
        store.write_sources(tuple(updated_sources))
        source_map = {item.source_id: item for item in updated_sources}
        completed = set(store.deep_read_completed())
        cards = {item.card_id: item for item in store.cards()}
        pending = [
            source_map[source_id]
            for source_id in selected
            if source_id in source_map
            and source_id not in completed
            and store.material(source_id) is not None
        ]
        extracted = 0
        progress_sink.update(
            stage="deep-read",
            detail="Extracting citation-ready EvidenceCards",
            completed=extracted,
            total=len(pending),
        )
        if pending:
            with ThreadPoolExecutor(max_workers=config.deep_read_concurrency) as executor:
                futures = {
                    executor.submit(
                        semantic_engine.extract_evidence,
                        scope=scope,
                        source=source,
                        material=store.material(source.source_id),
                        claims=store.claims(),
                        source_role=(
                            skims[source.source_id].source_role
                            if source.source_id in skims
                            else "background"
                        ),
                    ): source
                    for source in pending
                }
                for future in as_completed(futures):
                    source = futures[future]
                    try:
                        extraction = future.result()
                    except Exception as exc:
                        _record_error(
                            store,
                            stage="deep-read",
                            recurrence_key="review-evidence:structured-output",
                            observed=f"{type(exc).__name__}: {exc}",
                            source_id=source.source_id,
                        )
                    else:
                        cards.update({item.card_id: item for item in extraction.cards})
                        store.write_cards(tuple(cards.values()))
                        store.mark_deep_read_completed(source.source_id)
                    extracted += 1
                    progress_sink.update(
                        stage="deep-read",
                        detail=f"Evidence extraction: {source.title}",
                        completed=extracted,
                        total=len(pending),
                    )
        completed = set(store.deep_read_completed())
        uncertainties = _refresh_review_analysis(
            scope=scope,
            store=store,
            claims=store.claims(),
            uncertainties=store.uncertainties(),
            assessments=store.assessments(),
        )
        sequence = _append_trajectory(
            store,
            state,
            stage="deep-read",
            action=(
                f"Deep-read target {len(selected)}; completed {len(completed)} sources "
                f"and retained {len(cards)} located EvidenceCards."
            ),
            evidence_gained=tuple(sorted(cards)),
        )
        return {
            "phase": "assessment",
            "selected_deep_read_ids": list(selected),
            "deep_read_ids": sorted(completed),
            "evidence_card_ids": sorted(cards),
            "uncertainty_ids": [item.uncertainty_id for item in uncertainties],
            "trajectory_sequence": sequence,
        }

    def assess(state: ReviewState) -> Dict[str, Any]:
        progress_sink.update(
            stage="assessment",
            detail="Evaluating coverage, gaps, and cross-paper evidence",
        )
        selected = set(state.get("selected_skim_ids") or ())
        skims = tuple(item for item in store.skims() if item.source_id in selected)
        claims = store.claims()
        uncertainties = store.uncertainties()
        assessments = store.assessments()
        update: Optional[ReasoningUpdate] = None
        try:
            update = semantic_engine.reason(
                scope=scope,
                skims=skims,
                cards=store.cards(),
                claims=claims,
                uncertainties=uncertainties,
            )
            claims, uncertainties, assessments = _merge_reasoning(
                update=update,
                existing_claims=claims,
                existing_uncertainties=uncertainties,
                existing_assessments=assessments,
                cards=store.cards(),
            )
            store.write_claims(claims)
            store.write_uncertainties(uncertainties)
            store.write_assessments(assessments)
        except Exception as exc:
            _record_error(
                store,
                stage="assessment",
                recurrence_key="review-assessment:structured-output",
                observed=f"{type(exc).__name__}: {exc}",
            )
        uncertainties = _refresh_review_analysis(
            scope=scope,
            store=store,
            claims=claims,
            uncertainties=uncertainties,
            assessments=assessments,
        )
        start = state.get("round_start") or {}
        current_families = {
            family for skim in store.skims() for family in skim.method_families
        }
        previous_families = set(start.get("method_families") or [])
        previous_blocking = set(start.get("blocking_open") or [])
        current_blocking = {
            item.uncertainty_id
            for item in uncertainties
            if item.blocking and item.status == "open"
        }
        current_evidence_sources = {item.source_id for item in store.cards()}
        previous_evidence_sources = set(start.get("evidence_sources") or [])
        coverage = store.coverage()
        current_covered_facets = {
            item.facet for item in coverage.facets if item.status == "covered"
        } if coverage else set()
        previous_covered_facets = set(start.get("covered_facets") or [])
        technology_map = store.technology_map()
        current_confirmed_relations = {
            str(item.get("relation_id"))
            for item in technology_map.get("relation_candidates", [])
            if isinstance(item, Mapping) and item.get("status") == "confirmed"
        }
        previous_confirmed_relations = set(start.get("confirmed_relations") or [])
        gain = {
            "round": int(state.get("round_number") or 0),
            "new_method_families": len(current_families - previous_families),
            "new_evidence_cards": max(0, len(store.cards()) - int(start.get("cards") or 0)),
            "new_independent_sources": len(
                current_evidence_sources - previous_evidence_sources
            ),
            "new_covered_facets": len(
                current_covered_facets - previous_covered_facets
            ),
            "resolved_blocking_uncertainties": len(previous_blocking - current_blocking),
            "independent_counterevidence": bool(
                update and update.found_independent_counterevidence
            ),
            "new_confirmed_topology_relations": len(
                current_confirmed_relations - previous_confirmed_relations
            ),
        }
        gains = (*store.round_gains(), gain)
        store.write_round_gains(gains)
        saturated = search_saturated(gains)
        readiness = review_readiness(
            required_facets=scope.required_facets,
            cards=store.cards(),
            claims=claims,
            uncertainties=uncertainties,
            assessments=assessments,
            saturated=saturated,
        )
        store.write_readiness(readiness)
        round_number = int(state.get("round_number") or 0)
        budget_stop = (
            round_number >= config.max_search_rounds
            or len(store.queries()) >= config.max_queries
        )
        next_phase = "synthesis" if readiness.ready or budget_stop else "retrieval"
        stop_reason = (
            "review-ready"
            if readiness.ready
            else "review-budget-reached"
            if budget_stop
            else ""
        )
        next_uncertainty = max(
            (item for item in uncertainties if item.status == "open"),
            key=lambda item: item.priority,
            default=None,
        )
        sequence = _append_trajectory(
            store,
            state,
            stage="assessment",
            action=(
                "Evaluate evidence coverage, independent non-consensus evidence, "
                "and understanding-level saturation."
            ),
            understanding_change=update.summary if update else None,
            next_pivot=(
                next_uncertainty.question if next_phase == "retrieval" and next_uncertainty else None
            ),
            stop_reason=stop_reason or None,
        )
        return {
            "phase": next_phase,
            "round_gains": list(gains),
            "readiness": readiness.model_dump(mode="json"),
            "claim_ids": [item.claim_id for item in claims],
            "uncertainty_ids": [item.uncertainty_id for item in uncertainties],
            "assessment_ids": [item.assessment_id for item in assessments],
            "trajectory_sequence": sequence,
            "stop_reason": stop_reason,
        }

    def synthesize(state: ReviewState) -> Dict[str, Any]:
        progress_sink.update(
            stage="synthesis",
            detail="Building the cited review and promotion suggestions",
        )
        claims = store.claims()
        assessments = store.assessments()
        uncertainties = _refresh_review_analysis(
            scope=scope,
            store=store,
            claims=claims,
            uncertainties=store.uncertainties(),
            assessments=assessments,
        )
        readiness = review_readiness(
            required_facets=scope.required_facets,
            cards=store.cards(),
            claims=claims,
            uncertainties=uncertainties,
            assessments=assessments,
            saturated=search_saturated(store.round_gains()),
        )
        store.write_readiness(readiness)
        try:
            draft = semantic_engine.synthesize(
                scope=scope,
                config=config,
                sources=store.sources(),
                cards=store.cards(),
                claims=claims,
                uncertainties=uncertainties,
                assessments=assessments,
                readiness=readiness,
            )
        except Exception as exc:
            _record_error(
                store,
                stage="synthesis",
                recurrence_key="review-synthesis:structured-output",
                observed=f"{type(exc).__name__}: {exc}",
            )
            draft = ReviewSynthesisDraft(
                title=scope.title,
                scope_summary=scope.question,
                open_questions=tuple(
                    item.question
                    for item in store.uncertainties()
                    if item.status != "resolved"
                ),
                limitations=(
                    "结构化综合模型调用失败；报告仅保留已验证的证据索引和开放问题。",
                ),
            )
        store.write_synthesis_draft(draft)
        manifest = build_promotion_manifest(
            config=config,
            sources=store.sources(),
            cards=store.cards(),
            created_at=_utc_now(),
            draft=draft,
            claims=claims,
            uncertainties=uncertainties,
            assessments=assessments,
            existing_paper_identities=formal_wiki_paper_identities(
                store.settings.wiki_root
            ),
        )
        store.write_promotion_manifest(manifest)
        report = render_review_markdown(
            scope=scope,
            config=config,
            draft=draft,
            sources=store.sources(),
            cards=store.cards(),
            uncertainties=store.uncertainties(),
            assessments=store.assessments(),
            readiness=readiness,
            trajectory_path=store.relative(store.trajectory_path),
        )
        store.write_report(report)
        aggregate_review_error_book(
            store.settings,
            research_id=config.research_id,
        )
        sequence = _append_trajectory(
            store,
            state,
            stage="synthesis",
            action="Render the structured synthesis into Markdown and create a promotion manifest.",
            evidence_gained=tuple(item.card_id for item in store.cards()),
            stop_reason=state.get("stop_reason") or "synthesis-complete",
        )
        return {
            "phase": "completed",
            "completed": True,
            "report_path": store.relative(store.report_path),
            "promotion_manifest_path": store.relative(store.promotion_path),
            "trajectory_sequence": sequence,
            "stop_reason": state.get("stop_reason") or "synthesis-complete",
        }

    def stop_early(state: ReviewState) -> Dict[str, Any]:
        progress_sink.update(
            stage=config.stop_after,
            detail=f"Stopping after requested stage {config.stop_after}",
        )
        sequence = _append_trajectory(
            store,
            state,
            stage=config.stop_after,
            action=f"Stop at requested stage {config.stop_after}.",
            stop_reason=f"stop-after-{config.stop_after}",
        )
        return {
            "phase": "stopped",
            "completed": False,
            "stop_reason": f"stop-after-{config.stop_after}",
            "trajectory_sequence": sequence,
        }

    def route(current_stage: str, next_node: str):
        def choose(state: ReviewState) -> str:
            del state
            return "stop" if config.stop_after == current_stage else next_node

        return choose

    def route_after_assessment(state: ReviewState) -> str:
        if config.stop_after == "assessment":
            return "stop"
        return "synthesis" if state.get("phase") == "synthesis" else "retrieval"

    def route_after_retrieval(state: ReviewState) -> str:
        if config.stop_after == "retrieval":
            return "stop"
        return "synthesis" if state.get("phase") == "synthesis" else "screen"

    builder = StateGraph(ReviewState)
    builder.add_node("bootstrap", bootstrap)
    builder.add_node("frame", frame)
    builder.add_node("retrieve", retrieve)
    builder.add_node("screen", screen)
    builder.add_node("skim", skim)
    builder.add_node("reason", reason_from_skims)
    builder.add_node("deep_read", deep_read)
    builder.add_node("assess", assess)
    builder.add_node("synthesize", synthesize)
    builder.add_node("stop_early", stop_early)
    builder.add_edge(START, "bootstrap")
    builder.add_edge("bootstrap", "frame")
    builder.add_conditional_edges(
        "frame", route("frame", "retrieve"), {"stop": "stop_early", "retrieve": "retrieve"}
    )
    builder.add_conditional_edges(
        "retrieve",
        route_after_retrieval,
        {"stop": "stop_early", "screen": "screen", "synthesis": "synthesize"},
    )
    builder.add_conditional_edges(
        "screen",
        route("screening", "skim"),
        {"stop": "stop_early", "skim": "skim"},
    )
    builder.add_conditional_edges(
        "skim", route("skim", "reason"), {"stop": "stop_early", "reason": "reason"}
    )
    builder.add_conditional_edges(
        "reason",
        route("reasoning", "deep_read"),
        {"stop": "stop_early", "deep_read": "deep_read"},
    )
    builder.add_conditional_edges(
        "deep_read",
        route("deep-read", "assess"),
        {"stop": "stop_early", "assess": "assess"},
    )
    builder.add_conditional_edges(
        "assess",
        route_after_assessment,
        {"stop": "stop_early", "synthesis": "synthesize", "retrieval": "retrieve"},
    )
    builder.add_edge("synthesize", END)
    builder.add_edge("stop_early", END)
    return builder.compile(
        checkpointer=persistence.checkpointer,
        name="review-first-research-loop-v0.1",
    )


class ReviewController:
    """Lifecycle and resume semantics for one review run."""

    def __init__(
        self,
        settings: HarnessSettings,
        *,
        config: ReviewRunConfig,
        scope: Optional[ReviewScope] = None,
        semantic_engine: Optional[ReviewSemanticEngine] = None,
        providers: Optional[ReviewProviderRegistry] = None,
        progress: Optional[ProgressSink] = None,
    ):
        self.settings = settings
        self.settings.validate()
        self.config = config
        self.scope = scope or load_review_scope(settings, config.research_id)
        self.store = ReviewArtifactStore(settings, config)
        if semantic_engine is None:
            if not config.allow_network:
                raise ValueError(
                    "The default review semantic engine requires explicit --allow-network"
                )
            bundle = ReviewModelBundle.from_env(
                settings,
                allow_single_model_fallback=config.allow_single_model_fallback,
                require_reasoning=config.profile == "standard",
            )
            semantic_engine = LangChainReviewSemanticEngine(
                bundle, settings.skills_root
            )
        self.semantic_engine = semantic_engine
        if (
            config.model_fingerprint
            and config.model_fingerprint != semantic_engine.model_fingerprint
        ):
            raise ValueError(
                "Configured review model fingerprint does not match the semantic engine"
            )
        self.providers = providers or ReviewProviderRegistry(
            settings.repository_root,
            self.store.working_root,
            network_concurrency=config.network_concurrency,
        )
        if providers is None and config.profile == "standard":
            self.providers.require_standard_sources()
        self.persistence = HarnessPersistence(settings)
        self.progress = progress or NullProgress()
        self.graph = None

    def open(self) -> "ReviewController":
        if self.graph is not None:
            return self
        self.persistence.open()
        try:
            self.graph = build_review_graph(
                config=self.config,
                scope=self.scope,
                store=self.store,
                semantic_engine=self.semantic_engine,
                providers=self.providers,
                persistence=self.persistence,
                progress=self.progress,
            )
        except Exception:
            self.close()
            raise
        return self

    def close(self) -> None:
        self.graph = None
        self.persistence.close()

    def start(self) -> Dict[str, Any]:
        if self.graph is None:
            raise RuntimeError("ReviewController is not open")
        selected = _selected_thread(self.config.research_id, self.config.thread_id)
        current = self.graph.get_state(
            _checkpoint_config(self.config.research_id, selected)
        )
        if current.values:
            raise ValueError(
                f"Review thread {selected!r} already exists; use research review resume"
            )
        return self.graph.invoke(
            {
                "research_id": self.config.research_id,
                "run_id": self.config.run_id,
                "thread_id": selected,
                "allow_network": self.config.allow_network,
            },
            _checkpoint_config(
                self.config.research_id, selected, recursion_limit=96
            ),
        )

    def resume(self, *, mode: str = "replan") -> Dict[str, Any]:
        if self.graph is None:
            raise RuntimeError("ReviewController is not open")
        if mode not in {"checkpoint", "replan"}:
            raise ValueError("review resume mode must be checkpoint or replan")
        selected = _selected_thread(self.config.research_id, self.config.thread_id)
        current = self.graph.get_state(
            _checkpoint_config(self.config.research_id, selected)
        )
        if not current.values:
            raise ValueError(f"No review checkpoint exists for thread {selected!r}")
        stored_fingerprint = current.values.get("model_fingerprint")
        if stored_fingerprint and stored_fingerprint != self.semantic_engine.model_fingerprint:
            raise ValueError(
                "Review checkpoint resume requires the same fast/reasoning model profiles"
            )
        if bool(current.values.get("allow_network")) != self.config.allow_network:
            raise ValueError("Review checkpoint resume must preserve network authority")
        if bool(current.values.get("completed")) and not current.next:
            return dict(current.values)
        if mode == "checkpoint":
            if not current.next:
                return dict(current.values)
            return self.graph.invoke(
                None,
                _checkpoint_config(
                    self.config.research_id, selected, recursion_limit=96
                ),
            )
        return self.graph.invoke(
            {
                "research_id": self.config.research_id,
                "run_id": self.config.run_id,
                "thread_id": selected,
                "allow_network": self.config.allow_network,
            },
            _checkpoint_config(
                self.config.research_id, selected, recursion_limit=96
            ),
        )

    def synthesize_now(self) -> Dict[str, Any]:
        if self.graph is None:
            raise RuntimeError("ReviewController is not open")
        self.progress.update(
            stage="assessment",
            detail="Refreshing evidence-based understanding before synthesis",
        )
        state = self.get_state().values
        if not state:
            raise ValueError("Review synthesis requires an existing checkpoint")
        selected = set(state.get("selected_skim_ids") or ())
        skims = tuple(
            item
            for item in self.store.skims()
            if not selected or item.source_id in selected
        )
        cards = self.store.cards()
        claims = self.store.claims()
        uncertainties = self.store.uncertainties()
        assessments = self.store.assessments()
        try:
            update = self.semantic_engine.reason(
                scope=self.scope,
                skims=skims,
                cards=cards,
                claims=claims,
                uncertainties=uncertainties,
            )
            claims, uncertainties, assessments = _merge_reasoning(
                update=update,
                existing_claims=claims,
                existing_uncertainties=uncertainties,
                existing_assessments=assessments,
                cards=cards,
            )
            self.store.write_claims(claims)
            self.store.write_uncertainties(uncertainties)
            self.store.write_assessments(assessments)
        except Exception as exc:
            _record_error(
                self.store,
                stage="assessment",
                recurrence_key="review-assessment:manual-synthesis",
                observed=f"{type(exc).__name__}: {exc}",
            )
        uncertainties = _refresh_review_analysis(
            scope=self.scope,
            store=self.store,
            claims=claims,
            uncertainties=uncertainties,
            assessments=assessments,
        )
        readiness = review_readiness(
            required_facets=self.scope.required_facets,
            cards=cards,
            claims=claims,
            uncertainties=uncertainties,
            assessments=assessments,
            saturated=search_saturated(self.store.round_gains()),
        )
        self.store.write_readiness(readiness)
        self.progress.update(
            stage="synthesis",
            detail="Building the cited review and promotion suggestions",
        )
        try:
            draft = self.semantic_engine.synthesize(
                scope=self.scope,
                config=self.config,
                sources=self.store.sources(),
                cards=cards,
                claims=claims,
                uncertainties=uncertainties,
                assessments=assessments,
                readiness=readiness,
            )
        except Exception as exc:
            _record_error(
                self.store,
                stage="synthesis",
                recurrence_key="review-synthesis:manual-command",
                observed=f"{type(exc).__name__}: {exc}",
            )
            raise
        self.store.write_synthesis_draft(draft)
        manifest = build_promotion_manifest(
            config=self.config,
            sources=self.store.sources(),
            cards=self.store.cards(),
            created_at=_utc_now(),
            draft=draft,
            claims=claims,
            uncertainties=uncertainties,
            assessments=assessments,
            existing_paper_identities=formal_wiki_paper_identities(
                self.store.settings.wiki_root
            ),
        )
        self.store.write_promotion_manifest(manifest)
        sequence = _append_trajectory(
            self.store,
            state,
            stage="synthesis",
            action="Refresh evidence-based understanding and render the review.",
            understanding_change=(
                f"Rendered {len(cards)} evidence cards into a review with "
                f"{len(claims)} understanding claims; readiness={readiness.ready}."
            ),
            stop_reason="manual-synthesis",
        )
        self.store.write_report(
            render_review_markdown(
                scope=self.scope,
                config=self.config,
                draft=draft,
                sources=self.store.sources(),
                cards=self.store.cards(),
                uncertainties=self.store.uncertainties(),
                assessments=self.store.assessments(),
                readiness=readiness,
                trajectory_path=self.store.relative(self.store.trajectory_path),
            )
        )
        self.progress.update(
            stage="synthesis",
            detail=f"Rendered report from {len(cards)} EvidenceCards",
            completed=len(cards),
            total=len(cards),
        )
        aggregate_review_error_book(
            self.settings,
            research_id=self.config.research_id,
        )
        return {
            **dict(state),
            "phase": "completed",
            "completed": True,
            "stop_reason": "manual-synthesis",
            "trajectory_sequence": sequence,
            "report_path": self.store.relative(self.store.report_path),
            "promotion_manifest_path": self.store.relative(self.store.promotion_path),
        }

    def get_state(self):
        if self.graph is None:
            raise RuntimeError("ReviewController is not open")
        selected = _selected_thread(self.config.research_id, self.config.thread_id)
        return self.graph.get_state(
            _checkpoint_config(self.config.research_id, selected)
        )

    def __enter__(self) -> "ReviewController":
        return self.open()

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.close()


def read_review_checkpoint(
    settings: HarnessSettings,
    *,
    research_id: str,
    thread_id: str,
) -> Dict[str, Any]:
    selected = _selected_thread(research_id, thread_id)
    with HarnessPersistence(settings) as persistence:
        assert persistence.checkpointer is not None
        checkpoint = persistence.checkpointer.get_tuple(
            _checkpoint_config(research_id, selected)
        )
    if checkpoint is None:
        raise ValueError(f"No review checkpoint exists for thread {selected!r}")
    return dict(checkpoint.checkpoint.get("channel_values") or {})


__all__ = [
    "REVIEW_CHECKPOINT_NAMESPACE",
    "ReviewController",
    "build_review_graph",
    "read_review_checkpoint",
]
