"""Bounded multi-source retrieval for the review-first loop."""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

import requests

from .paper_ingest import extract_pdf_document, select_paper_excerpt
from .paper_sources import ArxivPaperSourceAcquirer
from .review_logic import (
    canonical_arxiv_id,
    canonical_doi,
    canonical_repository,
    canonical_url,
    stable_id,
)
from .review_models import (
    DiscoveryRecord,
    RetrievalQuery,
    SourceMaterial,
    SourceRecord,
)


MAX_WEB_BYTES = 5 * 1024 * 1024
ALLOWED_WEB_CONTENT = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
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


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _year(value: Any) -> Optional[int]:
    match = re.search(r"\b(?:19|20)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else None


def _authors(value: Any) -> tuple[str, ...]:
    values = value if isinstance(value, (list, tuple)) else [value]
    result = []
    for item in values:
        if isinstance(item, Mapping):
            item = item.get("name") or item.get("full_name")
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _safe_message(exc: BaseException, secrets: Sequence[str]) -> str:
    message = f"{type(exc).__name__}: {exc}"
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message[:1200]


class RetrievalProvider(Protocol):
    name: str

    async def search(
        self, query: RetrievalQuery, *, limit: int
    ) -> tuple[SourceRecord, ...]: ...


class DeepXivProvider:
    name = "deepxiv"

    def __init__(self, token: str, *, timeout_seconds: int = 60):
        if not token.strip():
            raise ValueError("DEEPXIV_TOKEN is required")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def _search_sync(self, query: RetrievalQuery, limit: int) -> tuple[SourceRecord, ...]:
        try:
            from deepxiv_sdk import Reader  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("deepxiv-sdk is not installed") from exc
        reader = Reader(
            token=self.token,
            timeout=self.timeout_seconds,
            max_retries=0,
            retry_delay=0,
        )
        response = reader.search(
            query=query.text,
            size=limit,
            offset=0,
            source="arxiv",
            use_fine_rerank=False,
        )
        if not isinstance(response, Mapping):
            raise ValueError("DeepXiv returned a non-mapping response")
        rows = response.get("result") or []
        if not isinstance(rows, list):
            raise ValueError("DeepXiv result must be a list")
        retrieved_at = _utc_now()
        result = []
        for rank, row in enumerate(rows[:limit], start=1):
            if not isinstance(row, Mapping):
                continue
            raw_id = row.get("arxiv_id") or row.get("source_id")
            if not raw_id:
                continue
            arxiv_id = canonical_arxiv_id(str(raw_id))
            version_match = re.search(r"(v\d+)\s*$", str(raw_id), re.IGNORECASE)
            title = str(row.get("title") or row.get("paper_title") or "").strip()
            if not arxiv_id or not title:
                continue
            doi_value = row.get("doi") or row.get("DOI")
            doi = canonical_doi(str(doi_value)) if doi_value else None
            date = row.get("date") or row.get("published_at") or row.get("publish_at")
            result.append(
                SourceRecord(
                    source_id=f"paper:arxiv:{arxiv_id}",
                    source_type="paper",
                    provider="deepxiv",
                    title=title,
                    canonical_url=str(
                        row.get("paper_url") or f"https://arxiv.org/abs/{arxiv_id}"
                    ),
                    authors=_authors(row.get("authors")),
                    year=_year(row.get("year") or date),
                    venue=(
                        str(row.get("venue") or row.get("journal") or "").strip()
                        or None
                    ),
                    abstract=(
                        str(row.get("abstract") or row.get("summary") or "").strip()
                        or None
                    ),
                    snippet=(str(row.get("tldr") or "").strip() or None),
                    doi=doi,
                    arxiv_id=arxiv_id,
                    pdf_url=str(
                        row.get("pdf_url") or f"https://arxiv.org/pdf/{arxiv_id}"
                    ),
                    repository=(
                        str(
                            row.get("github_url")
                            or row.get("code_url")
                            or row.get("repository_url")
                            or ""
                        ).strip()
                        or None
                    ),
                    version=(
                        str(row.get("version") or "").strip()
                        or (
                            version_match.group(1).casefold()
                            if version_match
                            else "arxiv-latest-at-retrieval"
                        )
                    ),
                    target_facets=query.target_facets,
                    discoveries=(
                        DiscoveryRecord(
                            query_id=query.id,
                            provider="deepxiv",
                            rank=rank,
                            retrieved_at=retrieved_at,
                            provider_score=(
                                float(row["score"])
                                if isinstance(row.get("score"), (int, float))
                                else None
                            ),
                        ),
                    ),
                    metadata={
                        "citation_count": row.get("citation_count"),
                        "categories": row.get("categories") or [],
                    },
                )
            )
        return tuple(result)

    async def search(
        self, query: RetrievalQuery, *, limit: int
    ) -> tuple[SourceRecord, ...]:
        return await asyncio.to_thread(self._search_sync, query, limit)


def _semantic_scholar_identifier(source: SourceRecord) -> Optional[str]:
    if source.arxiv_id:
        return f"ARXIV:{canonical_arxiv_id(source.arxiv_id)}"
    if source.doi:
        return f"DOI:{canonical_doi(source.doi)}"
    paper_id = str(source.metadata.get("semantic_scholar_paper_id") or "").strip()
    return paper_id or None


def _semantic_scholar_paper_details(
    identifier: str,
    api_key: str,
) -> Mapping[str, Any]:
    """Fetch one bounded paper-detail record after funnel selection."""

    fields = (
        "paperId,corpusId,title,abstract,year,authors,url,venue,externalIds,"
        "openAccessPdf,citationCount,influentialCitationCount,publicationDate"
    )
    headers = {
        "Accept": "application/json",
        "User-Agent": "auto-paper-research-review-harness/0.1",
        "x-api-key": api_key,
    }
    encoded_identifier = urllib.parse.quote(identifier, safe=":")
    url = (
        "https://api.semanticscholar.org/graph/v1/paper/"
        f"{encoded_identifier}"
    )
    # Follow the official tutorial's requests-based example. In particular,
    # keep the documented lower-case x-api-key header instead of letting
    # urllib.request canonicalize its spelling.
    with requests.get(
        url,
        params={"fields": fields},
        headers=headers,
        timeout=45,
        stream=True,
    ) as response:
        data = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            data.extend(chunk)
            if len(data) > MAX_WEB_BYTES:
                raise ValueError(
                    "Semantic Scholar response exceeded the configured byte limit"
                )
        if response.status_code >= 400:
            detail = bytes(data).decode("utf-8", errors="replace")[:800]
            raise RuntimeError(
                f"Semantic Scholar HTTP {response.status_code}: {detail}"
            )
    payload = json.loads(bytes(data).decode("utf-8", errors="replace"))
    if not isinstance(payload, Mapping):
        raise ValueError("Semantic Scholar returned a non-mapping response")
    return payload


class SemanticScholarProvider:
    """Best-effort metadata enrichment for papers selected for deep reading."""

    name = "semantic_scholar"

    def __init__(self, api_key: str):
        if not api_key.strip():
            raise ValueError("SEMANTIC_SCHOLAR_API_KEY is required")
        self.api_key = api_key.strip()
        self._request_lock = threading.Lock()
        self._last_request_started = 0.0

    def _request(self, identifier: str) -> Mapping[str, Any]:
        with self._request_lock:
            wait_seconds = 1.0 - (time.monotonic() - self._last_request_started)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            self._last_request_started = time.monotonic()
            return _semantic_scholar_paper_details(
                identifier,
                self.api_key,
            )

    async def enrich(self, source: SourceRecord) -> SourceRecord:
        if source.source_type != "paper":
            return source
        if source.metadata.get("semantic_scholar_enriched_at"):
            return source
        identifier = _semantic_scholar_identifier(source)
        if not identifier:
            return source
        # Details are fetched only after deterministic deep-read selection,
        # never during broad discovery or skim.
        payload = await asyncio.to_thread(self._request, identifier)
        paper_id = str(payload.get("paperId") or "").strip()
        if not paper_id:
            raise ValueError("Semantic Scholar paper detail omitted paperId")
        external_ids = payload.get("externalIds") or {}
        if not isinstance(external_ids, Mapping):
            external_ids = {}
        raw_arxiv = external_ids.get("ArXiv") or external_ids.get("arXiv")
        raw_doi = external_ids.get("DOI") or external_ids.get("doi")
        open_access = payload.get("openAccessPdf") or {}
        if not isinstance(open_access, Mapping):
            open_access = {}
        open_pdf_url = str(open_access.get("url") or "").strip() or None
        publication_date = str(payload.get("publicationDate") or "").strip()
        metadata = {
            **source.metadata,
            "semantic_scholar_paper_id": paper_id,
            "semantic_scholar_corpus_id": payload.get("corpusId"),
            "semantic_scholar_url": payload.get("url"),
            "semantic_scholar_enriched_at": _utc_now(),
            "citation_count": payload.get("citationCount"),
            "influential_citation_count": payload.get("influentialCitationCount"),
            "external_ids": dict(external_ids),
            "open_access_pdf": open_pdf_url,
        }
        return source.model_copy(
            update={
                "authors": source.authors or _authors(payload.get("authors")),
                "year": source.year or _year(payload.get("year") or publication_date),
                "venue": source.venue or (str(payload.get("venue") or "").strip() or None),
                "abstract": source.abstract or (str(payload.get("abstract") or "").strip() or None),
                "doi": source.doi or (canonical_doi(str(raw_doi)) if raw_doi else None),
                "arxiv_id": source.arxiv_id or (
                    canonical_arxiv_id(str(raw_arxiv)) if raw_arxiv else None
                ),
                "pdf_url": source.pdf_url or open_pdf_url,
                "metadata": metadata,
            }
        )


class TavilyProvider:
    name = "tavily"

    def __init__(self, api_key: str):
        if not api_key.strip():
            raise ValueError("TAVILY_API_KEY is required")
        self.api_key = api_key

    async def search(
        self, query: RetrievalQuery, *, limit: int
    ) -> tuple[SourceRecord, ...]:
        try:
            from tavily import AsyncTavilyClient  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("tavily-python is not installed") from exc
        client = AsyncTavilyClient(api_key=self.api_key)
        bounded_limit = min(max(1, limit), 20)
        response = await client.search(
            query=query.text,
            search_depth="basic",
            max_results=bounded_limit,
            include_answer=False,
            include_raw_content=True,
        )
        rows = response.get("results") or []
        if not isinstance(rows, list):
            raise ValueError("Tavily results must be a list")
        retrieved_at = _utc_now()
        result = []
        for rank, row in enumerate(rows[:bounded_limit], start=1):
            if not isinstance(row, Mapping):
                continue
            url = str(row.get("url") or "").strip()
            title = str(row.get("title") or "").strip()
            if not url or not title:
                continue
            normalized_url = canonical_url(url)
            raw = str(row.get("raw_content") or row.get("content") or "").strip()
            parsed = urllib.parse.urlsplit(normalized_url)
            path_parts = [item for item in parsed.path.split("/") if item]
            repository = None
            arxiv_id = None
            doi = None
            version = (
                str(row.get("published_date") or "").strip()
                or f"retrieved:{retrieved_at}"
            )
            source_type = "web"
            source_id = stable_id("web", normalized_url)
            pdf_url = None
            if parsed.hostname == "github.com" and len(path_parts) >= 2:
                repository = canonical_repository(
                    f"{path_parts[0]}/{path_parts[1]}"
                )
                source_type = "project"
                source_id = f"project:github:{repository}"
            elif parsed.hostname in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
                match = re.search(r"/(?:abs|pdf)/([^/?#]+)", parsed.path)
                if match:
                    raw_arxiv_id = match.group(1)
                    arxiv_id = canonical_arxiv_id(raw_arxiv_id)
                    version_match = re.search(
                        r"(v\d+)\s*$", raw_arxiv_id, re.IGNORECASE
                    )
                    version = (
                        version_match.group(1).casefold()
                        if version_match
                        else "arxiv-latest-at-retrieval"
                    )
                    source_type = "paper"
                    source_id = f"paper:arxiv:{arxiv_id}"
                    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
            elif parsed.hostname in {"doi.org", "dx.doi.org"} and path_parts:
                doi = canonical_doi("/".join(path_parts))
                source_type = "paper"
                source_id = stable_id("paper-doi", doi)
            result.append(
                SourceRecord(
                    source_id=source_id,
                    source_type=source_type,
                    provider="tavily",
                    title=title,
                    canonical_url=normalized_url,
                    year=_year(row.get("published_date")),
                    snippet=(str(row.get("content") or "").strip() or None),
                    content_preview=raw[:12_000] or None,
                    doi=doi,
                    arxiv_id=arxiv_id,
                    pdf_url=pdf_url,
                    repository=repository,
                    version=version,
                    target_facets=tuple(
                        dict.fromkeys(
                            (
                                *query.target_facets,
                                *(
                                    ("open-source-implementations",)
                                    if source_type == "project"
                                    else ()
                                ),
                            )
                        )
                    ),
                    discoveries=(
                        DiscoveryRecord(
                            query_id=query.id,
                            provider="tavily",
                            rank=rank,
                            retrieved_at=retrieved_at,
                            provider_score=(
                                float(row["score"])
                                if isinstance(row.get("score"), (int, float))
                                else None
                            ),
                        ),
                    ),
                    metadata={"published_date": row.get("published_date")},
                )
            )
        return tuple(result)


def _github_request(url: str, token: str, *, accept: str) -> Any:
    headers = {
        "Accept": accept,
        "User-Agent": "auto-paper-research-review-harness",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=45) as response:
        data = response.read(MAX_WEB_BYTES + 1)
        if len(data) > MAX_WEB_BYTES:
            raise ValueError("GitHub response exceeded the configured byte limit")
        content_type = response.headers.get("Content-Type", "")
    if "json" in content_type:
        return json.loads(data.decode("utf-8", errors="replace"))
    return data.decode("utf-8", errors="replace")


class GitHubProvider:
    name = "github"

    def __init__(self, token: str = ""):
        self.token = token.strip()

    async def search(
        self, query: RetrievalQuery, *, limit: int
    ) -> tuple[SourceRecord, ...]:
        encoded = urllib.parse.urlencode(
            {
                "q": f"{query.text} in:name,description,readme",
                "sort": "stars",
                "order": "desc",
                "per_page": min(limit, 100),
            }
        )
        payload = await asyncio.to_thread(
            _github_request,
            f"https://api.github.com/search/repositories?{encoded}",
            self.token,
            accept="application/vnd.github+json",
        )
        if not isinstance(payload, Mapping) or not isinstance(payload.get("items"), list):
            raise ValueError("GitHub repository search returned an invalid response")
        retrieved_at = _utc_now()
        result = []
        for rank, row in enumerate(payload["items"][:limit], start=1):
            if not isinstance(row, Mapping):
                continue
            repository = str(row.get("full_name") or "").strip()
            title = str(row.get("name") or repository).strip()
            html_url = str(row.get("html_url") or "").strip()
            if not repository or not html_url:
                continue
            license_payload = row.get("license") or {}
            license_name = (
                str(license_payload.get("spdx_id") or "").strip()
                if isinstance(license_payload, Mapping)
                else ""
            )
            result.append(
                SourceRecord(
                    source_id=f"project:github:{canonical_repository(repository)}",
                    source_type="project",
                    provider="github",
                    title=title,
                    canonical_url=canonical_url(html_url),
                    snippet=(str(row.get("description") or "").strip() or None),
                    repository=canonical_repository(repository),
                    license=license_name or None,
                    stars=(
                        int(row["stargazers_count"])
                        if isinstance(row.get("stargazers_count"), int)
                        else None
                    ),
                    updated_at=(str(row.get("updated_at") or "").strip() or None),
                    version=(str(row.get("updated_at") or "").strip() or None),
                    target_facets=tuple(
                        dict.fromkeys((*query.target_facets, "open-source-implementations"))
                    ),
                    discoveries=(
                        DiscoveryRecord(
                            query_id=query.id,
                            provider="github",
                            rank=rank,
                            retrieved_at=retrieved_at,
                            provider_score=None,
                        ),
                    ),
                    metadata={
                        "default_branch": row.get("default_branch"),
                        "fork": bool(row.get("fork")),
                        "archived": bool(row.get("archived")),
                        "language": row.get("language"),
                        "open_issues_count": row.get("open_issues_count"),
                    },
                )
            )
        return tuple(result)

    async def readme(self, repository: str) -> str:
        owner_name = canonical_repository(repository)
        return str(
            await asyncio.to_thread(
                _github_request,
                f"https://api.github.com/repos/{owner_name}/readme",
                self.token,
                accept="application/vnd.github.raw+json",
            )
        )

    async def audit(self, repository: str) -> Mapping[str, Any]:
        """Fetch bounded official metadata needed for engineering maturity review."""

        owner_name = canonical_repository(repository)
        details = await asyncio.to_thread(
            _github_request,
            f"https://api.github.com/repos/{owner_name}",
            self.token,
            accept="application/vnd.github+json",
        )
        if not isinstance(details, Mapping):
            raise ValueError("GitHub repository detail returned an invalid response")
        release: Mapping[str, Any] = {}
        try:
            latest = await asyncio.to_thread(
                _github_request,
                f"https://api.github.com/repos/{owner_name}/releases/latest",
                self.token,
                accept="application/vnd.github+json",
            )
            if isinstance(latest, Mapping):
                release = latest
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        license_payload = details.get("license") or {}
        return {
            "full_name": details.get("full_name"),
            "html_url": details.get("html_url"),
            "description": details.get("description"),
            "default_branch": details.get("default_branch"),
            "license": (
                license_payload.get("spdx_id")
                if isinstance(license_payload, Mapping)
                else None
            ),
            "archived": bool(details.get("archived")),
            "fork": bool(details.get("fork")),
            "stars": details.get("stargazers_count"),
            "forks": details.get("forks_count"),
            "open_issues": details.get("open_issues_count"),
            "created_at": details.get("created_at"),
            "updated_at": details.get("updated_at"),
            "pushed_at": details.get("pushed_at"),
            "latest_release": {
                "tag_name": release.get("tag_name"),
                "name": release.get("name"),
                "published_at": release.get("published_at"),
                "html_url": release.get("html_url"),
            }
            if release
            else None,
        }


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        del attrs
        if tag.casefold() in {"script", "style", "noscript", "svg"}:
            self._ignored += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg"} and self._ignored:
            self._ignored -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored and data.strip():
            self.parts.append(data.strip())


def _validate_public_host(hostname: str) -> None:
    lowered = hostname.casefold().rstrip(".")
    if lowered in {"localhost", "localhost.localdomain"}:
        raise ValueError("local web targets are not allowed")
    try:
        addresses = socket.getaddrinfo(lowered, None)
    except socket.gaierror as exc:
        raise ValueError(f"web source host cannot be resolved: {lowered}") from exc
    for address in addresses:
        value = ipaddress.ip_address(address[4][0])
        if not value.is_global:
            raise ValueError("private or non-global web targets are not allowed")


def _fetch_static_web(url: str) -> str:
    normalized = canonical_url(url)
    parsed = urllib.parse.urlsplit(normalized)
    assert parsed.hostname
    _validate_public_host(parsed.hostname)
    request = urllib.request.Request(
        normalized,
        headers={"User-Agent": "auto-paper-research-review-harness/0.1"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        final_url = canonical_url(response.geturl())
        final_host = urllib.parse.urlsplit(final_url).hostname
        if not final_host:
            raise ValueError("web source redirected to an invalid URL")
        _validate_public_host(final_host)
        content_type = response.headers.get("Content-Type", "").casefold()
        if not any(content_type.startswith(item) for item in ALLOWED_WEB_CONTENT):
            raise ValueError(f"unsupported web content type: {content_type}")
        data = response.read(MAX_WEB_BYTES + 1)
        if len(data) > MAX_WEB_BYTES:
            raise ValueError("web source exceeded the configured byte limit")
    text = data.decode("utf-8", errors="replace")
    if "html" in content_type:
        parser = _TextExtractor()
        parser.feed(text)
        text = "\n".join(parser.parts)
    return html.unescape(text).strip()


@dataclass(frozen=True)
class RetrievalBatch:
    sources: tuple[SourceRecord, ...]
    errors: tuple[tuple[str, str], ...] = ()


class ReviewProviderRegistry:
    """Execute independent providers concurrently and isolate their failures."""

    def __init__(
        self,
        repository_root: Path,
        working_root: Path,
        *,
        providers: Optional[Mapping[str, RetrievalProvider]] = None,
        semantic_scholar: Optional[SemanticScholarProvider] = None,
        network_concurrency: int = 4,
    ):
        self.repository_root = repository_root.resolve()
        self.working_root = working_root.resolve()
        self.network_concurrency = network_concurrency
        if providers is None:
            configured: dict[str, RetrievalProvider] = {
                "github": GitHubProvider(os.getenv("GITHUB_TOKEN", "")),
            }
            deepxiv = os.getenv("DEEPXIV_TOKEN", "").strip()
            semantic_key = (
                os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()
                or os.getenv("S2_API_KEY", "").strip()
            )
            tavily = os.getenv("TAVILY_API_KEY", "").strip()
            if deepxiv:
                configured["deepxiv"] = DeepXivProvider(deepxiv)
            if tavily:
                configured["tavily"] = TavilyProvider(tavily)
            self.providers = configured
            self.semantic_scholar = semantic_scholar or (
                SemanticScholarProvider(semantic_key) if semantic_key else None
            )
        else:
            self.providers = dict(providers)
            self.semantic_scholar = semantic_scholar

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.providers))

    def require_standard_sources(self) -> None:
        missing = [name for name in ("deepxiv", "tavily", "github") if name not in self.providers]
        if missing:
            credential = {
                "deepxiv": "DEEPXIV_TOKEN",
                "tavily": "TAVILY_API_KEY",
                "github": "GitHub provider",
            }
            raise ValueError(
                "standard review source providers are unavailable: "
                + ", ".join(f"{name} ({credential[name]})" for name in missing)
            )

    def _query_cache_path(self, query: RetrievalQuery) -> Path:
        digest = hashlib.sha256(
            json.dumps(
                query.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        return self.working_root / "provider-results" / f"{query.id}-{digest}.json"

    def _cached_query(
        self, query: RetrievalQuery
    ) -> Optional[tuple[SourceRecord, ...]]:
        path = self._query_cache_path(query)
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if payload.get("query") != query.model_dump(mode="json"):
            raise ValueError(f"provider cache query mismatch: {path.name}")
        return tuple(SourceRecord.model_validate(item) for item in payload.get("sources") or [])

    def _cache_query(
        self, query: RetrievalQuery, sources: Sequence[SourceRecord]
    ) -> None:
        _atomic_json(
            self._query_cache_path(query),
            {
                "schema_version": "0.1",
                "query": query.model_dump(mode="json"),
                "provider": query.provider,
                "sources": [item.model_dump(mode="json") for item in sources],
            },
        )

    async def search(
        self,
        queries: Sequence[RetrievalQuery],
        *,
        limits: Mapping[str, int],
    ) -> RetrievalBatch:
        semaphore = asyncio.Semaphore(self.network_concurrency)
        secrets = (
            os.getenv("DEEPXIV_TOKEN", ""),
            os.getenv("SEMANTIC_SCHOLAR_API_KEY", ""),
            os.getenv("S2_API_KEY", ""),
            os.getenv("TAVILY_API_KEY", ""),
            os.getenv("GITHUB_TOKEN", ""),
        )

        async def one(query: RetrievalQuery) -> tuple[tuple[SourceRecord, ...], Optional[str]]:
            cached = self._cached_query(query)
            if cached is not None:
                return cached, None
            provider = self.providers.get(query.provider)
            if provider is None:
                return (), f"provider {query.provider} is not configured"
            limit = max(0, int(limits.get(query.provider, 0)))
            if limit == 0:
                return (), None
            try:
                async with semaphore:
                    sources = await provider.search(query, limit=limit)
                self._cache_query(query, sources)
                return sources, None
            except Exception as exc:
                return (), _safe_message(exc, secrets)

        results = await asyncio.gather(*(one(item) for item in queries))
        sources = []
        errors = []
        for query, (items, error) in zip(queries, results):
            sources.extend(items)
            if error:
                errors.append((query.id, error))
        return RetrievalBatch(
            sources=tuple(sorted(sources, key=lambda item: item.source_id)),
            errors=tuple(errors),
        )

    async def enrich_source(self, source: SourceRecord) -> SourceRecord:
        """Best-effort caller hook used only after deep-read selection."""

        if self.semantic_scholar is None or source.source_type != "paper":
            return source
        return await self.semantic_scholar.enrich(source)

    async def acquire_material(self, source: SourceRecord) -> SourceMaterial:
        acquired_at = _utc_now()
        if source.source_type == "paper":
            if not source.arxiv_id:
                raise ValueError("automatic paper acquisition currently requires arXiv")
            acquirer = ArxivPaperSourceAcquirer(
                self.repository_root,
                destination_root=self.working_root / "sources" / "papers",
            )
            acquired = await asyncio.to_thread(
                acquirer.acquire,
                {
                    "candidate_id": source.source_id,
                    "source": "arxiv",
                    "source_id": source.arxiv_id,
                    "pdf_url": source.pdf_url,
                    "review_state": "selected-for-ingest",
                },
            )
            document = await asyncio.to_thread(
                extract_pdf_document,
                self.repository_root / acquired.relative_path,
                self.repository_root,
            )
            excerpt = select_paper_excerpt(document, max_pages=16, max_chars=60_000)
            return SourceMaterial(
                source_id=source.source_id,
                media_type="pdf-text",
                sha256=document.sha256,
                text=excerpt.text,
                local_path=acquired.relative_path,
                page_count=len(document.pages),
                selected_pages=excerpt.selected_pages,
                acquired_at=acquired_at,
            )
        if source.source_type == "project":
            if not source.repository:
                raise ValueError("project source has no GitHub repository identity")
            provider = self.providers.get("github")
            if not isinstance(provider, GitHubProvider) and not hasattr(provider, "readme"):
                raise ValueError("GitHub README provider is unavailable")
            readme_task = provider.readme(source.repository)  # type: ignore[attr-defined]
            audit_method = getattr(provider, "audit", None)
            if callable(audit_method):
                readme, audit = await asyncio.gather(
                    readme_task,
                    audit_method(source.repository),
                )
            else:
                readme = await readme_task
                audit = {
                    "repository": source.repository,
                    "license": source.license,
                    "stars": source.stars,
                    "updated_at": source.updated_at,
                    **source.metadata,
                }
            text = (
                "GITHUB OFFICIAL METADATA\n"
                + json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n\nREADME\n"
                + str(readme)
            )
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            return SourceMaterial(
                source_id=source.source_id,
                media_type="repository-readme",
                sha256=digest,
                text=text[:80_000],
                acquired_at=acquired_at,
            )
        text = source.content_preview or await asyncio.to_thread(
            _fetch_static_web, source.canonical_url
        )
        if not text.strip():
            raise ValueError("web source returned no readable content")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return SourceMaterial(
            source_id=source.source_id,
            media_type="web-content",
            sha256=digest,
            text=text[:80_000],
            acquired_at=acquired_at,
        )


__all__ = [
    "DeepXivProvider",
    "GitHubProvider",
    "RetrievalBatch",
    "RetrievalProvider",
    "ReviewProviderRegistry",
    "SemanticScholarProvider",
    "TavilyProvider",
]
