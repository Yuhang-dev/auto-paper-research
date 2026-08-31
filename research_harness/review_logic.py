"""Deterministic selection, identity, readiness, and comparison rules."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Iterable, Mapping, Sequence, Tuple, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .review_models import (
    EvidenceCard,
    NonConsensusAssessment,
    ResearchUncertainty,
    ReviewReadiness,
    ReviewRunConfig,
    SourceRecord,
    SourceScreening,
    SourceSkim,
    SourceType,
    UnderstandingClaim,
)


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
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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
) -> Tuple[str, ...]:
    by_id = {item.source_id: item for item in sources}
    ranked = []
    for source_id, screening in screenings.items():
        source = by_id.get(source_id)
        if source is None or screening.label == "exclude":
            continue
        ranked.append((screening.ranking_score, source, source_id))
    ranked.sort(key=lambda item: (-item[0], source_identity(item[1]), item[2]))
    return _stratified_select(ranked, limit=config.max_skims, config=config)


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
        score = skim.relevance_score + 0.2
        score += min(len(skim.target_facets), 5) * 0.01
        if source_is_survey(source):
            score -= 0.25
        ranked.append((score, source, source_id))
    ranked.sort(key=lambda item: (-item[0], source_identity(item[1]), item[2]))
    required_papers = min(
        config.minimum_deep_read_papers,
        config.max_deep_reads,
    )
    selected = [
        item for item in ranked if item[1].source_type == "paper"
    ][:required_papers]
    selected_ids = {item[1].source_id for item in selected}
    remaining = [item for item in ranked if item[1].source_id not in selected_ids]
    slots = config.max_deep_reads - len(selected)
    if slots > 0:
        additions = _stratified_select(remaining, limit=slots, config=config)
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
            f"assessment {assessment.assessment_id} independent sources do not "
            "match its EvidenceCards"
        )
    if assessment.result in {"supported-consensus", "contested"} and len(
        actual_sources
    ) < 2:
        raise ValueError(
            "same-paper methods or configurations cannot establish cross-paper consensus"
        )
    if assessment.result in {"supported-consensus", "contested"}:
        referenced_cards = [cards[item] for item in referenced]
        if not _has_comparable_cross_source_pair(referenced_cards):
            raise ValueError(
                "consensus or contested evidence lacks a deterministically comparable "
                "cross-source experiment pair"
            )


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


def review_readiness(
    *,
    required_facets: Sequence[str],
    cards: Sequence[EvidenceCard],
    claims: Sequence[UnderstandingClaim],
    uncertainties: Sequence[ResearchUncertainty],
    assessments: Sequence[NonConsensusAssessment],
    saturated: bool,
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
    nonconsensus_questions = {
        item.uncertainty_id
        for item in uncertainties
        if item.category == "nonconsensus"
    }
    nonconsensus_complete = bool(assessments) or not nonconsensus_questions
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
    """Require two consecutive no-information rounds, never a paper-count threshold."""

    if len(round_gains) < 2:
        return False
    for gain in round_gains[-2:]:
        if int(gain.get("new_method_families") or 0) > 0:
            return False
        if int(gain.get("new_evidence_cards") or 0) > 0:
            return False
        if int(gain.get("resolved_blocking_uncertainties") or 0) > 0:
            return False
        if bool(gain.get("independent_counterevidence")):
            return False
    return True


__all__ = [
    "canonical_arxiv_id",
    "canonical_doi",
    "canonical_repository",
    "canonical_url",
    "merge_sources",
    "review_readiness",
    "search_saturated",
    "select_for_deep_read",
    "select_for_skim",
    "source_identity",
    "stable_id",
    "validate_nonconsensus_assessment",
]
