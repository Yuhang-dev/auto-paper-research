"""Small, explicit research-memory layer over LangGraph's SQLite store."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from langgraph.store.base import BaseStore


MEMORY_KINDS = {"observation", "decision", "preference", "open-question"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def memory_namespace(workspace_id: str) -> Tuple[str, ...]:
    return "research-harness", workspace_id, "notes"


def _normalized_evidence(values: Optional[Sequence[str]]) -> List[str]:
    unique: List[str] = []
    for value in values or []:
        text = str(value).strip()
        if text and text not in unique:
            unique.append(text)
        if len(unique) >= 20:
            break
    return unique


def remember_note(
    store: BaseStore,
    workspace_id: str,
    *,
    text: str,
    topic: str = "general",
    kind: str = "observation",
    evidence_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    clean_text = text.strip()
    clean_topic = topic.strip() or "general"
    if not clean_text:
        raise ValueError("Research memory text cannot be empty")
    if len(clean_text) > 4000:
        raise ValueError("Research memory text cannot exceed 4000 characters")
    if len(clean_topic) > 200:
        raise ValueError("Research memory topic cannot exceed 200 characters")
    if kind not in MEMORY_KINDS:
        raise ValueError(f"Unsupported research memory kind: {kind}")
    evidence = _normalized_evidence(evidence_ids)
    fingerprint_payload = {
        "text": clean_text,
        "topic": clean_topic,
        "kind": kind,
        "evidence_ids": evidence,
    }
    key = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    namespace = memory_namespace(workspace_id)
    existing = store.get(namespace, key)
    now = utc_now()
    value = {
        **fingerprint_payload,
        "created_at": existing.value.get("created_at", now) if existing else now,
        "updated_at": now,
        "confirmations": int(existing.value.get("confirmations", 0)) + 1
        if existing
        else 1,
        "evidence_status": "grounded" if evidence else "unverified-note",
    }
    store.put(namespace, key, value, index=False)
    return {"key": key, **value}


def list_notes(
    store: BaseStore,
    workspace_id: str,
    *,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    bounded_limit = max(1, min(int(limit), 200))
    items = store.search(memory_namespace(workspace_id), limit=bounded_limit)
    records = [{"key": item.key, **dict(item.value)} for item in items]
    return sorted(
        records,
        key=lambda item: (
            str(item.get("updated_at") or ""),
            str(item.get("key") or ""),
        ),
        reverse=True,
    )


def _query_terms(query: str) -> set[str]:
    terms = {
        token
        for token in re.findall(r"[\w:-]+", query.casefold(), flags=re.UNICODE)
        if len(token) > 1
    }
    for sequence in re.findall(r"[\u3400-\u9fff]+", query):
        terms.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return terms


def recall_notes(
    store: BaseStore,
    workspace_id: str,
    *,
    query: str = "",
    limit: int = 8,
) -> List[Dict[str, Any]]:
    candidates = list_notes(store, workspace_id, limit=200)
    terms = _query_terms(query)
    ranked: List[Tuple[int, str, Dict[str, Any]]] = []
    for record in candidates:
        haystack = " ".join(
            [
                str(record.get("text") or ""),
                str(record.get("topic") or ""),
                " ".join(record.get("evidence_ids") or []),
            ]
        ).casefold()
        score = sum(term in haystack for term in terms)
        if terms and score == 0:
            continue
        ranked.append((score, str(record.get("updated_at") or ""), record))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [record for _, _, record in ranked[: max(1, min(int(limit), 20))]]


def render_memory_context(records: Iterable[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for record in records:
        evidence = record.get("evidence_ids") or []
        suffix = f" evidence={','.join(evidence)}" if evidence else " unverified"
        lines.append(
            f"- [{record.get('kind')}/{record.get('topic')}] "
            f"{record.get('text')}{suffix}"
        )
    return "\n".join(lines) if lines else "- No recalled research memory."
