"""Deterministic, bounded acquisition of selected public paper PDFs."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

from .network_retry import (
    NetworkRetryPolicy,
    is_transient_network_error,
    retry_sync,
)


MAX_PDF_BYTES = 200 * 1024 * 1024
ALLOWED_ARXIV_HOSTS = frozenset({"arxiv.org", "www.arxiv.org", "export.arxiv.org"})


class PaperSourceError(RuntimeError):
    """Raised when a selected candidate cannot be acquired safely."""


@dataclass(frozen=True)
class AcquiredPaperSource:
    relative_path: str
    source_url: str
    sha256: str
    size_bytes: int
    downloaded: bool


class PaperSourceAcquirer(Protocol):
    requires_network: bool

    def acquire(self, candidate: Mapping[str, Any]) -> AcquiredPaperSource: ...


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


def _normalize_arxiv_id(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(
        r"^https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\.pdf$", "", text, flags=re.IGNORECASE)
    if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[A-Za-z.-]+/\d{7})(?:v\d+)?", text):
        raise PaperSourceError(f"Unsafe or unsupported arXiv identifier: {value!r}")
    return text


def _candidate_url(candidate: Mapping[str, Any], arxiv_id: str) -> str:
    supplied = str(candidate.get("pdf_url") or "").strip()
    if supplied:
        parsed = urllib.parse.urlparse(supplied)
        if (
            parsed.scheme.casefold() != "https"
            or parsed.hostname not in ALLOWED_ARXIV_HOSTS
        ):
            raise PaperSourceError(
                "Selected arXiv PDF URL must use HTTPS on an approved arxiv.org host"
            )
        return supplied
    return f"https://arxiv.org/pdf/{urllib.parse.quote(arxiv_id, safe='/')}.pdf"


def _candidate_urls(candidate: Mapping[str, Any], arxiv_id: str) -> tuple[str, ...]:
    """Return approved arXiv endpoints in deterministic fallback order."""

    encoded = urllib.parse.quote(arxiv_id, safe="/")
    return tuple(
        dict.fromkeys(
            (
                _candidate_url(candidate, arxiv_id),
                f"https://arxiv.org/pdf/{encoded}.pdf",
                f"https://export.arxiv.org/pdf/{encoded}.pdf",
            )
        )
    )


def _validate_pdf(path: Path) -> None:
    size = path.stat().st_size
    if size <= 0 or size > MAX_PDF_BYTES:
        raise PaperSourceError(
            f"Paper PDF size must be between 1 and {MAX_PDF_BYTES} bytes"
        )
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise PaperSourceError("Downloaded source is not a PDF file")


class ArxivPaperSourceAcquirer:
    """Download only an explicitly selected arXiv candidate into the repository."""

    requires_network = True

    def __init__(
        self,
        repository_root: Path,
        *,
        destination_root: Optional[Path] = None,
        timeout_seconds: Optional[int] = None,
        max_attempts: Optional[int] = None,
        backoff_seconds: Optional[float] = None,
    ):
        defaults = NetworkRetryPolicy.from_env()
        timeout_seconds = (
            defaults.read_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        max_attempts = defaults.max_attempts if max_attempts is None else max_attempts
        backoff_seconds = (
            defaults.backoff_seconds
            if backoff_seconds is None
            else backoff_seconds
        )
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if backoff_seconds < 0:
            raise ValueError("backoff_seconds cannot be negative")
        self.repository_root = repository_root.resolve()
        self.destination_root = (
            destination_root or self.repository_root / "sources" / "papers"
        ).resolve()
        if not _is_within(self.destination_root, self.repository_root):
            raise ValueError("Paper source destination must stay inside the repository")
        self.timeout_seconds = timeout_seconds
        self.retry_policy = NetworkRetryPolicy(
            max_attempts=max_attempts,
            connect_timeout_seconds=defaults.connect_timeout_seconds,
            read_timeout_seconds=timeout_seconds,
            backoff_seconds=backoff_seconds,
            max_backoff_seconds=defaults.max_backoff_seconds,
        )

    def acquire(self, candidate: Mapping[str, Any]) -> AcquiredPaperSource:
        if str(candidate.get("review_state") or "") != "selected-for-ingest":
            raise PaperSourceError(
                "Only a selected-for-ingest candidate can be acquired"
            )
        if str(candidate.get("source") or "").casefold() != "arxiv":
            raise PaperSourceError(
                "V0 automatic source acquisition supports selected arXiv candidates only"
            )
        arxiv_id = _normalize_arxiv_id(candidate.get("source_id"))
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", arxiv_id.replace("/", "-"))
        destination = (self.destination_root / f"arxiv-{safe_stem}.pdf").resolve()
        if not _is_within(destination, self.destination_root):
            raise PaperSourceError("Resolved PDF path escapes the source directory")
        urls = _candidate_urls(candidate, arxiv_id)
        url = urls[0]
        self.destination_root.mkdir(parents=True, exist_ok=True)
        if destination.is_file():
            _validate_pdf(destination)
            return AcquiredPaperSource(
                relative_path=destination.relative_to(self.repository_root).as_posix(),
                source_url=url,
                sha256=_sha256(destination),
                size_bytes=destination.stat().st_size,
                downloaded=False,
            )

        def download_once(attempt: int) -> AcquiredPaperSource:
            selected_url = urls[(attempt - 1) % len(urls)]
            request = urllib.request.Request(
                selected_url,
                headers={"User-Agent": "llm-wiki-research-harness/0.2"},
            )
            temporary: Optional[Path] = None
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as response:
                    final_url = response.geturl()
                    parsed_final = urllib.parse.urlparse(final_url)
                    if (
                        parsed_final.scheme.casefold() != "https"
                        or parsed_final.hostname not in ALLOWED_ARXIV_HOSTS
                    ):
                        raise PaperSourceError(
                            "arXiv download redirected to an unapproved host"
                        )
                    declared = response.headers.get("Content-Length")
                    if declared and int(declared) > MAX_PDF_BYTES:
                        raise PaperSourceError(
                            "Paper PDF exceeds the configured byte limit"
                        )
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        delete=False,
                        dir=str(self.destination_root),
                        prefix=f".{destination.stem}-",
                        suffix=".tmp",
                    ) as handle:
                        temporary = Path(handle.name)
                        total = 0
                        while chunk := response.read(1024 * 1024):
                            total += len(chunk)
                            if total > MAX_PDF_BYTES:
                                raise PaperSourceError(
                                    "Paper PDF exceeded the configured byte limit while downloading"
                                )
                            handle.write(chunk)
                _validate_pdf(temporary)
                os.replace(temporary, destination)
                temporary = None
                return AcquiredPaperSource(
                    relative_path=destination.relative_to(
                        self.repository_root
                    ).as_posix(),
                    source_url=final_url,
                    sha256=_sha256(destination),
                    size_bytes=destination.stat().st_size,
                    downloaded=True,
                )
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()

        def retryable(exc: BaseException) -> bool:
            return is_transient_network_error(exc) or (
                isinstance(exc, PaperSourceError)
                and "Downloaded source is not a PDF file" in str(exc)
            )

        try:
            return retry_sync(
                download_once,
                policy=self.retry_policy,
                should_retry=retryable,
            )
        except PaperSourceError:
            raise
        except Exception as exc:
            raise PaperSourceError(
                "arXiv PDF download failed after "
                f"{self.retry_policy.max_attempts} attempts: "
                f"{type(exc).__name__}: {exc}"
            ) from exc


__all__ = [
    "ALLOWED_ARXIV_HOSTS",
    "AcquiredPaperSource",
    "ArxivPaperSourceAcquirer",
    "MAX_PDF_BYTES",
    "PaperSourceAcquirer",
    "PaperSourceError",
]
