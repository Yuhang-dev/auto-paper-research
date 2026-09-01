"""Reusable console heartbeat and persisted progress snapshots."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol, TextIO

from .text_normalization import normalize_data, normalize_text


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
            json.dump(
                normalize_data(payload),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class ProgressSink(Protocol):
    def update(
        self,
        *,
        stage: str,
        detail: str,
        completed: Optional[int] = None,
        total: Optional[int] = None,
    ) -> None: ...


class NullProgress:
    def update(
        self,
        *,
        stage: str,
        detail: str,
        completed: Optional[int] = None,
        total: Optional[int] = None,
    ) -> None:
        del stage, detail, completed, total


class ConsoleProgress:
    """Show one live console line and persist a heartbeat for status commands."""

    def __init__(
        self,
        *,
        path: Path,
        stream: Optional[TextIO] = None,
        mode: Optional[str] = None,
        heartbeat_seconds: float = 1.0,
        persist_seconds: float = 5.0,
    ) -> None:
        self.path = path
        self.stream = stream or sys.stderr
        configured = (mode or os.getenv("HARNESS_PROGRESS", "auto")).strip().casefold()
        if configured not in {"auto", "live", "plain", "off"}:
            raise ValueError("HARNESS_PROGRESS must be auto, live, plain, or off")
        interactive = bool(getattr(self.stream, "isatty", lambda: False)())
        if configured == "auto":
            configured = "live" if interactive else "plain"
        self.display_mode = configured
        self.heartbeat_seconds = heartbeat_seconds
        self.persist_seconds = persist_seconds
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started_monotonic = time.monotonic()
        self._stage_started_monotonic = self._started_monotonic
        self._last_progress_monotonic = self._started_monotonic
        self._last_persist_monotonic = 0.0
        self._last_plain_monotonic = 0.0
        self._last_width = 0
        self._tick = 0
        self._snapshot: dict[str, Any] = {
            "schema_version": "0.1",
            "status": "starting",
            "stage": "bootstrap",
            "detail": "Preparing review run",
            "completed": None,
            "total": None,
            "started_at": _utc_now(),
            "updated_at": _utc_now(),
            "heartbeat_at": _utc_now(),
            "last_progress_at": _utc_now(),
            "elapsed_seconds": 0.0,
            "stage_elapsed_seconds": 0.0,
            "seconds_since_progress": 0.0,
        }

    def __enter__(self) -> "ConsoleProgress":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del traceback
        if exc_type is KeyboardInterrupt:
            self.finish(status="interrupted", detail="Interrupted; checkpoint preserved")
        elif exc is not None:
            self.finish(status="failed", detail=f"{type(exc).__name__}; see command error")
        else:
            self.finish(status="completed", detail="Review command completed")

    def start(self) -> None:
        self._persist(force=True)
        self._thread = threading.Thread(
            target=self._heartbeat,
            name="harness-progress",
            daemon=True,
        )
        self._thread.start()

    def update(
        self,
        *,
        stage: str,
        detail: str,
        completed: Optional[int] = None,
        total: Optional[int] = None,
    ) -> None:
        now = time.monotonic()
        with self._lock:
            previous_stage = self._snapshot["stage"]
            if stage != previous_stage:
                if self.display_mode == "live" and self._snapshot["status"] == "running":
                    self._render_live_locked(final=True, marker="done")
                self._stage_started_monotonic = now
            self._snapshot.update(
                {
                    "status": "running",
                    "stage": normalize_text(stage),
                    "detail": normalize_text(" ".join(detail.split())),
                    "completed": completed,
                    "total": total,
                }
            )
            self._last_progress_monotonic = now
            self._snapshot["last_progress_at"] = _utc_now()
            self._refresh_times(now)
            self._persist_locked(now, force=True)
            if self.display_mode == "live":
                self._render_live_locked()
            if self.display_mode == "plain" and (
                stage != previous_stage or now - self._last_plain_monotonic >= 30
            ):
                self._write_plain_locked()
                self._last_plain_monotonic = now

    def finish(self, *, status: str, detail: str) -> None:
        with self._lock:
            self._snapshot.update(
                {
                    "status": status,
                    "detail": normalize_text(" ".join(detail.split())),
                }
            )
            now = time.monotonic()
            self._last_progress_monotonic = now
            self._snapshot["last_progress_at"] = _utc_now()
            self._refresh_times(now)
            self._persist_locked(now, force=True)
            if self.display_mode == "live":
                self._render_live_locked(final=True, marker=status)
            elif self.display_mode == "plain":
                self._write_plain_locked(marker=status)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.heartbeat_seconds * 2))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._snapshot)

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            with self._lock:
                now = time.monotonic()
                self._tick += 1
                self._refresh_times(now)
                self._persist_locked(now)
                if self.display_mode == "live":
                    self._render_live_locked()
                elif (
                    self.display_mode == "plain"
                    and now - self._last_plain_monotonic >= 30
                ):
                    self._write_plain_locked()
                    self._last_plain_monotonic = now

    def _refresh_times(self, now: float) -> None:
        heartbeat_at = _utc_now()
        self._snapshot["updated_at"] = heartbeat_at
        self._snapshot["heartbeat_at"] = heartbeat_at
        self._snapshot["elapsed_seconds"] = round(now - self._started_monotonic, 1)
        self._snapshot["stage_elapsed_seconds"] = round(
            now - self._stage_started_monotonic, 1
        )
        self._snapshot["seconds_since_progress"] = round(
            now - self._last_progress_monotonic, 1
        )

    def _persist(self, *, force: bool = False) -> None:
        with self._lock:
            self._persist_locked(time.monotonic(), force=force)

    def _persist_locked(self, now: float, *, force: bool = False) -> None:
        if not force and now - self._last_persist_monotonic < self.persist_seconds:
            return
        _atomic_json(self.path, self._snapshot)
        self._last_persist_monotonic = now

    def _line(
        self,
        *,
        marker: Optional[str] = None,
        max_width: Optional[int] = None,
    ) -> str:
        completed = self._snapshot.get("completed")
        total = self._snapshot.get("total")
        count = f" {completed}/{total}" if completed is not None and total else ""
        prefix = f"[{self._snapshot['stage']}]{count}"
        elapsed = int(float(self._snapshot.get("elapsed_seconds") or 0))
        timer = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
        if marker == "done" or marker == "completed":
            suffix = " done"
        elif marker == "interrupted":
            suffix = " interrupted"
        elif marker == "failed":
            suffix = " failed"
        else:
            suffix = "." * (self._tick % 4)
        since_progress = int(float(self._snapshot.get("seconds_since_progress") or 0))
        progress_age = ""
        if marker is None and since_progress >= 10:
            progress_age = (
                f" · last +{since_progress // 60:02d}:{since_progress % 60:02d}"
            )
        tail = f" · {timer}{progress_age}{suffix}"
        detail = str(self._snapshot["detail"])
        if max_width is not None:
            available = max(8, max_width - len(prefix) - len(tail) - 3)
            if len(detail) > available:
                detail = detail[: available - 1] + "…"
        return f"{prefix} · {detail}{tail}"

    def _render_live_locked(
        self,
        *,
        final: bool = False,
        marker: Optional[str] = None,
    ) -> None:
        width = max(40, shutil.get_terminal_size((120, 20)).columns)
        clipped = self._line(marker=marker, max_width=width - 1)[: width - 1]
        padded = clipped.ljust(min(max(self._last_width, len(clipped)), width - 1))
        self.stream.write("\r" + padded)
        if final:
            self.stream.write("\n")
            self._last_width = 0
        else:
            self._last_width = len(clipped)
        self.stream.flush()

    def _write_plain_locked(self, *, marker: Optional[str] = None) -> None:
        self.stream.write(self._line(marker=marker) + "\n")
        self.stream.flush()


def read_progress(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return payload if isinstance(payload, dict) else None


__all__ = ["ConsoleProgress", "NullProgress", "ProgressSink", "read_progress"]
