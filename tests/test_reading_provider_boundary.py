from __future__ import annotations

from io import BytesIO
import json
from unittest import TestCase
from unittest.mock import patch

from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import (
    CanonicalPaperIR,
    DeepSeekPaperReadingModel,
    FullPaperReadRequest,
    PaperIRBlock,
    PaperReader,
    ReadingIntent,
    ReadingTarget,
    VisualReadRequest,
)


class _ProviderResponse:
    def __init__(self, payload: dict) -> None:
        self._body = BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self) -> "_ProviderResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body.read()


def _request() -> FullPaperReadRequest:
    candidate = PaperCandidate("provider-paper", "Provider boundary", "https://example.com/p", "test")
    paper = CanonicalPaperIR(
        candidate.source_id,
        candidate.title,
        (PaperIRBlock("problem", "paragraph", "Introduction", "A concrete problem is established.", 1),),
    )
    return FullPaperReadRequest(candidate, paper, ("problem",))


class ReadingProviderBoundaryTests(TestCase):
    def test_section_repair_sends_only_compact_assignment_state(self) -> None:
        model = DeepSeekPaperReadingModel(text_model="text-model", vision_model="vision-model", api_key="test-key")
        captured: dict[str, object] = {}

        def fake_call(operation: str, model_name: str, prompt: str, image: str | None = None) -> dict:
            captured.update(operation=operation, model=model_name, payload=json.loads(prompt))
            return {"sections": []}

        model._json_call = fake_call  # type: ignore[method-assign]
        model.repair_section_plan({
            "required_obligation_ids": ("fact:f1", "asset:a1"),
            "obligations": (
                {"obligation_id": "fact:f1", "kind": "fact", "label": "A concise fact."},
                {"obligation_id": "asset:a1", "kind": "asset", "label": "Explain the architecture."},
            ),
            "coverage_conflict": {"missing": ("asset:a1",)},
            "sections": ({"section_id": "method", "heading": "Method"},),
            "paper_model": {"large": "must-not-be-resent"},
            "asset_plan": {"large": "must-not-be-resent"},
        })

        payload = captured["payload"]
        self.assertEqual(len(payload["obligations"]), 2)
        self.assertEqual(payload["sections"][0]["section_id"], "method")
        self.assertNotIn("paper_model", payload)
        self.assertNotIn("asset_plan", payload)

    def test_writer_repair_sends_compact_missing_obligation_evidence(self) -> None:
        model = DeepSeekPaperReadingModel(text_model="text-model", vision_model="vision-model", api_key="test-key")
        captured: dict[str, object] = {}

        def fake_call(operation: str, model_name: str, prompt: str, image: str | None = None) -> dict:
            captured.update(operation=operation, model=model_name, payload=json.loads(prompt))
            return {"markdown": "# repaired"}

        model._json_call = fake_call  # type: ignore[method-assign]
        model.repair_writer({
            "markdown": "# original",
            "missing_obligations": ("unknown:u1", "asset:figure-1"),
            "missing_obligation_evidence": (
                {"obligation_id": "unknown:u1", "kind": "material_unknown", "statement": "Exact label construction is not described."},
                {"obligation_id": "asset:figure-1", "kind": "inline_visual", "visual_interpretation": "A flows to B."},
            ),
            "affected_sections": ("method", "limitations"),
            "unsupported_writer_claims": (),
            "paper_model": {"large": "must-not-be-resent"},
            "coverage_ledger": {"large": "must-not-be-resent"},
            "asset_plan": {"large": "must-not-be-resent"},
            "visual_interpretations": ({"large": "must-not-be-resent"},),
            "definition_neighborhoods": ({"large": "must-not-be-resent"},),
        })

        payload = captured["payload"]
        self.assertEqual(payload["required_completion_count"], 2)
        self.assertEqual(len(payload["missing_obligation_evidence"]), 2)
        self.assertIn("exactly one section_patch per missing obligation", payload["instruction"])
        self.assertIn("exactly one obligation_id", payload["instruction"])
        self.assertNotIn("paper_model", payload)
        self.assertNotIn("coverage_ledger", payload)
        self.assertNotIn("asset_plan", payload)
        self.assertNotIn("visual_interpretations", payload)
        self.assertNotIn("definition_neighborhoods", payload)

    def test_writer_assembles_ordered_structured_sections_with_stable_internal_markers(self) -> None:
        model = DeepSeekPaperReadingModel(text_model="text-model", vision_model="vision-model", api_key="test-key")

        def fake_call(operation: str, model_name: str, prompt: str, image: str | None = None) -> dict:
            self.assertEqual(operation, "note_write")
            return {
                "title": "中文精读",
                "sections": [
                    {"section_id": "sec_method", "heading": "方法", "markdown": "方法正文。"},
                    {"section_id": "sec_results", "heading": "实验结果", "markdown": "结果正文。"},
                ],
            }

        model._json_call = fake_call  # type: ignore[method-assign]
        markdown = model.write_note({
            "section_contracts": [
                {"section_id": "sec_method", "heading": "Method"},
                {"section_id": "sec_results", "heading": "Results"},
            ],
        })

        self.assertLess(markdown.index("rp-section:sec_method"), markdown.index("rp-section:sec_results"))
        self.assertIn("## 方法\n\n方法正文。", markdown)
        self.assertIn("## 实验结果\n\n结果正文。", markdown)

    def test_final_note_writer_has_a_dedicated_long_form_output_budget(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(request: object, timeout: int = 0) -> _ProviderResponse:
            payload = json.loads(request.data.decode("utf-8"))
            captured.update(payload)
            content = json.dumps({
                "title": "中文精读",
                "sections": [{"section_id": "sec_main", "heading": "主线", "markdown": "完整正文。"}],
            })
            return _ProviderResponse({
                "model": "text-model",
                "choices": [{"finish_reason": "stop", "message": {"content": content}}],
                "usage": {"completion_tokens": 32},
            })

        with patch("research_pulse.production.reading.urlopen", side_effect=fake_urlopen):
            model = DeepSeekPaperReadingModel(text_model="text-model", api_key="test-key")
            model.write_note({"section_contracts": [{"section_id": "sec_main", "heading": "Main"}]})

        self.assertGreaterEqual(captured["max_tokens"], 8_192)

    def test_visual_adapter_requests_interpretation_and_accepts_named_visual_field(self) -> None:
        model = DeepSeekPaperReadingModel(text_model="text-model", vision_model="vision-model", api_key="test-key")
        captured: dict[str, str] = {}

        def fake_call(operation: str, model_name: str, prompt: str, image: str | None = None) -> dict:
            captured.update(operation=operation, model=model_name, prompt=prompt, image=image or "")
            return {"visual_interpretation": "The arrows connect the encoder to the patch decoder.", "_resolved_model": "vision-model"}

        model._json_call = fake_call  # type: ignore[method-assign]
        target = ReadingTarget("t", ("q",), (), "Explain the architecture flow.", "Explain component relationships.", "Keep unclear pixels unknown.")
        block = PaperIRBlock("figure", "figure", "Architecture", "Figure 1 architecture.", 1, caption="Architecture flow", image_path="safe.png", safe_image=True)

        interpretation = model.interpret_visual(VisualReadRequest(_request().candidate, target, block, block.caption or "", "Neighboring method text."))

        self.assertEqual(interpretation, "The arrows connect the encoder to the patch decoder.")
        prompt = json.loads(captured["prompt"])
        self.assertIn("interpretation", prompt["required_json_shape"])

    def test_missing_visual_interpretation_field_is_audited(self) -> None:
        model = DeepSeekPaperReadingModel(text_model="text-model", vision_model="vision-model", api_key="test-key")
        model._json_call = lambda *args, **kwargs: {"description": "", "_resolved_model": "vision-model"}  # type: ignore[method-assign]
        target = ReadingTarget("t", ("q",), (), "Explain the architecture flow.", "Explain component relationships.", "Keep unclear pixels unknown.")
        block = PaperIRBlock("figure", "figure", "Architecture", "Figure 1 architecture.", 1, caption="Architecture flow", image_path="safe.png", safe_image=True)

        interpretation = model.interpret_visual(VisualReadRequest(_request().candidate, target, block, block.caption or "", "Neighboring method text."))

        self.assertEqual(interpretation, "")
        self.assertEqual(model.fallbacks, ["visual_interpretation:missing_field"])
        self.assertEqual(model.provider_failures[0]["kind"], "missing_field")

    def test_truncated_full_paper_json_is_diagnosed_and_retried_once(self) -> None:
        responses = iter((
            {
                "model": "deepseek-v4-flash",
                "choices": [{"finish_reason": "length", "message": {"content": '{"thesis":"cut"'}}],
                "usage": {"completion_tokens": 12_000},
            },
            {
                "model": "deepseek-v4-flash",
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"thesis": "complete"})}}],
                "usage": {"completion_tokens": 12},
            },
        ))

        with patch("research_pulse.production.reading.urlopen", side_effect=lambda *_args, **_kwargs: _ProviderResponse(next(responses))):
            model = DeepSeekPaperReadingModel(text_model="deepseek-v4-flash", api_key="test-key")
            result = model.read_full_paper(_request())

        self.assertEqual(result["thesis"], "complete")
        self.assertEqual(model.fallbacks, ["full_paper_read:truncated_json_retry"])
        self.assertEqual(
            model.provider_failures,
            [{
                "operation": "full_paper_read",
                "kind": "truncated_json",
                "finish_reason": "length",
                "content_chars": 15,
                "completion_tokens": 12_000,
                "json_error_position": 15,
            }],
        )

    def test_failed_full_paper_retry_returns_auditable_receipt_without_target_fallback(self) -> None:
        responses = iter((
            {
                "model": "deepseek-v4-flash",
                "choices": [{"finish_reason": "length", "message": {"content": '{"thesis":"cut"'}}],
                "usage": {"completion_tokens": 12_000},
            },
            {
                "model": "deepseek-v4-flash",
                "choices": [{"finish_reason": "stop", "message": {"content": "not-json"}}],
                "usage": {"completion_tokens": 3},
            },
        ))
        request = _request()

        with patch("research_pulse.production.reading.urlopen", side_effect=lambda *_args, **_kwargs: _ProviderResponse(next(responses))):
            model = DeepSeekPaperReadingModel(text_model="deepseek-v4-flash", api_key="test-key")
            result = PaperReader(model).read(request.candidate, request.paper_ir, ReadingIntent(max_targets=6))

        self.assertEqual(
            (result.receipt.status, result.receipt.target_count, result.receipt.text_calls),
            ("failed", 0, 2),
        )
        self.assertEqual(
            tuple(item["kind"] for item in result.receipt.provider_failures),
            ("truncated_json", "invalid_json"),
        )
        self.assertEqual(
            result.receipt.fallbacks,
            ("full_paper_read:truncated_json_retry", "full_paper_read:invalid_json"),
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
