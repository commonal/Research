"""MemGuard q-method 垂直切片：只验证开发 harness 的可观察行为。"""

from __future__ import annotations

from dataclasses import replace
import json
import unittest

from research_pulse.production.method_slice import (
    ClaimUse,
    DeepSeekMethodSliceModel,
    PageAnchor,
    PaperMaterialSnapshot,
    ParserObservation,
    SourceSpan,
    run_memguard_method_slice,
)
from research_pulse.production.normalized import NormalizedBlock, SourceRef


class _SupportedMethodModel:
    def form_finding(self, *, question, candidates):  # noqa: ANN001
        return {
            "proposition": (
                "MemGuard 将多准则验证器信号保存为记忆的持久描述符，"
                "并用这些描述符持续治理准入、检索、冲突处理、摘要和归档。"
            ),
            "source_span_ids": [candidates[0]["span_id"]],
            "derivation": "direct",
        }

    def write_single_finding(self, *, task):  # noqa: ANN001
        return task["proposition"]

    def assess_grounding(self, *, claim, evidence):  # noqa: ANN001
        return {"verdict": "supported", "issues": []}


class _MustNotReadConflictingMaterial:
    def form_finding(self, **kwargs):  # noqa: ANN003
        raise AssertionError("conflicting material must stop before Finding generation")

    def write_single_finding(self, **kwargs):  # noqa: ANN003
        raise AssertionError("conflicting material must not reach Writer")

    def assess_grounding(self, **kwargs):  # noqa: ANN003
        raise AssertionError("conflicting material must not reach Grounding")


class _OverreachingWriterModel(_SupportedMethodModel):
    def write_single_finding(self, *, task):  # noqa: ANN001
        return task["proposition"] + " 因此它彻底消除了长期记忆污染。"

    def assess_grounding(self, *, claim, evidence):  # noqa: ANN001
        return {
            "verdict": "unsupported",
            "issues": ["原文没有支持‘彻底消除长期记忆污染’这一因果结论。"],
        }


class _InferredFindingModel(_MustNotReadConflictingMaterial):
    def form_finding(self, *, question, candidates):  # noqa: ANN001
        return {
            "proposition": "MemGuard 的目标是彻底杜绝所有长期记忆错误。",
            "source_span_ids": [candidates[0]["span_id"]],
            "derivation": "inferred",
        }


class _EmptyWriterModel(_SupportedMethodModel):
    def write_single_finding(self, *, task):  # noqa: ANN001
        return ""

    def assess_grounding(self, *, claim, evidence):  # noqa: ANN001
        raise AssertionError("empty Writer output must stop before semantic Grounding")


class _FormulaFromTextModel(_SupportedMethodModel):
    def form_finding(self, *, question, candidates):  # noqa: ANN001
        return {
            "proposition": "MemGuard 使用描述符 d_m = (R_m, c_m, l_m, v_m) 治理记忆。",
            "source_span_ids": [candidates[0]["span_id"]],
            "derivation": "direct",
        }

    def assess_grounding(self, *, claim, evidence):  # noqa: ANN001
        raise AssertionError("disallowed formula semantics must stop before semantic Grounding")


class _FormulaExposureProbe(_SupportedMethodModel):
    def __init__(self) -> None:
        self.saw_omission = False

    def form_finding(self, *, question, candidates):  # noqa: ANN001
        joined = "\n".join(candidate["text"] for candidate in candidates)
        self.saw_omission = "[FORMULA OMITTED" in joined and "d_m =" not in joined
        return {
            "proposition": "MemGuard 将验证器信号作为持久元数据，用于管理记忆的准入与后续生命周期。",
            "source_span_ids": [candidates[0]["span_id"]],
            "derivation": "direct",
        }


