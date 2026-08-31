"""Deterministic inspection, gap candidates, progress, and completion gates."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import (
    AbstractSet,
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

import yaml  # type: ignore[import-untyped]

from tools.wiki.indexer import WikiIndex, build_index
from tools.wiki.models import Entity
from tools.wiki.validator import validate_index

from .config import HarnessSettings
from .evidence_revision import (
    evidence_revision_candidates,
    evidence_revision_exhausted,
)
from .research_models import (
    ActionAttemptStats,
    CorpusSnapshot,
    DoneCheck,
    DoneCriteria,
    EvidenceSnapshot,
    NonConsensusAssessment,
    ProgressMeasurement,
    QualitySnapshot,
    ResearchAction,
    ResearchDecision,
    ResearchGap,
    ResearchSnapshot,
    SearchYield,
    StopReason,
    TaxonomySnapshot,
)


RESEARCH_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,119}$")
FACET_STATUS_RANK = {"missing": 0, "partial": 1, "covered": 2}
RELEVANCE_LABELS = ("core", "adjacent", "background", "exclude")
CONTEXT_BUCKETS = ("<8K", "8K-32K", "32K-64K", ">=64K")
ENGINEERING_METRICS = ("latency", "throughput", "memory", "flops")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_research_directory(settings: HarnessSettings, research_id: str) -> Path:
    clean_id = research_id.strip()
    if not RESEARCH_ID_PATTERN.fullmatch(clean_id):
        raise ValueError(
            "research_id must use 1-120 lowercase letters, digits, and hyphens"
        )
    root = settings.research_root.resolve()
    path = (root / clean_id).resolve()
    if not _inside(path, root):
        raise ValueError(
            "Research directory must stay inside the repository research root"
        )
    if not path.is_dir():
        raise FileNotFoundError(f"Research directory does not exist: {path}")
    return path


def load_done_criteria(
    settings: HarnessSettings,
    research_id: str,
    path: Optional[Union[str, Path]] = None,
) -> DoneCriteria:
    research_directory = resolve_research_directory(settings, research_id)
    criteria_path = Path(path) if path is not None else Path("done-criteria.yaml")
    if not criteria_path.is_absolute():
        criteria_path = research_directory / criteria_path
    criteria_path = criteria_path.resolve()
    if not _inside(criteria_path, research_directory):
        raise ValueError(
            "Done criteria must stay inside the selected research directory"
        )
    if criteria_path.suffix.casefold() not in {".yaml", ".yml"}:
        raise ValueError("Done criteria must be a YAML file")
    if not criteria_path.is_file():
        raise FileNotFoundError(f"Done criteria do not exist: {criteria_path}")
    with criteria_path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, Mapping):
        raise ValueError(f"Done criteria must be a YAML mapping: {criteria_path}")
    return DoneCriteria.model_validate(payload)


def _load_search_runs(
    research_directory: Path,
) -> List[Tuple[Path, Mapping[str, Any], str]]:
    search_root = research_directory / "search-runs"
    if not search_root.is_dir():
        return []
    paths = sorted(
        [*search_root.glob("*.yaml"), *search_root.glob("*.yml")],
        key=lambda item: item.name.casefold(),
    )
    loaded = []
    for path in paths:
        resolved = path.resolve()
        if not _inside(resolved, research_directory):
            raise ValueError(f"Search run escapes research directory: {path}")
        content = resolved.read_bytes()
        try:
            payload = yaml.safe_load(content.decode("utf-8-sig"))
        except UnicodeDecodeError as exc:
            raise ValueError(f"Search run is not valid UTF-8: {resolved}") from exc
        if not isinstance(payload, Mapping):
            raise ValueError(f"Search run must be a YAML mapping: {resolved}")
        loaded.append((resolved, payload, hashlib.sha256(content).hexdigest()))
    return loaded


def _split_query_hint(value: Any) -> Tuple[str, ...]:
    if value in (None, ""):
        return ()
    values = value if isinstance(value, list) else [value]
    result = []
    for item in values:
        for part in re.split(r"[,/\s]+", str(item).strip()):
            if part and part not in result:
                result.append(part)
    return tuple(result)


def _method_family(entity: Entity) -> Optional[str]:
    metadata = entity.metadata
    candidates = [metadata.get("method_family"), metadata.get("family")]
    taxonomy = metadata.get("taxonomy")
    if isinstance(taxonomy, Mapping):
        candidates.append(taxonomy.get("family"))
    sparsity = metadata.get("sparsity")
    if isinstance(sparsity, Mapping):
        candidates.append(sparsity.get("object"))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return "-".join(value.casefold().split())
    return None


def _context_bucket(value: Any) -> Optional[str]:
    try:
        length = int(value)
    except (TypeError, ValueError):
        return None
    if length < 8_192:
        return "<8K"
    if length < 32_768:
        return "8K-32K"
    if length < 65_536:
        return "32K-64K"
    return ">=64K"


def _metric_categories(entity: Entity) -> Tuple[str, ...]:
    metric = entity.metadata.get("metric")
    if not isinstance(metric, Mapping):
        return ()
    text = " ".join(
        str(value) for value in (metric.get("name"), metric.get("type")) if value
    ).casefold()
    categories = []
    if any(token in text for token in ("latency", "wall-clock", "time", "hour")):
        categories.append("latency")
    if any(token in text for token in ("throughput", "token/s", "tokens/s")):
        categories.append("throughput")
    if any(token in text for token in ("memory", "vram", "gpu ram")):
        categories.append("memory")
    if any(token in text for token in ("flop", "compute", "computation")):
        categories.append("flops")
    return tuple(categories)


def _best_facet_status(statuses: Iterable[str]) -> str:
    values = tuple(statuses)
    ranked = [value for value in values if value in FACET_STATUS_RANK]
    if ranked:
        return max(ranked, key=lambda value: FACET_STATUS_RANK[value])
    if "not-required" in values:
        return "not-required"
    return "missing"


def _entity_facets(entity: Entity) -> Tuple[str, ...]:
    values = entity.metadata.get("facets")
    if not isinstance(values, list):
        return ()
    return tuple(
        dict.fromkeys(
            str(value).strip().casefold()
            for value in values
            if isinstance(value, str) and value.strip()
        )
    )


def _assessment_contract(entity: Entity) -> Optional[NonConsensusAssessment]:
    if entity.entity_type != "assessment" or not entity.entity_id:
        return None
    fields = {
        "id": entity.entity_id,
        "question": entity.metadata.get("question"),
        "result": entity.metadata.get("result"),
        "claim_ids": entity.metadata.get("claim_ids", []),
        "evidence_ids": entity.metadata.get("evidence_ids", []),
        "method_family": entity.metadata.get("method_family"),
        "benchmark_ids": entity.metadata.get("benchmark_ids", []),
        "rationale": entity.metadata.get("rationale"),
        "verified": entity.metadata.get("verified", False),
    }
    try:
        return NonConsensusAssessment.model_validate(fields)
    except ValueError:
        return None


def inspect_research(
    settings: HarnessSettings,
    research_id: str,
) -> ResearchSnapshot:
    """Measure Wiki and search-run state without using an LLM or writing files."""

    research_directory = resolve_research_directory(settings, research_id)
    search_runs = _load_search_runs(research_directory)
    index = build_index(settings.wiki_root, settings.wiki_meta_root)
    diagnostics = validate_index(index)

    run_statuses: Counter[str] = Counter()
    query_statuses: Counter[str] = Counter()
    planned_query_ids: set[str] = set()
    blocked_query_ids: set[str] = set()
    candidates: Dict[str, Dict[str, Any]] = {}
    required_facets: set[str] = set()
    facet_statuses: Dict[str, List[str]] = defaultdict(list)
    facet_candidate_ids: Dict[str, set[str]] = defaultdict(set)
    facet_next_queries: Dict[str, set[str]] = defaultdict(set)
    search_yields: List[SearchYield] = []
    declared_search_gaps: set[str] = set()
    unresolved_scope_questions: set[str] = set()
    run_paths: List[str] = []

    for path, payload, _ in search_runs:
        relative_path = path.relative_to(settings.repository_root).as_posix()
        run_paths.append(relative_path)
        raw_header = payload.get("run")
        header: Mapping[str, Any] = (
            raw_header if isinstance(raw_header, Mapping) else {}
        )
        run_id = str(header.get("id") or path.stem)
        run_statuses[str(header.get("status") or "unknown")] += 1

        raw_scope = payload.get("scope")
        scope: Mapping[str, Any] = raw_scope if isinstance(raw_scope, Mapping) else {}
        required_facets.update(
            str(item) for item in scope.get("required_facets", []) if item
        )
        unresolved_scope_questions.update(
            str(item) for item in scope.get("unresolved_questions", []) if item
        )

        query_rounds: Dict[str, int] = {}
        round_query_statuses: Dict[int, Counter[str]] = defaultdict(Counter)
        round_retained_counts: Counter[int] = Counter()
        for query in payload.get("queries", []) or []:
            if not isinstance(query, Mapping):
                continue
            query_id = str(query.get("id") or "unknown")
            round_number = max(
                int(query.get("round") or header.get("round") or 1),
                1,
            )
            query_rounds[query_id] = round_number
            raw_execution = query.get("execution")
            execution: Mapping[str, Any] = (
                raw_execution if isinstance(raw_execution, Mapping) else {}
            )
            status = str(execution.get("status") or "unknown")
            query_statuses[status] += 1
            round_query_statuses[round_number][status] += 1
            round_retained_counts[round_number] += max(
                int(execution.get("retained_count") or 0),
                0,
            )
            if status == "planned":
                planned_query_ids.add(query_id)
            if status in {"blocked-credential", "failed"}:
                blocked_query_ids.add(query_id)

        run_candidates = [
            candidate
            for candidate in payload.get("candidates", []) or []
            if isinstance(candidate, Mapping)
        ]
        round_screening_flags: Dict[int, List[bool]] = defaultdict(list)
        for candidate in run_candidates:
            screened = (
                isinstance(candidate.get("relevance"), Mapping)
                and (candidate.get("relevance") or {}).get("label")
                in RELEVANCE_LABELS
            )
            discovered_rounds = {
                query_rounds[str(discovery.get("query_id"))]
                for discovery in candidate.get("discovered_by", []) or []
                if isinstance(discovery, Mapping)
                and str(discovery.get("query_id")) in query_rounds
            }
            for round_number in discovered_rounds:
                round_screening_flags[round_number].append(screened)

        def round_observation(
            round_number: int,
        ) -> tuple[bool, tuple[str, ...], Dict[str, int], bool]:
            statuses = round_query_statuses.get(round_number, Counter())
            successful_discovery = bool(
                statuses.get("succeeded") or statuses.get("empty")
            )
            terminal_nonfailure = bool(statuses) and all(
                status in {"succeeded", "empty", "skipped-duplicate"}
                for status in statuses
            )
            screening_flags = round_screening_flags.get(round_number, [])
            screening_complete = all(screening_flags) and (
                bool(screening_flags) or round_retained_counts[round_number] == 0
            )
            invalid_reasons = []
            if not successful_discovery:
                invalid_reasons.append("no-successful-provider-query")
            if not terminal_nonfailure:
                invalid_reasons.append("query-round-not-terminal-or-has-failure")
            if not screening_complete:
                invalid_reasons.append("candidate-screening-incomplete")
            if statuses.get("blocked-credential"):
                invalid_reasons.append("provider-credential-blocked")
            return (
                not invalid_reasons,
                tuple(invalid_reasons),
                dict(sorted(statuses.items())),
                screening_complete,
            )

        for position, candidate in enumerate(run_candidates):
            if not isinstance(candidate, Mapping):
                continue
            candidate_id = str(candidate.get("candidate_id") or f"@{run_id}:{position}")
            record = candidates.setdefault(
                candidate_id,
                {"labels": set(), "selected": False, "staged": False},
            )
            relevance = candidate.get("relevance")
            label = relevance.get("label") if isinstance(relevance, Mapping) else None
            if label:
                record["labels"].add(str(label))
            if candidate.get("review_state") == "selected-for-ingest":
                record["selected"] = True
            if candidate.get("review_state") == "staged-for-wiki":
                record["staged"] = True

        raw_coverage = payload.get("coverage")
        coverage: Mapping[str, Any] = (
            raw_coverage if isinstance(raw_coverage, Mapping) else {}
        )
        for item in coverage.get("facets", []) or []:
            if not isinstance(item, Mapping) or not item.get("name"):
                continue
            name = str(item["name"])
            facet_statuses[name].append(str(item.get("status") or "missing"))
            facet_candidate_ids[name].update(
                str(candidate_id)
                for candidate_id in item.get("candidate_ids", []) or []
                if candidate_id
            )
            facet_next_queries[name].update(_split_query_hint(item.get("next_query")))
        declared_search_gaps.update(
            str(item) for item in coverage.get("gaps", []) or [] if item
        )

        raw_metrics = coverage.get("metrics")
        metrics: Mapping[str, Any] = (
            raw_metrics if isinstance(raw_metrics, Mapping) else {}
        )
        raw_yields = metrics.get("new_core_by_round", []) or []
        yield_counts: Counter[int] = Counter()
        for item in raw_yields:
            if not isinstance(item, Mapping):
                continue
            round_number = max(
                int(item.get("round") or header.get("round") or 1),
                1,
            )
            yield_counts[round_number] += max(int(item.get("count") or 0), 0)
        observed_rounds = {
            round_number
            for round_number, statuses in round_query_statuses.items()
            if any(
                statuses.get(status)
                for status in {
                    "succeeded",
                    "empty",
                    "failed",
                    "blocked-credential",
                }
            )
        }
        for round_number in sorted(set(yield_counts) | observed_rounds):
            (
                valid_discovery_round,
                invalid_reasons,
                round_statuses,
                screening_complete,
            ) = round_observation(round_number)
            search_yields.append(
                SearchYield(
                    run_id=run_id,
                    round=round_number,
                    new_core_papers=yield_counts[round_number],
                    valid_discovery_round=valid_discovery_round,
                    invalid_reasons=invalid_reasons,
                    query_statuses=round_statuses,
                    screening_complete=screening_complete,
                )
            )

    relevance_counts: Counter[str] = Counter()
    selected_for_ingest = 0
    staged_for_wiki = 0
    for record in candidates.values():
        labels = record["labels"]
        if not labels:
            relevance_counts["untriaged"] += 1
        elif len(labels) == 1:
            relevance_counts[next(iter(labels))] += 1
        else:
            relevance_counts["conflict"] += 1
        selected_for_ingest += bool(record["selected"])
        staged_for_wiki += bool(record["staged"])
    for label in (*RELEVANCE_LABELS, "untriaged", "conflict"):
        relevance_counts.setdefault(label, 0)

    unique_entities = index.unique_entities()
    entities = tuple(unique_entities.values())
    papers = tuple(entity for entity in entities if entity.entity_type == "paper")
    methods = tuple(
        entity
        for entity in entities
        if entity.entity_type == "method"
        or (entity.entity_type == "concept" and entity.metadata.get("kind") == "method")
    )
    experiments = tuple(
        entity for entity in entities if entity.entity_type == "experiment"
    )
    claims = tuple(entity for entity in entities if entity.entity_type == "claim")
    assessments = tuple(
        entity for entity in entities if entity.entity_type == "assessment"
    )
    benchmarks = tuple(
        entity for entity in entities if entity.entity_type == "benchmark"
    )
    models = tuple(entity for entity in entities if entity.entity_type == "model")

    method_families: Counter[str] = Counter()
    unclassified_methods = 0
    for entity in methods:
        family = _method_family(entity)
        if family:
            method_families[family] += 1
        else:
            unclassified_methods += 1

    experiments_with_locator = sum(
        isinstance(entity.metadata.get("evidence"), Mapping)
        and bool(entity.metadata["evidence"].get("locator"))
        for entity in experiments
    )
    context_lengths: Counter[str] = Counter({name: 0 for name in CONTEXT_BUCKETS})
    engineering_metrics: Counter[str] = Counter(
        {name: 0 for name in ENGINEERING_METRICS}
    )
    for entity in experiments:
        bucket = _context_bucket(entity.metadata.get("context_length"))
        if bucket:
            context_lengths[bucket] += 1
        for category in _metric_categories(entity):
            engineering_metrics[category] += 1

    claim_assessments: Counter[str] = Counter(
        str(entity.metadata.get("assessment") or "unknown") for entity in claims
    )
    known_claim_ids = {entity.entity_id for entity in claims if entity.entity_id}
    evidence_claim_ids = {
        edge.target
        for edge in index.edges
        if edge.relation in {"supports", "contradicts"}
    } & known_claim_ids
    contradicted_claim_ids = {
        edge.target for edge in index.edges if edge.relation == "contradicts"
    } & known_claim_ids
    contested_claim_ids = {
        entity.entity_id
        for entity in claims
        if entity.metadata.get("assessment") == "contested"
    } | contradicted_claim_ids

    valid_assessments = tuple(
        (entity, contract)
        for entity in assessments
        if (contract := _assessment_contract(entity)) is not None
    )
    assessment_results: Counter[str] = Counter(
        contract.result for _, contract in valid_assessments
    )

    evidence_facet_verified_ids: Dict[str, set[str]] = defaultdict(set)
    evidence_facet_draft_ids: Dict[str, set[str]] = defaultdict(set)
    for entity in (*papers, *experiments, *claims):
        if not entity.entity_id:
            continue
        status = str(entity.metadata.get("status") or "")
        for facet in _entity_facets(entity):
            if status == "verified":
                evidence_facet_verified_ids[facet].add(entity.entity_id)
            elif status in {"draft", "needs-review"}:
                evidence_facet_draft_ids[facet].add(entity.entity_id)

    model_families: Counter[str] = Counter()
    for entity in models:
        family = str(entity.metadata.get("family") or "unclassified").strip().casefold()
        model_families[family or "unclassified"] += 1

    diagnostic_codes = Counter(item.code for item in diagnostics)
    revision_candidates = evidence_revision_candidates(index)
    revision_exhausted = evidence_revision_exhausted(index)
    search_hashes = [
        {
            "path": path.relative_to(settings.repository_root).as_posix(),
            "sha256": digest,
        }
        for path, _, digest in search_runs
    ]
    snapshot_material = json.dumps(
        {"wiki_source_hash": index.source_hash, "search_runs": search_hashes},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    snapshot_id = hashlib.sha256(snapshot_material).hexdigest()

    candidate_facet_coverage = {
        name: _best_facet_status(facet_statuses.get(name, ["missing"]))
        for name in sorted(required_facets | set(facet_statuses))
    }
    all_facets = set(candidate_facet_coverage)
    all_facets.update(evidence_facet_verified_ids)
    all_facets.update(evidence_facet_draft_ids)
    evidence_facet_coverage = {
        name: (
            "covered"
            if evidence_facet_verified_ids.get(name)
            else "partial" if evidence_facet_draft_ids.get(name) else "missing"
        )
        for name in sorted(all_facets)
    }
    candidate_facet_coverage = {
        name: candidate_facet_coverage.get(name, "missing")
        for name in sorted(all_facets)
    }
    corpus = CorpusSnapshot(
        search_run_count=len(search_runs),
        search_runs_by_status=dict(sorted(run_statuses.items())),
        query_count=sum(query_statuses.values()),
        query_statuses=dict(sorted(query_statuses.items())),
        pending_queries=query_statuses.get("planned", 0),
        blocked_queries=sum(
            query_statuses.get(status, 0) for status in ("blocked-credential", "failed")
        ),
        planned_query_ids=tuple(sorted(planned_query_ids)),
        blocked_query_ids=tuple(sorted(blocked_query_ids)),
        unique_candidates=len(candidates),
        candidates_by_relevance=dict(sorted(relevance_counts.items())),
        core_candidates=relevance_counts.get("core", 0),
        selected_for_ingest=selected_for_ingest,
        staged_for_wiki=staged_for_wiki,
        ingested_papers=len(papers),
        verified_papers=sum(
            entity.metadata.get("status") == "verified" for entity in papers
        ),
        search_run_paths=tuple(run_paths),
        search_yields=tuple(search_yields),
        declared_search_gaps=tuple(sorted(declared_search_gaps)),
    )
    taxonomy = TaxonomySnapshot(
        required_facets=tuple(sorted(required_facets)),
        candidate_facet_coverage=candidate_facet_coverage,
        candidate_facet_counts={
            name: len(facet_candidate_ids.get(name, set()))
            for name in sorted(all_facets)
        },
        evidence_facet_coverage=evidence_facet_coverage,
        evidence_facet_counts={
            name: len(evidence_facet_verified_ids.get(name, set()))
            for name in sorted(all_facets)
        },
        facet_next_queries={
            name: tuple(sorted(facet_next_queries.get(name, set())))
            for name in sorted(all_facets)
        },
        method_entities=len(methods),
        method_families=dict(sorted(method_families.items())),
        unclassified_methods=unclassified_methods,
        unresolved_scope_questions=tuple(sorted(unresolved_scope_questions)),
    )
    evidence = EvidenceSnapshot(
        experiments_total=len(experiments),
        verified_experiments=sum(
            entity.metadata.get("status") == "verified" for entity in experiments
        ),
        experiments_with_evidence_locator=experiments_with_locator,
        evidence_locator_ratio=(
            round(experiments_with_locator / len(experiments), 6)
            if experiments
            else 0.0
        ),
        claims_total=len(claims),
        verified_claims=sum(
            entity.metadata.get("status") == "verified" for entity in claims
        ),
        claims_with_evidence=sum(
            bool(entity.entity_id and entity.entity_id in evidence_claim_ids)
            for entity in claims
        ),
        claims_by_assessment=dict(sorted(claim_assessments.items())),
        contested_claims=len(contested_claim_ids),
        nonconsensus_assessments=len(assessments),
        verified_nonconsensus_assessments=sum(
            entity.metadata.get("status") == "verified" and contract.verified
            for entity, contract in valid_assessments
        ),
        assessments_by_result=dict(sorted(assessment_results.items())),
        benchmarks_total=len(benchmarks),
        benchmark_ids=tuple(
            sorted(entity.entity_id for entity in benchmarks if entity.entity_id)
        ),
        models_total=len(models),
        model_families=dict(sorted(model_families.items())),
        context_length_buckets=dict(context_lengths),
        engineering_metrics=dict(engineering_metrics),
        revision_candidates=len(revision_candidates),
        revision_candidate_ids=tuple(
            str(entity.entity_id) for entity in revision_candidates if entity.entity_id
        ),
        revision_exhausted=len(revision_exhausted),
        revision_exhausted_ids=tuple(
            str(entity.entity_id) for entity in revision_exhausted if entity.entity_id
        ),
    )
    quality = QualitySnapshot(
        schema_errors=sum(item.severity == "ERROR" for item in diagnostics),
        schema_warnings=sum(item.severity == "WARNING" for item in diagnostics),
        diagnostic_codes=dict(sorted(diagnostic_codes.items())),
        duplicate_entity_ids=len(index.resolver.duplicate_ids()),
        unresolved_wikilinks=sum(link.target is None for link in index.links),
    )
    return ResearchSnapshot(
        snapshot_id=snapshot_id,
        research_id=research_id,
        wiki_source_hash=index.source_hash,
        corpus=corpus,
        taxonomy=taxonomy,
        evidence=evidence,
        quality=quality,
    )


def _gap_id(gap_type: str, key: str) -> str:
    digest = hashlib.sha256(f"{gap_type}:{key}".encode("utf-8")).hexdigest()[:12]
    return f"gap-{digest}"


def _gap(
    *,
    gap_type: str,
    key: str,
    question: str,
    priority: float,
    reasons: Sequence[str],
    recommended_action: str,
    evidence: Optional[Mapping[str, Sequence[str]]] = None,
    search_focus: Sequence[str] = (),
    blocking: bool = False,
) -> ResearchGap:
    return ResearchGap(
        id=_gap_id(gap_type, key),
        key=key,
        type=gap_type,
        question=question,
        priority=priority,
        reasons=tuple(reasons),
        evidence={
            str(name): tuple(str(item) for item in values)
            for name, values in (evidence or {}).items()
        },
        recommended_action=recommended_action,
        search_focus=tuple(search_focus),
        blocking=blocking,
    )


def evaluate_gaps(
    snapshot: ResearchSnapshot,
    criteria: DoneCriteria,
) -> Tuple[ResearchGap, ...]:
    """Generate measurable gap candidates; semantic ranking can be added later."""

    gaps: List[ResearchGap] = []
    for facet, required_status in criteria.facet_requirements.items():
        required_rank = FACET_STATUS_RANK[required_status]
        candidate_status = snapshot.taxonomy.candidate_facet_coverage.get(
            facet, "missing"
        )
        evidence_status = snapshot.taxonomy.evidence_facet_coverage.get(
            facet, "missing"
        )
        if FACET_STATUS_RANK.get(evidence_status, -1) >= required_rank:
            continue
        if evidence_status == "partial":
            action = "verify"
        elif (
            FACET_STATUS_RANK.get(candidate_status, -1) >= FACET_STATUS_RANK["partial"]
        ):
            action = "ingest"
        else:
            action = "search"
        gaps.append(
            _gap(
                gap_type="coverage_gap",
                key=f"facet:{facet}",
                question=f"What verified evidence is needed to cover the research facet {facet}?",
                priority=0.95 if evidence_status == "missing" else 0.82,
                reasons=(
                    f"Candidate coverage is {candidate_status} with {snapshot.taxonomy.candidate_facet_counts.get(facet, 0)} linked candidates.",
                    f"Evidence coverage is {evidence_status} with {snapshot.taxonomy.evidence_facet_counts.get(facet, 0)} verified Wiki entities; required status is {required_status}.",
                ),
                recommended_action=action,
                evidence={
                    "facet": (facet,),
                    "candidate_coverage": (candidate_status,),
                    "evidence_coverage": (evidence_status,),
                    "required_evidence_coverage": (required_status,),
                },
                search_focus=(
                    facet,
                    *snapshot.taxonomy.facet_next_queries.get(facet, ()),
                ),
                blocking=True,
            )
        )

    if snapshot.corpus.pending_queries:
        gaps.append(
            _gap(
                gap_type="workflow_gap",
                key="planned-search-queries",
                question="What does the already planned discovery run retrieve?",
                priority=0.99,
                reasons=(
                    f"{snapshot.corpus.pending_queries} planned queries have not executed.",
                    "Coverage cannot be evaluated from an unexecuted discovery plan.",
                ),
                recommended_action="search",
                evidence={"planned_query_ids": snapshot.corpus.planned_query_ids},
                search_focus=snapshot.corpus.planned_query_ids,
            )
        )
    if snapshot.corpus.selected_for_ingest:
        gaps.append(
            _gap(
                gap_type="workflow_gap",
                key="selected-papers-pending-ingest",
                question="Which selected candidate papers should be ingested next?",
                priority=0.97,
                reasons=(
                    f"{snapshot.corpus.selected_for_ingest} candidates are selected for ingest.",
                ),
                recommended_action="ingest",
            )
        )
    if snapshot.evidence.revision_candidates:
        gaps.append(
            _gap(
                gap_type="evidence_gap",
                key="verifier-retained-evidence-revision",
                question=(
                    "Which verifier-retained method or claim should be corrected "
                    "from its source before independent re-verification?"
                ),
                priority=0.965,
                reasons=(
                    f"{snapshot.evidence.revision_candidates} entities have "
                    "actionable verifier feedback.",
                    "Revision is bounded to source contradictions or locator "
                    "defects and cannot mark an entity verified.",
                ),
                recommended_action="revise_evidence",
                evidence={"entity_ids": snapshot.evidence.revision_candidate_ids},
                blocking=True,
            )
        )
    if snapshot.evidence.revision_exhausted:
        gaps.append(
            _gap(
                gap_type="evidence_gap",
                key="evidence-revision-budget-exhausted",
                question=(
                    "Which repeatedly unresolved evidence records require human review "
                    "or a new source?"
                ),
                priority=0.96,
                reasons=(
                    f"{snapshot.evidence.revision_exhausted} entities reached "
                    "the two-revision limit.",
                ),
                recommended_action="synthesize",
                evidence={"entity_ids": snapshot.evidence.revision_exhausted_ids},
                blocking=True,
            )
        )
    if snapshot.corpus.core_candidates < criteria.minimum_core_candidates:
        gaps.append(
            _gap(
                gap_type="coverage_gap",
                key="minimum-core-candidates",
                question="Which additional core papers are required for adequate coverage?",
                priority=0.94,
                reasons=(
                    f"Core candidates: {snapshot.corpus.core_candidates}; required: {criteria.minimum_core_candidates}.",
                ),
                recommended_action="search",
            )
        )
    family_count = len(snapshot.taxonomy.method_families)
    if family_count < criteria.minimum_method_families:
        gaps.append(
            _gap(
                gap_type="coverage_gap",
                key="method-family-coverage",
                question="Which sparse long-context method families remain unrepresented?",
                priority=0.9,
                reasons=(
                    f"Classified method families: {family_count}; required: {criteria.minimum_method_families}.",
                    f"Unclassified method entities: {snapshot.taxonomy.unclassified_methods}.",
                ),
                recommended_action=(
                    "ingest"
                    if snapshot.corpus.selected_for_ingest
                    or snapshot.corpus.core_candidates > snapshot.corpus.ingested_papers
                    else "search"
                ),
                evidence={
                    "known_families": tuple(snapshot.taxonomy.method_families),
                },
                search_focus=("technical taxonomy", "sparse attention method families"),
            )
        )
    if snapshot.corpus.ingested_papers < criteria.minimum_ingested_papers:
        gaps.append(
            _gap(
                gap_type="workflow_gap",
                key="minimum-ingested-papers",
                question="Which core candidates should be converted into structured Wiki papers?",
                priority=0.89,
                reasons=(
                    f"Ingested papers: {snapshot.corpus.ingested_papers}; required: {criteria.minimum_ingested_papers}.",
                ),
                recommended_action=(
                    "ingest"
                    if snapshot.corpus.selected_for_ingest
                    or snapshot.corpus.core_candidates > snapshot.corpus.ingested_papers
                    else "search"
                ),
            )
        )
    if snapshot.corpus.verified_papers < criteria.minimum_verified_papers:
        gaps.append(
            _gap(
                gap_type="evidence_gap",
                key="minimum-verified-papers",
                question="Which ingested papers still require evidence verification?",
                priority=0.82,
                reasons=(
                    f"Verified papers: {snapshot.corpus.verified_papers}; required: {criteria.minimum_verified_papers}.",
                ),
                recommended_action="verify",
            )
        )
    if snapshot.evidence.experiments_total < criteria.minimum_experiments:
        gaps.append(
            _gap(
                gap_type="evidence_gap",
                key="minimum-structured-experiments",
                question="Which quantitative experiments should be extracted as structured evidence?",
                priority=0.91,
                reasons=(
                    f"Structured experiments: {snapshot.evidence.experiments_total}; required: {criteria.minimum_experiments}.",
                ),
                recommended_action=(
                    "ingest" if snapshot.corpus.selected_for_ingest else "verify"
                ),
            )
        )
    if snapshot.evidence.verified_claims < criteria.minimum_verified_claims:
        gaps.append(
            _gap(
                gap_type="evidence_gap",
                key="minimum-verified-claims",
                question="Which key claims still lack verified experiment evidence?",
                priority=0.88,
                reasons=(
                    f"Verified claims: {snapshot.evidence.verified_claims}; required: {criteria.minimum_verified_claims}.",
                    f"Claims with evidence edges: {snapshot.evidence.claims_with_evidence}.",
                ),
                recommended_action="verify",
            )
        )
    if (
        criteria.require_nonconsensus_review
        and snapshot.evidence.verified_nonconsensus_assessments
        < criteria.minimum_verified_nonconsensus_assessments
    ):
        gaps.append(
            _gap(
                gap_type="contradiction_gap",
                key="nonconsensus-review",
                question="Which non-consensus questions still need a verified assessment?",
                priority=0.86,
                reasons=(
                    f"Verified non-consensus assessments: {snapshot.evidence.verified_nonconsensus_assessments}; required: {criteria.minimum_verified_nonconsensus_assessments}.",
                    f"Draft or unverified assessments: {snapshot.evidence.nonconsensus_assessments - snapshot.evidence.verified_nonconsensus_assessments}.",
                    "A supported consensus, contested result, or insufficient-evidence result is valid when evidence-grounded.",
                ),
                recommended_action=(
                    "verify"
                    if snapshot.evidence.nonconsensus_assessments
                    > snapshot.evidence.verified_nonconsensus_assessments
                    else (
                        "analyze_claims"
                        if snapshot.evidence.verified_claims
                        and snapshot.evidence.verified_experiments
                        else "verify"
                    )
                ),
                search_focus=(
                    "limitations",
                    "counter evidence",
                    "strong dense baselines",
                ),
                blocking=True,
            )
        )
    if (
        snapshot.evidence.experiments_total
        and snapshot.evidence.evidence_locator_ratio
        < criteria.minimum_evidence_locator_ratio
    ):
        gaps.append(
            _gap(
                gap_type="evidence_gap",
                key="evidence-locator-ratio",
                question="Which experiments lack precise source locations?",
                priority=0.93,
                reasons=(
                    f"Evidence locator ratio: {snapshot.evidence.evidence_locator_ratio:.3f}; required: {criteria.minimum_evidence_locator_ratio:.3f}.",
                ),
                recommended_action="verify",
            )
        )
    for bucket, required_count in criteria.context_bucket_requirements.items():
        current_count = snapshot.evidence.context_length_buckets.get(bucket, 0)
        if current_count >= required_count:
            continue
        gaps.append(
            _gap(
                gap_type="context_gap",
                key=f"context:{bucket}",
                question=f"What controlled evidence exists at context length {bucket}?",
                priority=0.92 if bucket == ">=64K" else 0.84,
                reasons=(
                    f"Structured experiments in {bucket}: {current_count}; required: {required_count}.",
                ),
                recommended_action="search",
                search_focus=(bucket, "long context", "controlled experiment"),
                blocking=True,
            )
        )
    for metric, required_count in criteria.engineering_metric_requirements.items():
        current_count = snapshot.evidence.engineering_metrics.get(metric, 0)
        if current_count >= required_count:
            continue
        gaps.append(
            _gap(
                gap_type="engineering_gap",
                key=f"engineering:{metric}",
                question=f"What measured {metric} evidence exists for sparse long-context methods?",
                priority=0.9,
                reasons=(
                    f"Structured {metric} experiments: {current_count}; required: {required_count}.",
                ),
                recommended_action="search",
                search_focus=(metric, "sparse attention", "long context", "hardware"),
                blocking=True,
            )
        )
    if snapshot.evidence.benchmarks_total == 0:
        gaps.append(
            _gap(
                gap_type="benchmark_gap",
                key="structured-benchmarks",
                question="Which long-context benchmarks need canonical Wiki entities?",
                priority=0.81,
                reasons=("No structured benchmark entity exists in the Wiki.",),
                recommended_action="ingest",
                search_focus=("LongBench", "RULER", "long-document tasks"),
            )
        )
    if snapshot.quality.schema_errors > criteria.maximum_schema_errors:
        gaps.append(
            _gap(
                gap_type="schema_gap",
                key="schema-errors",
                question="Which Wiki schema errors must be repaired before completion?",
                priority=1.0,
                reasons=(
                    f"Schema errors: {snapshot.quality.schema_errors}; allowed: {criteria.maximum_schema_errors}.",
                ),
                recommended_action="verify",
                evidence={"diagnostic_codes": tuple(snapshot.quality.diagnostic_codes)},
                blocking=True,
            )
        )
    elif snapshot.quality.schema_warnings:
        gaps.append(
            _gap(
                gap_type="schema_gap",
                key="schema-warnings",
                question="Which compatibility warnings should be resolved before final reporting?",
                priority=0.62,
                reasons=(
                    f"Wiki validation reports {snapshot.quality.schema_warnings} warnings.",
                ),
                recommended_action="verify",
                evidence={"diagnostic_codes": tuple(snapshot.quality.diagnostic_codes)},
            )
        )

    return tuple(sorted(gaps, key=lambda item: (-item.priority, item.id)))


def check_done(
    snapshot: ResearchSnapshot,
    criteria: DoneCriteria,
    gaps: Sequence[ResearchGap],
    *,
    research_iteration: int,
    tool_calls: int = 0,
    no_progress_rounds: int = 0,
    attempts_by_gap_action: Optional[Mapping[str, Mapping[str, Any]]] = None,
    supported_actions: Optional[AbstractSet[ResearchAction]] = None,
) -> DoneCheck:
    """Apply hard coverage, quality, saturation, and budget gates."""

    # Retained for CLI/checkpoint compatibility. Global stopping is intentionally
    # based on the action frontier below, not the last attempted pair's counter.
    del no_progress_rounds

    coverage_failures = []
    for facet, required_status in criteria.facet_requirements.items():
        required_rank = FACET_STATUS_RANK[required_status]
        status = snapshot.taxonomy.evidence_facet_coverage.get(facet, "missing")
        if FACET_STATUS_RANK.get(status, -1) < required_rank:
            coverage_failures.append(
                f"evidence facet {facet} is {status}, requires {required_status}"
            )
    classified_families = len(snapshot.taxonomy.method_families)
    if classified_families < criteria.minimum_method_families:
        coverage_failures.append(
            f"method families {classified_families} < {criteria.minimum_method_families}"
        )
    if snapshot.corpus.core_candidates < criteria.minimum_core_candidates:
        coverage_failures.append(
            f"core candidates {snapshot.corpus.core_candidates} < {criteria.minimum_core_candidates}"
        )
    if snapshot.corpus.ingested_papers < criteria.minimum_ingested_papers:
        coverage_failures.append(
            f"ingested papers {snapshot.corpus.ingested_papers} < {criteria.minimum_ingested_papers}"
        )

    quality_failures = []
    checks = (
        (
            snapshot.corpus.verified_papers,
            criteria.minimum_verified_papers,
            "verified papers",
        ),
        (
            snapshot.evidence.experiments_total,
            criteria.minimum_experiments,
            "experiments",
        ),
        (
            snapshot.evidence.verified_claims,
            criteria.minimum_verified_claims,
            "verified claims",
        ),
    )
    for current, required, label in checks:
        if current < required:
            quality_failures.append(f"{label} {current} < {required}")
    if (
        snapshot.evidence.evidence_locator_ratio
        < criteria.minimum_evidence_locator_ratio
    ):
        quality_failures.append(
            f"evidence locator ratio {snapshot.evidence.evidence_locator_ratio:.3f} "
            f"< {criteria.minimum_evidence_locator_ratio:.3f}"
        )
    if snapshot.quality.schema_errors > criteria.maximum_schema_errors:
        quality_failures.append(
            f"schema errors {snapshot.quality.schema_errors} > {criteria.maximum_schema_errors}"
        )
    if (
        criteria.require_nonconsensus_review
        and snapshot.evidence.verified_nonconsensus_assessments
        < criteria.minimum_verified_nonconsensus_assessments
    ):
        quality_failures.append(
            "verified non-consensus assessments "
            f"{snapshot.evidence.verified_nonconsensus_assessments} < "
            f"{criteria.minimum_verified_nonconsensus_assessments}"
        )
    for bucket, required_count in criteria.context_bucket_requirements.items():
        current_count = snapshot.evidence.context_length_buckets.get(bucket, 0)
        if current_count < required_count:
            quality_failures.append(
                f"context bucket {bucket} experiments {current_count} < {required_count}"
            )
    for metric, required_count in criteria.engineering_metric_requirements.items():
        current_count = snapshot.evidence.engineering_metrics.get(metric, 0)
        if current_count < required_count:
            quality_failures.append(
                f"engineering metric {metric} experiments {current_count} < {required_count}"
            )

    yields = tuple(
        item
        for item in snapshot.corpus.search_yields
        if item.valid_discovery_round
    )
    saturation_passed = False
    saturation_failure = ""
    if len(yields) < criteria.minimum_completed_search_rounds:
        saturation_failure = (
            f"valid completed discovery rounds {len(yields)} "
            f"< {criteria.minimum_completed_search_rounds}"
        )
    else:
        window = yields[-criteria.saturation_window :]
        if all(
            item.new_core_papers <= criteria.saturation_novelty_threshold
            for item in window
        ):
            saturation_passed = True
        else:
            saturation_failure = (
                "recent search novelty exceeds saturation threshold "
                f"{criteria.saturation_novelty_threshold}"
            )

    coverage_passed = not coverage_failures
    quality_passed = not quality_failures
    blocking_gap_ids = tuple(
        sorted(gap.id for gap in gaps if gap.blocking and gap.status == "open")
    )
    blocking_gaps_passed = (
        not criteria.require_no_open_blocking_gaps or not blocking_gap_ids
    )
    criteria_active = criteria.status == "active"
    complete = (
        criteria_active
        and coverage_passed
        and quality_passed
        and saturation_passed
        and blocking_gaps_passed
    )

    budget_hits = []
    if research_iteration >= criteria.max_research_iterations:
        budget_hits.append("max_research_iterations")
    if snapshot.corpus.search_run_count >= criteria.max_search_runs:
        budget_hits.append("max_search_runs")
    if snapshot.corpus.ingested_papers >= criteria.max_ingested_papers:
        budget_hits.append("max_ingested_papers")
    if tool_calls >= criteria.max_tool_calls:
        budget_hits.append("max_tool_calls")
    budget_exhausted = bool(budget_hits)

    failures = [*coverage_failures, *quality_failures]
    if saturation_failure:
        failures.append(saturation_failure)
    if not blocking_gaps_passed:
        failures.append("open blocking gaps: " + ", ".join(blocking_gap_ids))
    if not criteria_active:
        failures.append(
            "done criteria status is draft; automatic completion is disabled"
        )

    stop_reason: Optional[StopReason] = None
    if complete:
        stop_reason = "completed"
    elif budget_exhausted:
        stop_reason = "budget_exhausted"
    elif (
        snapshot.corpus.query_count > 0
        and snapshot.corpus.blocked_queries == snapshot.corpus.query_count
        and snapshot.corpus.selected_for_ingest == 0
    ):
        stop_reason = "blocked"
        failures.append("all known search queries are blocked")
    elif supported_actions is not None:
        open_gaps = tuple(gap for gap in gaps if gap.status == "open")
        attempts = attempts_by_gap_action or {}
        supported_pairs = []
        eligible_pairs = []
        exhausted_pairs = []
        unsupported_pairs = []
        for gap in open_gaps:
            attempt_key = f"{gap.id}:{gap.recommended_action}"
            if gap.recommended_action not in supported_actions:
                unsupported_pairs.append(attempt_key)
                continue
            supported_pairs.append(attempt_key)
            raw_stats = attempts.get(attempt_key)
            if raw_stats is None:
                eligible_pairs.append(attempt_key)
                continue
            stats = ActionAttemptStats.model_validate(raw_stats)
            if stats.no_progress >= criteria.max_no_progress_rounds:
                exhausted_pairs.append(attempt_key)
            else:
                eligible_pairs.append(attempt_key)
        if open_gaps and not eligible_pairs:
            if supported_pairs:
                stop_reason = "stalled"
                failures.append(
                    "all supported open gap/action pairs are exhausted: "
                    + ", ".join(exhausted_pairs)
                )
            else:
                stop_reason = "blocked"
                failures.append(
                    "no open gap has an available executor: "
                    + ", ".join(unsupported_pairs)
                )

    return DoneCheck(
        complete=complete,
        coverage_passed=coverage_passed,
        quality_passed=quality_passed,
        saturation_passed=saturation_passed,
        blocking_gaps_passed=blocking_gaps_passed,
        blocking_gap_ids=blocking_gap_ids,
        budget_exhausted=budget_exhausted,
        stop_reason=stop_reason,
        failures=tuple(failures),
        budget_hits=tuple(budget_hits),
    )


def measure_progress(
    before: Optional[ResearchSnapshot],
    after: ResearchSnapshot,
    *,
    previous_no_progress_rounds: int = 0,
    action_attempted: bool = False,
) -> ProgressMeasurement:
    """Measure structured novelty between two snapshots without an LLM."""

    if before is None:
        return ProgressMeasurement(
            baseline=True,
            action_attempted=action_attempted,
            changed=False,
            deltas={},
            progress_score=0.0,
            made_progress=False,
            no_progress_rounds=previous_no_progress_rounds,
            changed_sources=(),
        )

    before_metrics = {
        "unique_candidates": before.corpus.unique_candidates,
        "core_candidates": before.corpus.core_candidates,
        "selected_for_ingest": before.corpus.selected_for_ingest,
        "ingested_papers": before.corpus.ingested_papers,
        "verified_papers": before.corpus.verified_papers,
        "method_families": len(before.taxonomy.method_families),
        "evidence_facets_covered": sum(
            status == "covered"
            for status in before.taxonomy.evidence_facet_coverage.values()
        ),
        "experiments": before.evidence.experiments_total,
        "verified_claims": before.evidence.verified_claims,
        "contested_claims": before.evidence.contested_claims,
        "nonconsensus_assessments": before.evidence.nonconsensus_assessments,
        "verified_nonconsensus_assessments": before.evidence.verified_nonconsensus_assessments,
        "evidence_locators": before.evidence.experiments_with_evidence_locator,
        "benchmarks": before.evidence.benchmarks_total,
        "revision_candidates": before.evidence.revision_candidates,
    }
    after_metrics = {
        "unique_candidates": after.corpus.unique_candidates,
        "core_candidates": after.corpus.core_candidates,
        "selected_for_ingest": after.corpus.selected_for_ingest,
        "ingested_papers": after.corpus.ingested_papers,
        "verified_papers": after.corpus.verified_papers,
        "method_families": len(after.taxonomy.method_families),
        "evidence_facets_covered": sum(
            status == "covered"
            for status in after.taxonomy.evidence_facet_coverage.values()
        ),
        "experiments": after.evidence.experiments_total,
        "verified_claims": after.evidence.verified_claims,
        "contested_claims": after.evidence.contested_claims,
        "nonconsensus_assessments": after.evidence.nonconsensus_assessments,
        "verified_nonconsensus_assessments": after.evidence.verified_nonconsensus_assessments,
        "evidence_locators": after.evidence.experiments_with_evidence_locator,
        "benchmarks": after.evidence.benchmarks_total,
        "revision_candidates": after.evidence.revision_candidates,
    }
    deltas = {
        name: after_metrics[name] - before_metrics[name] for name in before_metrics
    }
    weights = {
        "unique_candidates": 0.5,
        "core_candidates": 1.0,
        "selected_for_ingest": 0.5,
        "ingested_papers": 1.0,
        "verified_papers": 2.0,
        "method_families": 3.0,
        "evidence_facets_covered": 2.0,
        "experiments": 1.0,
        "verified_claims": 2.0,
        "contested_claims": 3.0,
        "nonconsensus_assessments": 1.5,
        "verified_nonconsensus_assessments": 3.0,
        "evidence_locators": 1.0,
        "benchmarks": 1.0,
        "revision_candidates": 0.0,
    }
    progress_score = sum(
        max(deltas[name], 0) * weight for name, weight in weights.items()
    )
    progress_score += max(-deltas["selected_for_ingest"], 0) * 0.5
    progress_score += max(-deltas["revision_candidates"], 0) * 1.5
    made_progress = progress_score > 0
    if action_attempted:
        no_progress_rounds = 0 if made_progress else previous_no_progress_rounds + 1
    else:
        no_progress_rounds = previous_no_progress_rounds
    changed_sources = []
    if before.wiki_source_hash != after.wiki_source_hash:
        changed_sources.append("wiki")
    if (
        before.corpus.search_run_paths != after.corpus.search_run_paths
        or before.corpus.query_statuses != after.corpus.query_statuses
        or before.corpus.unique_candidates != after.corpus.unique_candidates
        or before.corpus.selected_for_ingest != after.corpus.selected_for_ingest
        or before.corpus.search_yields != after.corpus.search_yields
    ):
        changed_sources.append("search-runs")
    return ProgressMeasurement(
        baseline=False,
        action_attempted=action_attempted,
        changed=before.snapshot_id != after.snapshot_id,
        deltas=deltas,
        progress_score=float(progress_score),
        made_progress=made_progress,
        no_progress_rounds=no_progress_rounds,
        changed_sources=tuple(changed_sources),
    )


def decide_next_action(
    gaps: Sequence[ResearchGap],
    done: DoneCheck,
    *,
    attempts_by_gap_action: Optional[Mapping[str, Mapping[str, Any]]] = None,
    max_no_progress_per_gap_action: Optional[int] = None,
    supported_actions: Optional[AbstractSet[ResearchAction]] = None,
) -> ResearchDecision:
    """Choose from a finite action set while avoiding exhausted gap/action pairs."""

    if done.complete:
        return ResearchDecision(
            action="finish",
            reason="Coverage, quality, saturation, and active DoneCriteria all passed.",
            expected_information_gain=0.0,
        )
    if done.stop_reason is not None:
        return ResearchDecision(
            action="synthesize",
            reason=(
                f"Research stop gate is {done.stop_reason}; preserve unresolved gaps "
                "and produce a bounded status synthesis for review."
            ),
            expected_information_gain=0.1,
        )
    open_gaps = sorted(
        (gap for gap in gaps if gap.status == "open"),
        key=lambda item: (-item.priority, item.id),
    )
    if not open_gaps:
        return ResearchDecision(
            action="synthesize",
            reason="No measurable open gap is available, but deterministic DoneCriteria did not pass.",
            expected_information_gain=0.1,
        )
    attempts = attempts_by_gap_action or {}
    eligible_gaps = []
    exhausted = []
    unsupported = []
    for gap in open_gaps:
        attempt_key = f"{gap.id}:{gap.recommended_action}"
        if (
            supported_actions is not None
            and gap.recommended_action not in supported_actions
        ):
            unsupported.append(attempt_key)
            continue
        raw_stats = attempts.get(attempt_key)
        if raw_stats is None or max_no_progress_per_gap_action is None:
            eligible_gaps.append(gap)
            continue
        stats = ActionAttemptStats.model_validate(raw_stats)
        if stats.no_progress >= max_no_progress_per_gap_action:
            exhausted.append(attempt_key)
        else:
            eligible_gaps.append(gap)
    if not eligible_gaps:
        details = []
        if exhausted:
            details.append("exhausted=" + ", ".join(exhausted))
        if unsupported:
            details.append("unsupported=" + ", ".join(unsupported))
        return ResearchDecision(
            action="synthesize",
            reason=(
                "No executable open gap/action pair remains"
                + (": " + "; ".join(details) if details else ".")
            ),
            expected_information_gain=0.05,
        )
    target = eligible_gaps[0]
    return ResearchDecision(
        action=target.recommended_action,
        target_gap_id=target.id,
        reason=f"Highest-priority measurable gap: {target.question}",
        expected_information_gain=min(max(target.priority, 0.05), 0.95),
    )
