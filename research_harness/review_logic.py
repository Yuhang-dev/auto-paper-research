"""Deterministic selection, identity, readiness, and comparison rules."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from typing import Iterable, Literal, Mapping, Optional, Sequence, Tuple, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

from .review_models import (
    EvidenceCard,
    FacetCoverage,
    NonConsensusAssessment,
    ResearchUncertainty,
    ReviewCoverageMatrix,
    ReviewGap,
    ReviewReadiness,
    ReviewRunConfig,
    SourceRecord,
    SourceRelationCandidate,
    SourceScreening,
    SourceSkim,
    SourceType,
    UnderstandingClaim,
)
from .text_normalization import normalize_data


TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_NAMES = frozenset(
    {"fbclid", "gclid", "ref", "ref_src", "source", "mc_cid", "mc_eid"}
)
TYPE_ORDER = {"paper": 0, "project": 1, "web": 2}
SECONDARY_AGGREGATOR_HOSTS = frozenset(
    {
        "academia.edu",
        "researchgate.net",
        "scribd.com",
    }
)
OFFICIAL_WEB_HOSTS = frozenset(
    {
        "aclanthology.org",
        "ai.meta.com",
        "developer.nvidia.com",
        "docs.nvidia.com",
        "docs.pytorch.org",
        "docs.vllm.ai",
        "huggingface.co",
        "iclr.cc",
        "jmlr.org",
        "mlsys.org",
        "neurips.cc",
        "openreview.net",
        "papers.nips.cc",
        "proceedings.iclr.cc",
        "proceedings.mlr.press",
        "proceedings.neurips.cc",
        "pytorch.org",
        "research.google",
        "vllm.ai",
    }
)
UNVERIFIED_QUANTITATIVE_FINDING = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:%|×|x\b|times?\b|fold\b|ms\b|gb\b|"
    r"tokens?\b|[km]\b))|(?:speedup|latency|throughput|memory|accuracy|"
    r"perplexity|loss|improv\w*|reduc\w*|outperform\w*).{0,50}\d",
    re.IGNORECASE,
)
SURVEY_TITLE = re.compile(r"\bsurvey\b|\bsystematic review\b", re.IGNORECASE)
CORE_STUDY_ROLES = frozenset({"primary-study", "benchmark", "reproduction"})
ENGINEERING_FACETS = frozenset(
    {
        "latency-throughput",
        "memory-and-kv-cache",
        "kernels-and-hardware",
        "open-source-implementations",
        "limitations-and-counter-evidence",
        "prefill-vs-decode",
    }
)
T = TypeVar("T")


def canonical_url(value: str) -> str:
    """Normalize a public URL without erasing identity-bearing query fields."""

    parsed = urlsplit(value.strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"source URL must be absolute HTTP(S): {value!r}")
    scheme = parsed.scheme.casefold()
    host = parsed.hostname.casefold().rstrip(".")
    port = parsed.port
    netloc = host
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    kept = []
    for name, item in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = name.casefold()
        if lowered in TRACKING_QUERY_NAMES or lowered.startswith(TRACKING_QUERY_PREFIXES):
            continue
        kept.append((name, item))
    return urlunsplit((scheme, netloc, path, urlencode(sorted(kept)), ""))


def canonical_arxiv_id(value: str) -> str:
    text = value.strip()
    text = re.sub(r"^arxiv:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = text.split("?", 1)[0].split("#", 1)[0]
    text = re.sub(r"\.pdf$", "", text, flags=re.IGNORECASE)
    return re.sub(r"v\d+$", "", text, flags=re.IGNORECASE).strip()


def canonical_doi(value: str) -> str:
    text = value.strip().casefold()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)
    return text.removeprefix("doi:").strip()


def canonical_repository(value: str) -> str:
    text = value.strip()
    if text.startswith("http://") or text.startswith("https://"):
        parsed = urlsplit(text)
        if parsed.hostname and parsed.hostname.casefold() == "github.com":
            parts = [item for item in parsed.path.split("/") if item]
            if len(parts) >= 2:
                return f"{parts[0].casefold()}/{parts[1].removesuffix('.git').casefold()}"
    parts = [item for item in text.replace("\\", "/").split("/") if item]
    if len(parts) != 2:
        raise ValueError(f"GitHub repository must be owner/name: {value!r}")
    return f"{parts[0].casefold()}/{parts[1].removesuffix('.git').casefold()}"


def _host_matches(host: str, candidates: Iterable[str]) -> bool:
    return any(host == item or host.endswith(f".{item}") for item in candidates)


def web_source_authority(value: str) -> str:
    """Classify Web evidence conservatively from its canonical host."""

    parsed = urlsplit(canonical_url(value))
    host = (parsed.hostname or "").casefold().rstrip(".")
    if _host_matches(host, SECONDARY_AGGREGATOR_HOSTS):
        return "secondary-aggregator"
    if _host_matches(host, OFFICIAL_WEB_HOSTS):
        return "official"
    return "unknown"


def source_authority(source: SourceRecord) -> str:
    declared = str(source.metadata.get("source_authority") or "").strip()
    if declared in {
        "primary-paper",
        "repository",
        "official",
        "secondary-aggregator",
        "unknown",
    }:
        return declared
    if source.source_type == "paper":
        return "primary-paper"
    if source.source_type == "project":
        return "repository"
    return web_source_authority(source.canonical_url)


def source_evidence_eligible(source: SourceRecord) -> bool:
    """Only first-party artifacts may enter citation-ready deep reading."""

    return source_authority(source) in {
        "primary-paper",
        "repository",
        "official",
    }


def sanitize_provisional_skim(
    findings: Sequence[str],
    questions: Sequence[str],
) -> tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Remove unverified numeric results before Skims enter reasoning context."""

    retained = []
    omitted = 0
    for finding in findings:
        if UNVERIFIED_QUANTITATIVE_FINDING.search(finding):
            omitted += 1
        else:
            retained.append(finding)
    pending = list(questions)
    if omitted:
        pending.append(
            "Which quantitative performance claims survive full-text evidence "
            "checking under their original experimental conditions?"
        )
    return (
        tuple(dict.fromkeys(retained)),
        tuple(dict.fromkeys(pending)),
    )