class _ScriptedJsonProvider:
    text_model = "scripted-json-model"

    def __init__(self) -> None:
        self.operations = []

    def call_json(self, operation, model, prompt, image=None):  # noqa: ANN001
        self.operations.append(operation)
        if operation == "method_slice_ground":
            return {"verdict": "supported", "issues": []}
        payload = json.loads(prompt)
        if operation == "method_slice_find":
            return {
                "proposition": "MemGuard 把验证器信号持久化，并用于记忆生命周期治理。",
                "source_span_ids": [payload["candidates"][0]["span_id"]],
                "derivation": "direct",
            }
        if operation == "method_slice_write":
            return {"markdown": payload["finding"]["proposition"]}
        raise AssertionError(operation)


def _method_block() -> NormalizedBlock:
    return NormalizedBlock(
        block_id="normalized:2608.21867:text:method",
        kind="text",
        text=(
            "MemGuard's key distinction is to treat verifier output not as a one-shot filter, "
            "but as persistent lifecycle metadata. It converts multi-criteria verification into "
            "descriptors reused during retrieval, conflict resolution, summarization, and archival."
        ),
        section_path=("Abstract",),
        page_start=1,
        page_end=1,
        bbox=(87.0, 775.0, 273.0, 324.0),
        sources=(
            SourceRef(parser="mineru", locator="mineru:/pages/0/items/8"),
            SourceRef(parser="docling", locator="docling:/texts/16"),
        ),
        alignment="aligned",
        parse_status="available",
        confidence=0.99,
    )


