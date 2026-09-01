"""Optional Cross-Encoder reranking for bounded Ask V2 evidence candidates."""

from __future__ import annotations

import math
import os
from threading import Lock
from typing import Any


class AskCrossEncoderReranker:
    """Lazy process-local Cross-Encoder with a deterministic fallback."""

    def __init__(self, model_name: str, *, proxy: str | None = None) -> None:
        self.model_name = str(model_name or "").strip()
        self.proxy = str(proxy or "").strip() or None
        self._model: Any = None
        self._lock = Lock()
        self.status = "not_loaded"

    def _load(self) -> bool:
        if self._model is not None:
            return True
        with self._lock:
            if self._model is not None:
                return True
            try:
                if self.proxy:
                    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                        os.environ[key] = self.proxy
                    os.environ.pop("HF_HUB_OFFLINE", None)
                else:
                    os.environ.setdefault("HF_HUB_OFFLINE", "1")
                import numpy as np
                import numpy.core as np_core
                if not hasattr(np, "_core"):
                    np._core = np_core
                if not hasattr(np._core, "multiarray"):
                    np._core.multiarray = np_core.multiarray
                from sentence_transformers import CrossEncoder
                self._model = CrossEncoder(
                    self.model_name,
                    max_length=512,
                    model_kwargs={"local_files_only": not bool(self.proxy)},
                )
                self.status = "ready"
                return True
            except Exception as exc:
                self.status = f"unavailable:{type(exc).__name__}"
                return False

    @staticmethod
    def _text(record: dict[str, Any]) -> str:
        scope = record.get("scope") if isinstance(record.get("scope"), dict) else {}
        values = (
            record.get("content"), record.get("text"), record.get("summary"),
            record.get("title"), record.get("subject"), record.get("predicate"),
            record.get("object_value"), scope.get("canonical_topic"),
            scope.get("semantic_facet"),
        )
        return " ".join(str(value or "") for value in values)[:3600]

    @staticmethod
    def _normalize(values: dict[str, float]) -> dict[str, float]:
        if not values:
            return {}
        low, high = min(values.values()), max(values.values())
        if math.isclose(low, high):
            return {key: 1.0 for key in values}
        return {key: (value - low) / (high - low) for key, value in values.items()}

    @staticmethod
    def _heuristic(query: str, records: list[dict[str, Any]]) -> dict[str, float]:
        query_text = str(query or "").casefold()
        query_terms = {term for term in query_text.split() if term}
        scores: dict[str, float] = {}
        for record in records:
            record_id = str(record.get("id") or record.get("memory_id") or record.get("note_id") or "")
            text = AskCrossEncoderReranker._text(record).casefold()
            overlap = sum(1 for term in query_terms if term in text) / max(1, len(query_terms))
            scores[record_id] = overlap + (0.1 if query_text and query_text in text else 0.0)
        return scores

    def rerank(self, query: str, records: list[dict[str, Any]], *, alpha: float = 0.75) -> list[dict[str, Any]]:
        if len(records) < 2:
            return records
        ids = [str(record.get("id") or record.get("memory_id") or record.get("note_id") or "") for record in records]
        base = self._normalize({
            record_id: float(record.get("score") or record.get("rerank_score") or 0.0)
            for record, record_id in zip(records, ids, strict=True)
        })
        cross: dict[str, float] = {}
        if self._load():
            pairs = [(str(query), self._text(record)) for record in records]
            try:
                with self._lock:
                    values = self._model.predict(pairs, batch_size=16, show_progress_bar=False)
                cross = {record_id: float(value) for record_id, value in zip(ids, values, strict=True)}
            except Exception as exc:
                self.status = f"failed:{type(exc).__name__}"
        if not cross:
            cross = self._heuristic(query, records)
            if self.status.startswith(("unavailable", "failed")):
                self.status += "+heuristic_fallback"
        cross = self._normalize(cross)
        weight = min(0.95, max(0.05, float(alpha)))
        scored: list[tuple[float, int, dict[str, Any]]] = []
        for index, (record, record_id) in enumerate(zip(records, ids, strict=True)):
            final_score = weight * cross.get(record_id, 0.0) + (1.0 - weight) * base.get(record_id, 0.0)
            enriched = dict(record)
            enriched["ask_cross_score"] = round(cross.get(record_id, 0.0), 6)
            enriched["ask_rerank_score"] = round(final_score, 6)
            enriched["ask_rerank_status"] = self.status
            scored.append((final_score, -index, enriched))
        return [item for _score, _index, item in sorted(scored, key=lambda row: (row[0], row[1]), reverse=True)]


_RERANKER: AskCrossEncoderReranker | None = None
_RERANKER_KEY: tuple[str, str] | None = None


def rerank_ask_records(query: str, records: list[dict[str, Any]], *, model_name: str, proxy: str | None, alpha: float) -> list[dict[str, Any]]:
    global _RERANKER, _RERANKER_KEY
    key = (str(model_name or ""), str(proxy or ""))
    if _RERANKER is None or _RERANKER_KEY != key:
        _RERANKER = AskCrossEncoderReranker(key[0], proxy=key[1] or None)
        _RERANKER_KEY = key
    return _RERANKER.rerank(query, records, alpha=alpha)
