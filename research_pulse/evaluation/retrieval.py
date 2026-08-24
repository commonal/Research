"""Small, deterministic retrieval metrics for the first golden question set."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
import json

from research_pulse.rag.contracts import EvidenceHit


@dataclass(frozen=True)
class GoldenQuestion:
    question_id: str
    query: str
    answerable: bool
    expected_knowledge_ids: tuple[str, ...]
    domain: str | None = None


@dataclass(frozen=True)
class RetrievalMetrics:
    answerable_count: int
    recall_at_k: float
    mean_reciprocal_rank: float
    unexpected_hit_rate_for_unanswerable: float
    current_version_leak_count: int


def load_golden_questions(path: Path) -> list[GoldenQuestion]:
    questions: list[GoldenQuestion] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            questions.append(
                GoldenQuestion(
                    question_id=_string(payload, "question_id", line_number),
                    query=_string(payload, "query", line_number),
                    answerable=_bool(payload, "answerable", line_number),
                    expected_knowledge_ids=tuple(_string_list(payload, "expected_knowledge_ids", line_number)),
                    domain=_nullable_string(payload.get("domain"), "domain", line_number),
                )
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid golden question at line {line_number}: {error}") from error
    if not questions:
        raise ValueError("Golden question set is empty.")
    return questions


def evaluate_retrieval(
    cases: Iterable[tuple[GoldenQuestion, Sequence[EvidenceHit]]], *, k: int
) -> RetrievalMetrics:
    """Measure document recall and detect answers that should have abstained."""

    if k < 1:
        raise ValueError("k must be positive.")
    answerable_count = 0
    recalled_count = 0
    reciprocal_rank_sum = 0.0
    unanswerable_count = 0
    unexpected_unanswerable_hits = 0
    current_version_leaks = 0
    for question, hits in cases:
        top_hits = list(hits[:k])
        if question.answerable:
            answerable_count += 1
            rank = _first_expected_rank(top_hits, set(question.expected_knowledge_ids))
            if rank is not None:
                recalled_count += 1
                reciprocal_rank_sum += 1 / rank
        else:
            unanswerable_count += 1
            if top_hits:
                unexpected_unanswerable_hits += 1
        for hit in top_hits:
            if hit.knowledge_id in question.expected_knowledge_ids and not hit.knowledge_version:
                current_version_leaks += 1
    return RetrievalMetrics(
        answerable_count=answerable_count,
        recall_at_k=_safe_divide(recalled_count, answerable_count),
        mean_reciprocal_rank=_safe_divide(reciprocal_rank_sum, answerable_count),
        unexpected_hit_rate_for_unanswerable=_safe_divide(
            unexpected_unanswerable_hits, unanswerable_count
        ),
        current_version_leak_count=current_version_leaks,
    )


def _first_expected_rank(hits: Sequence[EvidenceHit], expected: set[str]) -> int | None:
    for index, hit in enumerate(hits, start=1):
        if hit.knowledge_id in expected:
            return index
    return None


def _safe_divide(numerator: float, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _string(payload: object, field: str, line_number: int) -> str:
    value = payload[field] if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string at line {line_number}")
    return value.strip()


def _bool(payload: object, field: str, line_number: int) -> bool:
    value = payload[field] if isinstance(payload, dict) else None
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be boolean at line {line_number}")
    return value


def _string_list(payload: object, field: str, line_number: int) -> list[str]:
    value = payload[field] if isinstance(payload, dict) else None
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{field} must be a string list at line {line_number}")
    return [item.strip() for item in value]


def _nullable_string(value: object, field: str, line_number: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string or null at line {line_number}")
    return value.strip()