class MemGuardMethodSliceTests(unittest.TestCase):
    def test_supported_method_block_keeps_one_bounded_chain_back_to_pdf_region(self) -> None:
        snapshot = PaperMaterialSnapshot.from_normalized(
            source_id="2608.21867",
            source_url="https://arxiv.org/pdf/2608.21867",
            blocks=(_method_block(),),
        )

        result = run_memguard_method_slice(snapshot, _SupportedMethodModel())

        self.assertEqual(result.status, "SUPPORTED")
        self.assertIn("持久描述符", result.draft_block.markdown)
        self.assertEqual(result.finding.source_span_ids, result.grounding.inspected_span_ids)
        self.assertEqual(result.finding.source_span_ids, result.provenance.source_span_ids)
        self.assertEqual(result.search_trace.accepted_span_ids, result.finding.source_span_ids)
        self.assertEqual(result.search_trace.accepted_span_ids, result.grounding.inspected_span_ids)
        self.assertEqual(result.search_trace.candidate_span_ids, result.finding.source_span_ids)
        self.assertEqual(result.provenance.pdf_source, "https://arxiv.org/pdf/2608.21867")
        self.assertEqual(result.provenance.anchors[0].page_start, 1)
        self.assertEqual(result.provenance.anchors[0].bbox, (87.0, 775.0, 273.0, 324.0))

    def test_conflicting_method_observations_cannot_silently_choose_a_parser(self) -> None:
        span = SourceSpan(
            span_id="source:2608.21867:page1:method",
            kind="text",
            section_path=("Abstract",),
            anchor=PageAnchor(page_start=1, page_end=1, bbox=(87.0, 775.0, 273.0, 324.0)),
            observations=(
                ParserObservation(
                    parser="mineru",
                    text="MemGuard persists verifier descriptors across admission, retrieval, and archival.",
                    locators=("mineru:/pages/0/items/8",),
                ),
                ParserObservation(
                    parser="docling",
                    text="MemGuard applies a one-time relevance filter and stores no lifecycle metadata.",
                    locators=("docling:/texts/16",),
                ),
            ),
            parser_variants_available=True,
        )
        snapshot = PaperMaterialSnapshot(
            source_id="2608.21867",
            pdf_source="https://arxiv.org/pdf/2608.21867",
            source_spans=(span,),
        )

        result = run_memguard_method_slice(snapshot, _MustNotReadConflictingMaterial())

        self.assertEqual(result.status, "CONFLICTING")
        self.assertIsNone(result.finding)
        self.assertIsNone(result.draft_block)
        self.assertEqual(result.provenance.source_span_ids, (span.span_id,))
        self.assertIn("parser_observations_conflict", result.limitations)

    def test_missing_method_evidence_is_a_material_result_not_a_model_fill(self) -> None:
        unrelated = NormalizedBlock(
            block_id="normalized:2608.21867:text:authors",
            kind="text",
            text="Haoyu Wang, Guangyuan Dong, He Liang, and collaborators.",
            section_path=("Authors",),
            page_start=1,
            page_end=1,
            bbox=(120.0, 140.0, 475.0, 127.0),
            sources=(SourceRef(parser="docling", locator="docling:/texts/2"),),
            alignment="docling_only",
            parse_status="available",
            confidence=0.55,
        )
        snapshot = PaperMaterialSnapshot.from_normalized(
            source_id="2608.21867",
            source_url="https://arxiv.org/pdf/2608.21867",
            blocks=(unrelated,),
        )

        result = run_memguard_method_slice(snapshot, _MustNotReadConflictingMaterial())

        self.assertEqual(result.status, "MATERIAL_UNAVAILABLE")
        self.assertIsNone(result.finding)
        self.assertIsNone(result.draft_block)
        self.assertEqual(result.provenance.source_span_ids, ())
        self.assertIn("method_evidence_unavailable", result.limitations)

    def test_unparsed_method_text_is_not_a_readable_candidate(self) -> None:
        unparsed = replace(_method_block(), parse_status="unparsed")
        snapshot = PaperMaterialSnapshot.from_normalized(
            source_id="2608.21867",
            source_url="https://arxiv.org/pdf/2608.21867",
            blocks=(unparsed,),
        )

        result = run_memguard_method_slice(snapshot, _MustNotReadConflictingMaterial())

        self.assertEqual(result.status, "MATERIAL_UNAVAILABLE")
        self.assertIsNone(result.finding)

    def test_writer_overreach_rejects_only_the_current_block(self) -> None:
        snapshot = PaperMaterialSnapshot.from_normalized(
            source_id="2608.21867",
            source_url="https://arxiv.org/pdf/2608.21867",
            blocks=(_method_block(),),
        )

        result = run_memguard_method_slice(snapshot, _OverreachingWriterModel())

        self.assertEqual(result.status, "UNSUPPORTED")
        self.assertIsNone(result.draft_block)
        self.assertIn("彻底消除了", result.rejected_draft_block.markdown)
        self.assertIn("没有支持", result.grounding.issues[0])
        self.assertEqual(result.grounding.inspected_span_ids, result.finding.source_span_ids)

    def test_conflicting_table_numbers_do_not_allow_numeric_claims(self) -> None:
        table = SourceSpan(
            span_id="source:2608.21867:page8:table2",
            kind="table",
            section_path=("Experiments",),
            anchor=PageAnchor(page_start=8, page_end=8, bbox=(90.0, 700.0, 500.0, 300.0)),
            observations=(
                ParserObservation(
                    parser="mineru",
                    text="Method A | Accuracy 0.836",
                    locators=("mineru:/pages/7/items/4",),
                ),
                ParserObservation(
                    parser="docling",
                    text="Method A | Accuracy 0.386",
                    locators=("docling:/tables/2",),
                ),
            ),
            parser_variants_available=True,
        )
        snapshot = PaperMaterialSnapshot(
            source_id="2608.21867",
            pdf_source="https://arxiv.org/pdf/2608.21867",
            source_spans=(table,),
        )

        self.assertFalse(snapshot.allows(table.span_id, ClaimUse.NUMERIC_VALUE))

    def test_inferred_finding_cannot_enter_writer_in_v1(self) -> None:
        snapshot = PaperMaterialSnapshot.from_normalized(
            source_id="2608.21867",
            source_url="https://arxiv.org/pdf/2608.21867",
            blocks=(_method_block(),),
        )

        result = run_memguard_method_slice(snapshot, _InferredFindingModel())

        self.assertEqual(result.status, "PARTIAL")
        self.assertIsNotNone(result.finding)
        self.assertIsNone(result.draft_block)
        self.assertIn("inferred_finding_not_publishable", result.limitations)

    def test_deepseek_adapter_runs_through_the_same_non_publishing_seam(self) -> None:
        snapshot = PaperMaterialSnapshot.from_normalized(
            source_id="2608.21867",
            source_url="https://arxiv.org/pdf/2608.21867",
            blocks=(_method_block(),),
        )

        provider = _ScriptedJsonProvider()
        result = run_memguard_method_slice(
            snapshot,
            DeepSeekMethodSliceModel(provider),
        )

        self.assertEqual(result.status, "SUPPORTED")
        self.assertEqual(result.search_trace.accepted_span_ids, result.grounding.inspected_span_ids)
        self.assertNotIn("method_slice_write", provider.operations)

    def test_empty_writer_output_is_rejected_before_semantic_grounding(self) -> None:
        snapshot = PaperMaterialSnapshot.from_normalized(
            source_id="2608.21867",
            source_url="https://arxiv.org/pdf/2608.21867",
            blocks=(_method_block(),),
        )

        result = run_memguard_method_slice(snapshot, _EmptyWriterModel())

        self.assertEqual(result.status, "UNSUPPORTED")
        self.assertIsNone(result.draft_block)
        self.assertEqual(result.grounding.verdict, "unsupported")
        self.assertEqual(result.grounding.issues, ("empty_writer_output",))

    def test_text_only_source_cannot_smuggle_formula_semantics_into_draft(self) -> None:
        snapshot = PaperMaterialSnapshot.from_normalized(
            source_id="2608.21867",
            source_url="https://arxiv.org/pdf/2608.21867",
            blocks=(_method_block(),),
        )

        result = run_memguard_method_slice(snapshot, _FormulaFromTextModel())

        self.assertEqual(result.status, "UNSUPPORTED")
        self.assertIsNone(result.draft_block)
        self.assertIn("claim_use_not_allowed:FORMULA_SEMANTICS", result.grounding.issues)

    def test_method_transport_view_omits_formula_without_formula_capability(self) -> None:
        formula_block = replace(
            _method_block(),
            text=(
                "MemGuard stores descriptor d_m = (R_m, c_m, l_m, v_m) with each record, "
                "then uses persistent verifier metadata for admission and lifecycle governance."
            ),
        )
        snapshot = PaperMaterialSnapshot.from_normalized(
            source_id="2608.21867",
            source_url="https://arxiv.org/pdf/2608.21867",
            blocks=(formula_block,),
        )
        probe = _FormulaExposureProbe()

        result = run_memguard_method_slice(snapshot, probe)

        self.assertTrue(probe.saw_omission)
        self.assertEqual(result.status, "SUPPORTED")

    def test_method_candidate_context_is_bounded_before_model_reading(self) -> None:
        blocks = tuple(
            replace(
                _method_block(),
                block_id=f"normalized:2608.21867:text:method-{index}",
                text=(
                    "MemGuard verifier admission governance retrieval conflict archival persistent "
                    f"method detail {index}."
                ),
                page_start=index + 1,
                page_end=index + 1,
                bbox=(87.0, 775.0 - index, 273.0, 324.0 - index),
            )
            for index in range(12)
        )
        snapshot = PaperMaterialSnapshot.from_normalized(
            source_id="2608.21867",
            source_url="https://arxiv.org/pdf/2608.21867",
            blocks=blocks,
        )

        result = run_memguard_method_slice(snapshot, _SupportedMethodModel())

        self.assertLessEqual(len(result.search_trace.candidate_span_ids), 8)
        self.assertTrue(set(result.search_trace.accepted_span_ids) <= set(result.search_trace.candidate_span_ids))


if __name__ == "__main__":
    unittest.main()
