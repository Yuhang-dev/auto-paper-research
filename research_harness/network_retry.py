"""Deterministic retry policy for bounded external network operations."""

from __future__ import annotations

import asyncio
import os
import socket
import time
import urllib.error
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, TypeVar


T = TypeVar("T")
TRANSIENT_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


@dataclass(frozen=True)
class NetworkRetryPolicy:
    """Small, reproducible retry budget shared by providers and downloads."""

    max_attempts: int = 3
    connect_timeout_seconds: int = 20
    read_timeout_seconds: int = 180
    backoff_seconds: float = 2.0
    max_backoff_seconds: float = 30.0

    @classmethod
    def from_env(
        cls,
        prefix: str = "HARNESS_NETWORK",
        *,
        max_attempts: int = 3,
        connect_timeout_seconds: int = 20,
        read_timeout_seconds: int = 180,
        backoff_seconds: float = 2.0,
        max_backoff_seconds: float = 30.0,
    ) -> "NetworkRetryPolicy":
        return cls(
            max_attempts=_env_int(
                f"{prefix}_MAX_ATTEMPTS",
                max_attempts,
                minimum=1,
                maximum=8,
            ),
            connect_timeout_seconds=_env_int(
                f"{prefix}_CONNECT_TIMEOUT_SECONDS",
                connect_timeout_seconds,
                minimum=1,
                maximum=300,
            ),
            read_timeout_seconds=_env_int(
                f"{prefix}_READ_TIMEOUT_SECONDS",
                read_timeout_seconds,
                minimum=5,
                maximum=1800,
            ),
            backoff_seconds=_env_float(
                f"{prefix}_BACKOFF_SECONDS",
                backoff_seconds,
                minimum=0,
                maximum=120,
            ),
            max_backoff_seconds=_env_float(
                f"{prefix}_MAX_BACKOFF_SECONDS",
                max_backoff_seconds,
                minimum=0,
                maximum=600,
            ),
        )

    def delay_before(self, next_attempt: int) -> float:
        """Return the delay before a 1-based retry attempt."""

        if next_attempt <= 1:
            return 0.0
        return min(
            self.backoff_seconds * (2 ** (next_attempt - 2)),
            self.max_backoff_seconds,
        )


def _exception_chain(exc: BaseException):
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_transient_network_error(exc: BaseException) -> bool:
    """Recognize timeout, temporary connection, and retryable HTTP failures."""

    for current in _exception_chain(exc):
        if isinstance(current, urllib.error.HTTPError):
            return current.code in TRANSIENT_HTTP_STATUS
        if isinstance(
            current,
            (
                TimeoutError,
                socket.timeout,
                ConnectionError,
                ConnectionResetError,
                ConnectionAbortedError,
                BrokenPipeError,
                urllib.error.URLError,
            ),
        ):
            return True
        class_name = type(current).__name__.casefold()
        if "timeout" in class_name or class_name in {
            "connecterror",
            "networkerror",
            "remoteprotocolerror",
        }:
            return True
    return False


def retry_sync(
    operation: Callable[[int], T],
    *,
    policy: NetworkRetryPolicy,
    should_retry: Callable[[BaseException], bool] = is_transient_network_error,
    sleeper: Optional[Callable[[float], None]] = None,
    retry_delay: Optional[Callable[[BaseException, int], Optional[float]]] = None,
) -> T:
    """Run a synchronous operation with a fixed attempt budget."""

    sleep = time.sleep if sleeper is None else sleeper
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return operation(attempt)
        except Exception as exc:
            if attempt >= policy.max_attempts or not should_retry(exc):
                raise
            next_attempt = attempt + 1
            delay = policy.delay_before(next_attempt)
            if retry_delay is not None:
                advised = retry_delay(exc, next_attempt)
                if advised is not None:
                    delay = max(delay, advised)
            if delay:
                sleep(delay)
    raise RuntimeError("network retry budget exhausted")


async def retry_async(
    operation: Callable[[int], Awaitable[T]],
    *,
    policy: NetworkRetryPolicy,
    should_retry: Callable[[BaseException], bool] = is_transient_network_error,
    retry_delay: Optional[Callable[[BaseException, int], Optional[float]]] = None,
) -> T:
    """Run an asynchronous operation with the same deterministic policy."""

    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await operation(attempt)
        except Exception as exc:
            if attempt >= policy.max_attempts or not should_retry(exc):
                raise
            next_attempt = attempt + 1
            delay = policy.delay_before(next_attempt)
            if retry_delay is not None:
                advised = retry_delay(exc, next_attempt)
                if advised is not None:
                    delay = max(delay, advised)
            if delay:
                await asyncio.sleep(delay)
    raise RuntimeError("network retry budget exhausted")


__all__ = [
    "NetworkRetryPolicy",
    "TRANSIENT_HTTP_STATUS",
    "is_transient_network_error",
    "retry_async",
    "retry_sync",
]