def source_is_survey(source: SourceRecord) -> bool:
    return bool(SURVEY_TITLE.search(source.title))


def source_identity(source: SourceRecord) -> str:
    if source.source_type == "paper":
        if source.doi:
            return f"doi:{canonical_doi(source.doi)}"
        if source.arxiv_id:
            return f"arxiv:{canonical_arxiv_id(source.arxiv_id)}"
    if source.source_type == "project" and source.repository:
        return f"github:{canonical_repository(source.repository)}"
    return f"url:{canonical_url(source.canonical_url)}"


def _source_identities(source: SourceRecord) -> set[str]:
    identities = {f"url:{canonical_url(source.canonical_url)}"}
    if source.source_type == "paper":
        if source.doi:
            identities.add(f"doi:{canonical_doi(source.doi)}")
        if source.arxiv_id:
            identities.add(f"arxiv:{canonical_arxiv_id(source.arxiv_id)}")
    if source.source_type == "project" and source.repository:
        identities.add(f"github:{canonical_repository(source.repository)}")
    return identities


def stable_id(prefix: str, *parts: object, length: int = 20) -> str:
    payload = json.dumps(
        normalize_data(parts),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}-{digest}"


def _first_value(primary: object, alternate: object) -> object:
    return alternate if primary in (None, "", (), [], {}) else primary


def _merge_source_pair(current: SourceRecord, source: SourceRecord) -> SourceRecord:
    discoveries = tuple(
        sorted(
            {
                (
                    item.query_id,
                    item.provider,
                    item.rank,
                    item.retrieved_at,
                    item.provider_score,
                ): item
                for item in (*current.discoveries, *source.discoveries)
            }.values(),
            key=lambda item: (item.query_id, item.provider, item.rank),
        )
    )
    facets = tuple(dict.fromkeys((*current.target_facets, *source.target_facets)))
    metadata = {**source.metadata, **current.metadata}
    return current.model_copy(
        update={
            "authors": tuple(dict.fromkeys((*current.authors, *source.authors))),
            "year": _first_value(current.year, source.year),
            "venue": _first_value(current.venue, source.venue),
            "abstract": _first_value(current.abstract, source.abstract),
            "snippet": _first_value(current.snippet, source.snippet),
            "doi": _first_value(current.doi, source.doi),
            "arxiv_id": _first_value(current.arxiv_id, source.arxiv_id),
            "pdf_url": _first_value(current.pdf_url, source.pdf_url),
            "repository": _first_value(current.repository, source.repository),
            "license": _first_value(current.license, source.license),
            "stars": _first_value(current.stars, source.stars),
            "version": _first_value(current.version, source.version),
            "updated_at": _first_value(current.updated_at, source.updated_at),
            "target_facets": facets,
            "discoveries": discoveries,
            "local_path": _first_value(current.local_path, source.local_path),
            "content_sha256": _first_value(
                current.content_sha256, source.content_sha256
            ),
            "content_preview": _first_value(
                current.content_preview, source.content_preview
            ),
            "metadata": metadata,
        }
    )


