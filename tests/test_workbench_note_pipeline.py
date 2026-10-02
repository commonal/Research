from pathlib import Path
from unittest import TestCase

from research_pulse.traceable_reading.service import TraceableReadingOutcome, TraceableReadingRequest
from research_pulse.workbench.note_pipeline import EvidenceGap, FixedTwoStageNotePipeline, PlannedReadingResult


REQUEST = TraceableReadingRequest("paper-1", "urn:paper-1", "research", Path("knowledge"), Path("data"), material_root=Path("material"))


class _Planned:
    def __init__(self, gaps=()): self.gaps = gaps; self.calls = 0
    def run_planned(self, request):
        self.calls += 1
        return PlannedReadingResult(TraceableReadingOutcome(0, {"publication_status": "published"}), self.gaps)


class _Supplement:
    def __init__(self): self.calls = []
    def run_supplement(self, request, gaps):
        self.calls.append((request, gaps))
        return TraceableReadingOutcome(0, {"publication_status": "published", "supplemented": True})


class WorkbenchFixedNotePipelineTests(TestCase):
    def test_complete_planned_read_never_invokes_agentic_supplement(self) -> None:
        planned, supplement = _Planned(), _Supplement()
        outcome = FixedTwoStageNotePipeline(planned, supplement).run(REQUEST)
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(supplement.calls, [])

    def test_only_explicit_block_bounded_gaps_enable_limited_supplement(self) -> None:
        gap = EvidenceGap("gap-method", "方法边界是什么？", ("block:method",))
        planned, supplement = _Planned((gap,)), _Supplement()
        outcome = FixedTwoStageNotePipeline(planned, supplement).run(REQUEST)
        self.assertTrue(outcome.payload["supplemented"])
        self.assertEqual(supplement.calls[0][1], (gap,))

    def test_gap_count_over_budget_fails_without_supplement(self) -> None:
        gaps = tuple(EvidenceGap(f"g-{i}", "question", (f"block:{i}",)) for i in range(4))
        supplement = _Supplement()
        outcome = FixedTwoStageNotePipeline(_Planned(gaps), supplement, max_gaps=3).run(REQUEST)
        self.assertEqual(outcome.exit_code, 1)
        self.assertEqual(supplement.calls, [])
