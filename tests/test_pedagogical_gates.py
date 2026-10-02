"""S7 Evidence Gate + S8 Blind Reader 单测（不联网，用假 call_json/judge）。"""

from __future__ import annotations

import unittest

from research_pulse.pedagogical.contracts import (
    BlindReaderResult,
    EvidenceGateResult,
    PaperModel,
    TeachingPlan,
)
from research_pulse.pedagogical.gates import BlindReader, EvidenceGate


class _FakeModel:
    """有 call_json + text_model 即可驱动两个 gate。"""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.text_model = "fake-text"
        self.calls = 0

    def call_json(self, operation: str, model: str, prompt: str, image: str | None = None) -> dict:
        self.calls += 1
        return self._responses.pop(0)


def _msg(md: str):
    from research_pulse.pedagogical.contracts import RenderResult, RenderedNote

    return RenderedNote(markdown=md, render_results=(RenderResult("a", "rendered", ""),))


class EvidenceGateTests(unittest.TestCase):
    def test_reported_hard_issue_forces_failure_even_if_judge_sets_passed_true(self) -> None:
        result = EvidenceGate(_FakeModel([
            {"passed": True, "issues": ["结论与论文事实冲突"]},
        ])).evaluate(_msg("note"), PaperModel(thesis="t"))

        self.assertFalse(result.passed)
        self.assertEqual(("结论与论文事实冲突",), result.issues)

    def test_unexplained_failure_retries_once_and_uses_explained_result(self) -> None:
        model = _FakeModel([
            {"passed": False, "issues": []},
            {"passed": False, "issues": ["第 3 行的数值没有论文事实支持"]},
        ])

        result = EvidenceGate(model).evaluate(_msg("note"), PaperModel(thesis="t"))

        self.assertFalse(result.passed)
        self.assertEqual(("第 3 行的数值没有论文事实支持",), result.issues)
        self.assertEqual(2, model.calls)

    def test_persistent_unexplained_failure_returns_explicit_contract_issue(self) -> None:
        model = _FakeModel([
            {"passed": False, "issues": []},
            {"passed": False, "issues": []},
        ])

        result = EvidenceGate(model).evaluate(_msg("note"), PaperModel(thesis="t"))

        self.assertFalse(result.passed)
        self.assertEqual(2, model.calls)
        self.assertEqual(1, len(result.issues))
        self.assertTrue(result.issues[0].startswith("evidence_gate_invalid_response:"))

    def test_pass_with_no_issues(self) -> None:
        gate = EvidenceGate(_FakeModel([{"passed": True, "issues": []}]))
        result = gate.evaluate(_msg("note"), PaperModel(thesis="t"))
        self.assertTrue(result.passed)
        self.assertEqual(result.issues, ())

    def test_fail_captures_issues(self) -> None:
        gate = EvidenceGate(_FakeModel([{"passed": False, "issues": ["声称 X 但无证据"]}]))
        result = gate.evaluate(_msg("note"), PaperModel(thesis="t"))
        self.assertFalse(result.passed)
        self.assertIn("声称 X 但无证据", result.issues)