def merge_sources(records: Iterable[SourceRecord]) -> Tuple[SourceRecord, ...]:
    """Merge DOI/arXiv/repository/URL aliases with transitive provenance."""

    ordered = sorted(
        records,
        key=lambda item: (
            TYPE_ORDER[item.source_type],
            source_identity(item),
            item.source_id,
        ),
    )
    merged: dict[str, SourceRecord] = {}
    identity_owner: dict[str, str] = {}
    cluster_identities: dict[str, set[str]] = {}
    for source in ordered:
        identities = _source_identities(source)
        owners = {identity_owner[item] for item in identities if item in identity_owner}
        if not owners:
            owner = min(identities)
            merged[owner] = source
            cluster_identities[owner] = set(identities)
        else:
            owner = min(owners)
            current = merged[owner]
            for alternate in sorted(owners - {owner}):
                current = _merge_source_pair(current, merged.pop(alternate))
                identities.update(cluster_identities.pop(alternate))
                for identity, claimed_by in tuple(identity_owner.items()):
                    if claimed_by == alternate:
                        identity_owner[identity] = owner
            merged[owner] = _merge_source_pair(current, source)
            cluster_identities[owner].update(identities)
        for identity in cluster_identities[owner]:
            identity_owner[identity] = owner
    return tuple(
        sorted(
            merged.values(),
            key=lambda item: (TYPE_ORDER[item.source_type], source_identity(item)),
        )
    )


def _type_limits(total: int, config: ReviewRunConfig) -> dict[SourceType, int]:
    quotas = {
        "paper": config.paper_source_quota,
        "project": config.project_source_quota,
        "web": config.web_source_quota,
    }
    raw = {
        kind: (total * quota / config.max_sources) for kind, quota in quotas.items()
    }
    limits = {kind: int(value) for kind, value in raw.items()}
    remaining = total - sum(limits.values())
    for kind, _ in sorted(
        raw.items(),
        key=lambda item: (-(item[1] - int(item[1])), TYPE_ORDER[item[0]]),
    ):
        if remaining <= 0:
            break
        limits[kind] += 1
        remaining -= 1
    return limits


def _stratified_select(
    ranked: Sequence[tuple[float, SourceRecord, T]],
    *,
    limit: int,
    config: ReviewRunConfig,
) -> Tuple[T, ...]:
    type_limits = _type_limits(limit, config)
    selected: list[tuple[float, SourceRecord, T]] = []
    selected_ids: set[str] = set()
    counts: dict[SourceType, int] = defaultdict(int)
    for item in ranked:
        _, source, value = item
        if counts[source.source_type] >= type_limits[source.source_type]:
            continue
        selected.append(item)
        selected_ids.add(source.source_id)
        counts[source.source_type] += 1
    for item in ranked:
        if len(selected) >= limit:
            break
        _, source, _ = item
        if source.source_id in selected_ids:
            continue
        selected.append(item)
        selected_ids.add(source.source_id)
    return tuple(item[2] for item in selected[:limit])


def select_for_skim(
    sources: Sequence[SourceRecord],
    screenings: Mapping[str, SourceScreening],
    config: ReviewRunConfig,
    *,
    limit: Optional[int] = None,
) -> Tuple[str, ...]:
    selection_limit = min(config.max_skims, limit or config.max_skims)
    by_id = {item.source_id: item for item in sources}
    ranked = []
    for source_id, screening in screenings.items():
        source = by_id.get(source_id)
        if source is None or screening.label == "exclude":
            continue
        ranked.append((screening.ranking_score, source, source_id))
    ranked.sort(key=lambda item: (-item[0], source_identity(item[1]), item[2]))
    if config.source_role_targets:
        type_limits = _type_limits(selection_limit, config)
        selected: list[tuple[float, SourceRecord, str]] = []
        selected_ids: set[str] = set()
        type_counts: dict[SourceType, int] = defaultdict(int)

        def admit(item: tuple[float, SourceRecord, str]) -> bool:
            _, source, source_id = item
            if source_id in selected_ids:
                return False
            if type_counts[source.source_type] >= type_limits[source.source_type]:
                return False
            selected.append(item)
            selected_ids.add(source_id)
            type_counts[source.source_type] += 1
            return True

        for role, target in config.source_role_targets.items():
            progressive_target = max(
                1,
                math.ceil(target * selection_limit / config.max_skims),
            )
            admitted = 0
            for item in ranked:
                if len(selected) >= selection_limit:
                    break
                source = item[1]
                screening = screenings[item[2]]
                effective_role = (
                    "project" if source.source_type == "project" else screening.source_role
                )
                if effective_role == role and admit(item):
                    admitted += 1
                    if admitted >= progressive_target:
                        break
        for item in ranked:
            if len(selected) >= selection_limit:
                break
            admit(item)
        for item in ranked:
            if len(selected) >= selection_limit:
                break
            if item[2] not in selected_ids:
                selected.append(item)
                selected_ids.add(item[2])
        return tuple(item[2] for item in selected[:selection_limit])
    return _stratified_select(ranked, limit=selection_limit, config=config)


