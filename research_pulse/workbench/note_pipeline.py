"""Fixed two-stage note workflow with an explicit evidence-gap gate."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import time
from typing import Protocol

from research_pulse.traceable_reading.service import TraceableReadingOutcome, TraceableReadingRequest


class OperationObserver(Protocol):
    def __call__(self, operation: str, model: str, usage: Mapping[str, object] | None, elapsed_ms: int) -> None: ...


class ObservedReadingProvider:
    """Wrap a reading provider to record each (operation, model, usage, elapsed).

    Lives entirely on the workbench side of the seam so the parallel
    ``production/reading.py`` refactor is never touched. The wrapped provider
    is expected to expose ``text_model`` and ``call_json(operation, model,
    prompt[, image])`` (the shape ``DeepSeekOperationAdapter`` already uses).
    """

    def __init__(self, provider: object, observer: OperationObserver) -> None:
        self._provider = provider
        self._observer = observer
        self.text_model = getattr(provider, "text_model", "")

    def call_json(self, operation: str, model: str, prompt: str, image: str | None = None) -> Mapping[str, object]:
        started = time.monotonic()
        response = self._provider.call_json(operation, model, prompt, image=image)  # type: ignore[call-arg]
        elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
        usage = response.get("usage") if isinstance(response, Mapping) else None
        self._observer(operation, model, usage, elapsed_ms)
        return response


@dataclass(frozen=True)
class EvidenceGap:
    gap_id: str
    question: str
    allowed_block_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.gap_id.strip() or not self.question.strip() or not self.allowed_block_ids:
            raise ValueError("an evidence gap must be explicit and block-bounded")


@dataclass(frozen=True)
class PlannedReadingResult:
    outcome: TraceableReadingOutcome
    evidence_gaps: tuple[EvidenceGap, ...] = ()


class PlannedReadingStage(Protocol):
    def run_planned(self, request: TraceableReadingRequest) -> PlannedReadingResult: ...


class SupplementalReadingStage(Protocol):
    def run_supplement(
        self, request: TraceableReadingRequest, gaps: tuple[EvidenceGap, ...]
    ) -> TraceableReadingOutcome: ...


class TraceablePlannedReading:
    """Adapter: the existing traceable pipeline is the complete planned stage."""
    def __init__(self, service) -> None:
        self.service = service

    def run_planned(self, request: TraceableReadingRequest) -> PlannedReadingResult:
        return PlannedReadingResult(self.service.run(request))


class FixedTwoStageNotePipeline:
    def __init__(self, planned: PlannedReadingStage, supplement: SupplementalReadingStage, *, max_gaps: int = 3) -> None:
        if max_gaps < 1: raise ValueError("max_gaps must be positive")
        self.planned = planned
        self.supplement = supplement
        self.max_gaps = max_gaps

    def run(self, request: TraceableReadingRequest) -> TraceableReadingOutcome:
        first = self.planned.run_planned(request)
        if not first.evidence_gaps:
            return first.outcome
        if len(first.evidence_gaps) > self.max_gaps:
            return TraceableReadingOutcome(1, {
                "publication_status": "failed",
                "error": "explicit_evidence_gap_budget_exceeded",
            })
        return self.supplement.run_supplement(request, first.evidence_gaps)