class BlindReaderTests(unittest.TestCase):
    def _dims(self, scores: dict[str, str]) -> list[dict]:
        from research_pulse.pedagogical.gates import BLIND_DIMENSIONS

        return [{"dimension": d, "score": scores.get(d, "clear"), "evidence": "", "note": ""} for d in BLIND_DIMENSIONS]

    def test_pass_recomputed_from_scores(self) -> None:
        scores = {d: "clear" for d in ("background", "prior_gap", "mechanism", "formalism", "experiment", "visual", "boundary")}
        reader = BlindReader(_FakeModel([{"overall": "fail", "dimensions": self._dims(scores)}]))
        result = reader.read(_msg("note"))
        # LLM 返回 fail 但分数全 clear → 契约重推为 pass（守 SPEC 的分数口径）
        self.assertEqual(result.overall, "pass")
        self.assertEqual(len(result.dimensions), 7)

    def test_three_missing_is_fail(self) -> None:
        scores = {"background": "clear", "mechanism": "missing", "formalism": "missing", "experiment": "missing"}
        reader = BlindReader(_FakeModel([{"overall": "pass", "dimensions": self._dims(scores)}]))
        result = reader.read(_msg("note"))
        self.assertEqual(result.overall, "fail")  # ≥3 missing → fail

    def test_one_missing_is_needs_targeted_revision(self) -> None:
        scores = {"visual": "missing"}
        reader = BlindReader(_FakeModel([{"overall": "pass", "dimensions": self._dims(scores)}]))
        result = reader.read(_msg("note"))
        self.assertEqual(result.overall, "needs_targeted_revision")

    def test_blind_reader_takes_only_note(self) -> None:
        # 契约：BlindReader.read 签名只有 note —— 不允许注入 PaperModel/原文。
        import inspect

        sig = inspect.signature(BlindReader.read)
        self.assertEqual(list(sig.parameters), ["self", "note"])


class _FakeCallBackend:
    """可充作 DeepSeekPaperReadingModel：call_json 按序返回 dict + text_model 属性。"""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.text_model = "fake-text"
        self.vision_model = None

    def call_json(self, operation: str, model: str, prompt: str, image: str | None = None) -> dict:
        return self._responses.pop(0)


class PedagogicalServiceRepairTests(unittest.TestCase):
    def _backend(self, note_markdowns: list[str]) -> _FakeCallBackend:
        return _FakeCallBackend([
            {"thesis": "t", "central_problem": "", "prior_gap": "", "central_idea": "",
             "argument_chain": [], "experiments": [], "must_preserve_facts": [],
             "limitations": [], "source_facts": [], "material_unknowns": []},
            {"paper_archetype": ["method"], "domain": [], "reader_goal": "g", "prerequisites": [],
             "sections": [], "running_example": ""},
            *[{"markdown": md} for md in note_markdowns],
        ])

    def _paper(self):
        from research_pulse.production.reading import CanonicalPaperIR, PaperIRBlock

        return CanonicalPaperIR("p", "T", blocks=(PaperIRBlock("b1", "paragraph", "body", "some text", 1),))

    def test_repair_then_final_pass(self) -> None:
        from research_pulse.pedagogical.pipeline import PedagogicalService

        backend = self._backend(["# v1", "# v2"])
        service = PedagogicalService(
            backend,
            evidence_gate=_FakeGate([EvidenceGateResult(passed=True), EvidenceGateResult(passed=True)]),
            blind_reader=_FakeBlind([BlindReaderResult(overall="needs_targeted_revision"), BlindReaderResult(overall="pass")]),
        )
        result = service.run(self._paper())
        self.assertTrue(result.repaired)
        self.assertEqual(result.blind.overall, "pass")
        self.assertIn("# v2", result.note.markdown)

    def test_repair_cannot_save_final_fail(self) -> None:
        from research_pulse.pedagogical.pipeline import PedagogicalService

        backend = self._backend(["# v1", "# v2"])
        service = PedagogicalService(
            backend,
            evidence_gate=_FakeGate([EvidenceGateResult(passed=True), EvidenceGateResult(passed=False)]),
            blind_reader=_FakeBlind([BlindReaderResult(overall="fail"), BlindReaderResult(overall="fail")]),
        )
        result = service.run(self._paper())
        self.assertTrue(result.repaired)
        self.assertFalse(result.evidence.passed)
        self.assertEqual(result.blind.overall, "fail")


class _FakeGate:
    def __init__(self, results: list[EvidenceGateResult]) -> None:
        self._results = list(results)

    def evaluate(self, note, paper_model):  # noqa: ANN001
        return self._results.pop(0)


class _FakeBlind:
    def __init__(self, results: list[BlindReaderResult]) -> None:
        self._results = list(results)

    def read(self, note):  # noqa: ANN001
        return self._results.pop(0)


if __name__ == "__main__":
    unittest.main()