def select_for_skim_round(
    sources: Sequence[SourceRecord],
    screenings: Mapping[str, SourceScreening],
    existing_skim_ids: Sequence[str],
    config: ReviewRunConfig,
    *,
    round_number: int,
) -> Tuple[str, ...]:
    """Allocate the cumulative Skim budget progressively across search rounds."""

    target_total = min(
        config.max_skims,
        math.ceil(
            config.max_skims * max(1, round_number) / config.max_search_rounds
        ),
    )
    source_ids = {item.source_id for item in sources}
    existing = tuple(sorted({item for item in existing_skim_ids if item in source_ids}))
    remaining = max(0, target_total - len(existing))
    if remaining == 0:
        return existing
    ranked = select_for_skim(
        sources,
        screenings,
        config,
        limit=target_total,
    )
    additions = tuple(item for item in ranked if item not in existing)[:remaining]
    return (*existing, *additions)


def select_for_deep_read(
    sources: Sequence[SourceRecord],
    skims: Mapping[str, SourceSkim],
    config: ReviewRunConfig,
) -> Tuple[str, ...]:
    by_id = {item.source_id: item for item in sources}
    ranked = []
    for source_id, skim in skims.items():
        source = by_id.get(source_id)
        if (
            source is None
            or skim.label == "exclude"
            or not skim.select_for_deep_read
            or not source_evidence_eligible(source)
        ):
            continue
        role = "project" if source.source_type == "project" else skim.source_role
        score = skim.relevance_score + 0.2
        score += min(len(skim.target_facets), 5) * 0.01
        if source_is_survey(source):
            score -= 0.25
        ranked.append((score, source, source_id, role))
    ranked.sort(key=lambda item: (-item[0], source_identity(item[1]), item[2]))
    if config.minimum_core_study_deep_reads:
        selected: list[tuple[float, SourceRecord, str, str]] = []
        selected_ids: set[str] = set()
        survey_count = 0
        nonpaper_count = 0

        def admit(
            item: tuple[float, SourceRecord, str, str], *, enforce_caps: bool = True
        ) -> bool:
            nonlocal survey_count, nonpaper_count
            _, source, source_id, role = item
            if source_id in selected_ids:
                return False
            if enforce_caps and role == "survey" and survey_count >= config.max_survey_deep_reads:
                return False
            if (
                enforce_caps
                and source.source_type != "paper"
                and nonpaper_count >= config.max_nonpaper_deep_reads
            ):
                return False
            selected.append(item)
            selected_ids.add(source_id)
            survey_count += int(role == "survey")
            nonpaper_count += int(source.source_type != "paper")
            return True

        core_target = min(
            config.minimum_core_study_deep_reads,
            config.max_deep_reads,
        )
        for item in ranked:
            if len(selected) >= core_target:
                break
            if item[1].source_type == "paper" and item[3] in CORE_STUDY_ROLES:
                admit(item)
        for item in ranked:
            if len(selected) >= config.max_deep_reads:
                break
            admit(item)
        for item in ranked:
            if len(selected) >= config.max_deep_reads:
                break
            admit(item, enforce_caps=False)
        return tuple(item[2] for item in selected[: config.max_deep_reads])
    required_papers = min(
        config.minimum_deep_read_papers,
        config.max_deep_reads,
    )
    selected = [item for item in ranked if item[1].source_type == "paper"][:required_papers]
    selected_ids = {item[1].source_id for item in selected}
    remaining = [item for item in ranked if item[1].source_id not in selected_ids]
    slots = config.max_deep_reads - len(selected)
    if slots > 0:
        additions = _stratified_select(
            tuple((score, source, source_id) for score, source, source_id, _ in remaining),
            limit=slots,
            config=config,
        )
    else:
        additions = ()
    return tuple(item[2] for item in selected) + additions


def validate_nonconsensus_assessment(
    assessment: NonConsensusAssessment,
    cards: Mapping[str, EvidenceCard],
) -> None:
    referenced = (*assessment.supporting_card_ids, *assessment.opposing_card_ids)
    unknown = sorted(set(referenced) - set(cards))
    if unknown:
        raise ValueError(
            f"assessment {assessment.assessment_id} references unknown cards: "
            + ", ".join(unknown)
        )
    actual_sources = {cards[item].source_id for item in referenced}
    declared_sources = set(assessment.independent_source_ids)
    if actual_sources != declared_sources:
        raise ValueError(
            f"assessment {assessment.assessment_id} must list the source IDs "
            "referenced by its EvidenceCards"
        )
    if assessment.result in {"supported-consensus", "contested"} and len(
        actual_sources
    ) < 2:
        raise ValueError(
            "cross-paper consensus requires at least two independent source IDs"
        )
    if assessment.result in {"supported-consensus", "contested"}:
        referenced_cards = [cards[item] for item in referenced]
        if not _has_comparable_cross_source_pair(referenced_cards):
            raise ValueError(
                "consensus and contested assessments require a comparable "
                "cross-source experiment pair with aligned conditions"
            )


def normalize_nonconsensus_assessment(
    assessment: NonConsensusAssessment,
    cards: Mapping[str, EvidenceCard],
    *,
    basis: Literal["skim", "evidence-pool"],
) -> NonConsensusAssessment:
    """Derive source identity from cards and downgrade unsupported comparisons."""

    supporting = tuple(
        dict.fromkeys(item for item in assessment.supporting_card_ids if item in cards)
    )
    opposing = tuple(
        dict.fromkeys(item for item in assessment.opposing_card_ids if item in cards)
    )
    referenced = (*supporting, *opposing)
    source_ids = tuple(sorted({cards[item].source_id for item in referenced}))
    result = assessment.result
    comparable = assessment.comparable
    rationale = assessment.rationale
    downgrade_reason = None
    if result in {"supported-consensus", "contested"}:
        referenced_cards = [cards[item] for item in referenced]
        if len(source_ids) < 2:
            downgrade_reason = "fewer than two independent evidence sources"
        elif not comparable:
            downgrade_reason = "the proposed comparison is not comparable"
        elif not _has_comparable_cross_source_pair(referenced_cards):
            downgrade_reason = "the referenced experiments have misaligned conditions"
    if downgrade_reason:
        result = "insufficient-evidence"
        comparable = False
        rationale = (
            f"Deterministic evidence validation found {downgrade_reason}. "
            f"{rationale}"
        )
    normalized = assessment.model_copy(
        update={
            "result": result,
            "comparable": comparable,
            "independent_source_ids": source_ids,
            "supporting_card_ids": supporting,
            "opposing_card_ids": opposing,
            "rationale": rationale,
            "basis": basis,
        }
    )
    validate_nonconsensus_assessment(normalized, cards)
    return normalized


def _has_comparable_cross_source_pair(cards: Sequence[EvidenceCard]) -> bool:
    """Require aligned experimental axes rather than trusting a model label."""

    axes = ("model", "benchmark", "task", "context_length", "metric", "unit")
    for position, left in enumerate(cards):
        for right in cards[position + 1 :]:
            if left.source_id == right.source_id:
                continue
            matched = 0
            conflict = False
            for axis in axes:
                left_value = getattr(left, axis)
                right_value = getattr(right, axis)
                if left_value and right_value:
                    if str(left_value).casefold() != str(right_value).casefold():
                        conflict = True
                        break
                    matched += 1
            if conflict:
                continue
            common_conditions = set(left.conditions) & set(right.conditions)
            if any(
                left.conditions[key].casefold() != right.conditions[key].casefold()
                for key in common_conditions
            ):
                continue
            matched += len(common_conditions)
            if matched >= 2:
                return True
    return False


def _normalized_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def build_source_relation_candidates(
    sources: Sequence[SourceRecord],
    skims: Sequence[SourceSkim],
) -> Tuple[SourceRelationCandidate, ...]:
    """Create provisional topology edges without merging semantic aliases."""

    relations: dict[str, SourceRelationCandidate] = {}
    for skim in skims:
        for hint in skim.relation_hints:
            relation_id = stable_id(
                "relation",
                skim.source_id,
                hint.subject.casefold(),
                hint.relation,
                hint.object.casefold(),
            )
            relations[relation_id] = SourceRelationCandidate(
                relation_id=relation_id,
                subject=hint.subject,
                relation=hint.relation,
                object=hint.object,
                confidence=0.65,
                basis=hint.rationale,
                source_ids=(skim.source_id,),
            )

    work_sources = [item for item in sources if item.source_type != "project"]
    for left, right in combinations(work_sources, 2):
        if left.source_type != "paper" and right.source_type != "paper":
            continue
        if source_identity(left) == source_identity(right):
            continue
        left_title = _normalized_title(left.title)
        right_title = _normalized_title(right.title)
        if not left_title or not right_title:
            continue
        similarity = SequenceMatcher(None, left_title, right_title).ratio()
        exact_title = left_title == right_title
        same_year = left.year is not None and left.year == right.year
        left_authors = {item.casefold() for item in left.authors}
        right_authors = {item.casefold() for item in right.authors}
        author_overlap = bool(left_authors & right_authors)
        if not exact_title and not (similarity >= 0.94 and (same_year or author_overlap)):
            continue
        ordered_ids = tuple(sorted((left.source_id, right.source_id)))
        relation_id = stable_id("relation", *ordered_ids, "possible-same-work")
        relations[relation_id] = SourceRelationCandidate(
            relation_id=relation_id,
            subject=left.source_id,
            relation="possible-same-work",
            object=right.source_id,
            confidence=min(0.98, 0.82 + similarity * 0.12 + int(same_year) * 0.02),
            basis=(
                "Conservative title/version signal; exact identifiers did not match, "
                "so the records remain separate pending confirmation."
            ),
            source_ids=ordered_ids,
        )
    return tuple(sorted(relations.values(), key=lambda item: item.relation_id))


def build_review_coverage(
    *,
    required_facets: Sequence[str],
    skims: Sequence[SourceSkim],
    cards: Sequence[EvidenceCard],
) -> ReviewCoverageMatrix:
    cards_by_facet: dict[str, list[EvidenceCard]] = defaultdict(list)
    for card in cards:
        for facet in card.target_facets:
            cards_by_facet[facet].append(card)
    facets = []
    for facet in required_facets:
        selected = cards_by_facet.get(facet, [])
        source_ids = tuple(sorted({item.source_id for item in selected}))
        status = "covered" if len(source_ids) >= 2 else "partial" if source_ids else "missing"
        facets.append(
            FacetCoverage(
                facet=facet,
                status=status,
                independent_source_ids=source_ids,
                evidence_card_ids=tuple(sorted(item.card_id for item in selected)),
            )
        )
    role_counts: dict[str, int] = defaultdict(int)
    for skim in skims:
        role_counts[skim.source_role] += 1
    return ReviewCoverageMatrix(
        facets=tuple(facets),
        source_role_counts=dict(sorted(role_counts.items())),
        evidence_source_ids=tuple(sorted({item.source_id for item in cards})),
    )


def analyze_review_gaps(
    *,
    scope_title: str,
    sources: Sequence[SourceRecord],
    skims: Sequence[SourceSkim],
    cards: Sequence[EvidenceCard],
    claims: Sequence[UnderstandingClaim],
    uncertainties: Sequence[ResearchUncertainty],
    assessments: Sequence[NonConsensusAssessment],
    coverage: ReviewCoverageMatrix,
    current_year: Optional[int] = None,
) -> Tuple[ReviewGap, ...]:
    """Derive actionable research gaps from located evidence and open questions."""

    year = current_year or datetime.now(timezone.utc).year
    cards_by_id = {item.card_id: item for item in cards}
    sources_by_id = {item.source_id: item for item in sources}
    gaps: dict[str, ReviewGap] = {}

    def add(
        *,
        kind: str,
        key: str,
        question: str,
        base_priority: float,
        blocking: bool = False,
        report_critical: bool = False,
        target_facets: Sequence[str] = (),
        target_source_roles: Sequence[str] = (),
        source_ids: Sequence[str] = (),
        claim_ids: Sequence[str] = (),
        next_queries: Sequence[str] = (),
    ) -> None:
        gap_id = stable_id("review-gap", kind, key)
        gaps[gap_id] = ReviewGap(
            gap_id=gap_id,
            kind=kind,
            question=question,
            priority=round(
                min(1.0, base_priority + (0.05 if report_critical else 0.0)), 2
            ),
            blocking=blocking,
            report_critical=report_critical,
            target_facets=tuple(dict.fromkeys(target_facets)),
            target_source_roles=tuple(dict.fromkeys(target_source_roles)),
            source_ids=tuple(sorted(set(source_ids))),
            claim_ids=tuple(sorted(set(claim_ids))),
            next_queries=tuple(dict.fromkeys(next_queries)),
        )

    for item in uncertainties:
        if item.origin == "deterministic" or item.status != "open" or not item.blocking:
            continue
        add(
            kind="blocking-uncertainty",
            key=item.uncertainty_id,
            question=item.question,
            base_priority=1.0,
            blocking=True,
            report_critical=True,
            target_facets=item.target_facets,
            target_source_roles=item.target_source_roles or ("primary-study",),
            source_ids=(),
            next_queries=item.next_queries
            or (f'"{scope_title}" {item.question} research paper evidence',),
        )

    for facet in coverage.facets:
        if facet.status != "missing":
            continue
        add(
            kind="missing-facet",
            key=facet.facet,
            question=f"Which primary evidence covers the missing facet: {facet.facet}?",
            base_priority=0.90,
            report_critical=True,
            target_facets=(facet.facet,),
            target_source_roles=("primary-study", "benchmark", "reproduction"),
            next_queries=(f'"{scope_title}" {facet.facet} benchmark experiment',),
        )

    for claim in claims:
        referenced = tuple(
            card_id
            for card_id in (*claim.supporting_card_ids, *claim.opposing_card_ids)
            if card_id in cards_by_id
        )
        source_ids = {cards_by_id[item].source_id for item in referenced}
        if referenced and len(source_ids) == 1:
            add(
                kind="single-source-claim",
                key=claim.claim_id,
                question=f"Which independent study confirms or challenges: {claim.statement}",
                base_priority=0.80,
                report_critical=True,
                target_source_roles=("primary-study", "reproduction"),
                source_ids=source_ids,
                claim_ids=(claim.claim_id,),
                next_queries=(f'"{claim.statement}" replication limitations baseline',),
            )

    for assessment in assessments:
        if assessment.result != "insufficient-evidence" or len(
            set(assessment.independent_source_ids)
        ) < 2:
            continue
        add(
            kind="incomparable-evidence",
            key=assessment.assessment_id,
            question=f"Which controlled experiments make this comparison valid: {assessment.question}",
            base_priority=0.75,
            report_critical=True,
            target_source_roles=("benchmark", "reproduction", "primary-study"),
            source_ids=assessment.independent_source_ids,
            next_queries=(f'"{assessment.question}" controlled benchmark same model hardware',),
        )

    method_sources: dict[str, set[str]] = defaultdict(set)
    method_core_sources: dict[str, set[str]] = defaultdict(set)
    for skim in skims:
        for method in skim.method_families:
            method_sources[method].add(skim.source_id)
            if skim.label == "core":
                method_core_sources[method].add(skim.source_id)
    for method, core_source_ids in sorted(method_core_sources.items()):
        method_cards = [
            item
            for item in cards
            if item.source_id in method_sources[method]
            or (item.method and item.method.casefold() == method.casefold())
        ]
        engineering_cards = [
            item
            for item in method_cards
            if set(item.target_facets) & ENGINEERING_FACETS
        ]
        if not engineering_cards:
            add(
                kind="method-evidence",
                key=method,
                question=f"What engineering measurements and failure conditions are reported for {method}?",
                base_priority=0.70,
                target_facets=tuple(sorted(ENGINEERING_FACETS & set(coverage_item.facet for coverage_item in coverage.facets))),
                target_source_roles=("primary-study", "reproduction", "project"),
                source_ids=core_source_ids,
                next_queries=(f'"{method}" latency memory kernel failure long context',),
            )
        if len(core_source_ids) == 1 and not method_cards:
            add(
                kind="orphan-concept",
                key=method,
                question=f"Which independent sources define, evaluate, or reproduce {method}?",
                base_priority=0.60,
                target_source_roles=("primary-study", "survey", "reproduction"),
                source_ids=core_source_ids,
                next_queries=(f'"{method}" sparse attention paper evaluation',),
            )
        dated_sources = [sources_by_id[item] for item in core_source_ids if item in sources_by_id]
        known_years = [item.year for item in dated_sources if item.year is not None]
        if known_years and max(known_years) <= year - 2:
            add(
                kind="stale-evidence",
                key=method,
                question=f"What evidence published after {year - 2} updates the {method} route?",
                base_priority=0.50,
                target_source_roles=("primary-study", "benchmark", "project"),
                source_ids=core_source_ids,
                next_queries=(f'"{method}" {year - 1} {year} long context',),
            )
    return tuple(sorted(gaps.values(), key=lambda item: (-item.priority, item.gap_id)))


def merge_gap_uncertainties(
    uncertainties: Sequence[ResearchUncertainty],
    gaps: Sequence[ReviewGap],
) -> Tuple[ResearchUncertainty, ...]:
    semantic = [item for item in uncertainties if item.origin != "deterministic"]
    category_by_kind = {
        "blocking-uncertainty": "scope",
        "missing-facet": "coverage",
        "single-source-claim": "replication",
        "incomparable-evidence": "nonconsensus",
        "method-evidence": "engineering",
        "orphan-concept": "topology",
        "stale-evidence": "freshness",
    }
    generated = [
        ResearchUncertainty(
            uncertainty_id=item.gap_id,
            question=item.question,
            category=category_by_kind[item.kind],
            priority=item.priority,
            blocking=item.blocking,
            status="open",
            next_queries=item.next_queries,
            origin="deterministic",
            target_facets=item.target_facets,
            target_source_roles=item.target_source_roles,
            report_critical=item.report_critical,
        )
        for item in gaps
        if item.kind != "blocking-uncertainty"
    ]
    return tuple(
        sorted((*semantic, *generated), key=lambda item: (-item.priority, item.uncertainty_id))
    )


def formal_wiki_paper_identities(wiki_root: Path) -> frozenset[str]:
    """Read existing paper identifiers for promotion deduplication."""

    identities: set[str] = set()
    paper_root = wiki_root / "papers"
    if not paper_root.is_dir():
        return frozenset()
    for path in sorted(paper_root.glob("*.md")):
        text = path.read_text(encoding="utf-8-sig")
        match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", text, re.DOTALL)
        if not match:
            raise ValueError(f"Wiki paper page is missing YAML frontmatter: {path}")
        try:
            payload = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Wiki paper frontmatter is invalid: {path}") from exc
        if not isinstance(payload, Mapping):
            raise ValueError(f"Wiki paper frontmatter must be a mapping: {path}")
        paper_id = str(payload.get("id") or "").strip()
        if paper_id:
            identities.add(paper_id)
        identifiers = payload.get("identifiers") or {}
        if isinstance(identifiers, Mapping):
            arxiv = str(identifiers.get("arxiv") or "").strip()
            doi = str(identifiers.get("doi") or "").strip()
            try:
                if arxiv:
                    identities.add(f"arxiv:{canonical_arxiv_id(arxiv)}")
                if doi:
                    identities.add(f"doi:{canonical_doi(doi)}")
            except ValueError as exc:
                raise ValueError(f"Wiki paper identifier is invalid: {path}") from exc
    return frozenset(identities)


def review_readiness(
    *,
    required_facets: Sequence[str],
    cards: Sequence[EvidenceCard],
    claims: Sequence[UnderstandingClaim],
    uncertainties: Sequence[ResearchUncertainty],
    assessments: Sequence[NonConsensusAssessment],
    saturated: bool,
    required_nonconsensus_uncertainty_ids: Sequence[str] = (),
) -> ReviewReadiness:
    cards_by_facet: dict[str, set[str]] = defaultdict(set)
    for card in cards:
        for facet in card.target_facets:
            cards_by_facet[facet].add(card.source_id)
    statuses = {}
    for facet in required_facets:
        count = len(cards_by_facet.get(facet, set()))
        statuses[facet] = "covered" if count >= 2 else "partial" if count else "missing"
    blocking = tuple(
        sorted(
            item.uncertainty_id
            for item in uncertainties
            if item.blocking and item.status == "open"
        )
    )
    generic_nonconsensus_prefix = "what evidence is required to explain "
    eligible_uncertainties = {
        item.uncertainty_id: item
        for item in uncertainties
        if item.category == "nonconsensus"
        and not " ".join(item.question.casefold().split()).startswith(
            generic_nonconsensus_prefix
        )
    }
    required_assessment_ids = set(required_nonconsensus_uncertainty_ids)
    if not required_assessment_ids:
        required_assessment_ids = set(eligible_uncertainties)
    uncertainty_id_by_question = {
        " ".join(item.question.casefold().split()): item.uncertainty_id
        for item in eligible_uncertainties.values()
    }
    evidence_pool_assessment_ids = set()
    for item in assessments:
        if item.basis != "evidence-pool":
            continue
        if item.uncertainty_id:
            evidence_pool_assessment_ids.add(item.uncertainty_id)
            continue
        legacy_id = uncertainty_id_by_question.get(
            " ".join(item.question.casefold().split())
        )
        if legacy_id:
            evidence_pool_assessment_ids.add(legacy_id)
    nonconsensus_complete = required_assessment_ids.issubset(
        evidence_pool_assessment_ids
    )
    evidenced_claims = sum(
        bool(item.supporting_card_ids or item.opposing_card_ids) for item in claims
    )
    independent_sources = len({item.source_id for item in cards})
    reasons = []
    missing = [name for name, status in statuses.items() if status == "missing"]
    if missing:
        reasons.append("missing evidence facets: " + ", ".join(missing))
    if blocking:
        reasons.append("open blocking uncertainties: " + ", ".join(blocking))
    if not nonconsensus_complete:
        reasons.append("non-consensus review is incomplete")
    if not saturated:
        reasons.append("search has not reached understanding-level saturation")
    if evidenced_claims == 0:
        reasons.append("no understanding claim has citation-ready evidence")
    ready = (
        not missing
        and not blocking
        and nonconsensus_complete
        and evidenced_claims > 0
        and saturated
    )
    return ReviewReadiness(
        facet_statuses=statuses,
        citation_ready_cards=len(cards),
        evidenced_claims=evidenced_claims,
        independent_sources=independent_sources,
        unresolved_blocking_ids=blocking,
        nonconsensus_review_complete=nonconsensus_complete,
        saturated=saturated,
        ready=ready,
        reasons=tuple(reasons),
    )


def search_saturated(round_gains: Sequence[Mapping[str, object]]) -> bool:
    """Detect saturation from two consecutive rounds without information gain."""

    if len(round_gains) < 2:
        return False
    for gain in round_gains[-2:]:
        if int(gain.get("new_method_families") or 0) > 0:
            return False
        if int(gain.get("new_evidence_cards") or 0) > 0:
            return False
        if int(gain.get("new_independent_sources") or 0) > 0:
            return False
        if int(gain.get("new_covered_facets") or 0) > 0:
            return False
        if int(gain.get("resolved_blocking_uncertainties") or 0) > 0:
            return False
        if bool(gain.get("independent_counterevidence")):
            return False
        if int(gain.get("new_confirmed_topology_relations") or 0) > 0:
            return False
    return True


__all__ = [
    "canonical_arxiv_id",
    "canonical_doi",
    "canonical_repository",
    "canonical_url",
    "analyze_review_gaps",
    "build_review_coverage",
    "build_source_relation_candidates",
    "formal_wiki_paper_identities",
    "merge_sources",
    "merge_gap_uncertainties",
    "normalize_nonconsensus_assessment",
    "review_readiness",
    "search_saturated",
    "select_for_deep_read",
    "select_for_skim",
    "select_for_skim_round",
    "source_identity",
    "stable_id",
    "validate_nonconsensus_assessment",
]
