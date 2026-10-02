import unittest
from pathlib import Path
from unittest import mock

from research_pulse.pedagogical.pipeline import PedagogicalService
from research_pulse.pedagogical.source_quality import SourceQualityGate, SourceQualityRejected
from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import CanonicalPaperIR, DeepSeekPaperReadingModel, PaperIRBlock
from research_pulse.reader_production import ReaderConfig, ReaderProductionService


class SourceQualityGateTests(unittest.TestCase):
    def test_rejects_ir_containing_only_abstract_and_references(self) -> None:
        paper = CanonicalPaperIR(
            source_id="paper-1",
            title="Incomplete parse",
            abstract="We introduce a useful method.",
            blocks=(
                PaperIRBlock("abstract", "paragraph", "1 Abstract", "We introduce a useful method.", 0),
                PaperIRBlock("ref-1", "paragraph", "9 References", "[1] Prior work and publication details.", 1),
            ),
        )

        report = SourceQualityGate().evaluate(paper)

        self.assertEqual("rejected", report.status)
        self.assertFalse(report.passed)
        self.assertIn("missing_substantive_body", {issue.code for issue in report.issues})
        self.assertEqual((), report.usable_block_ids)

    def test_marks_repeated_and_hyphenated_body_as_degraded_but_usable(self) -> None:
        repeated = "The method improves resolu- tion while preserving the repre- sentation quality."
        paper = CanonicalPaperIR(
            source_id="paper-2",
            title="Noisy parse",
            blocks=(
                PaperIRBlock("intro-1", "paragraph", "Introduction", repeated, 0),
                PaperIRBlock("intro-2", "paragraph", "Introduction", repeated, 1),
                PaperIRBlock("method-1", "paragraph", "Method", repeated, 2),
            ),
        )

        report = SourceQualityGate().evaluate(paper)

        self.assertEqual("degraded", report.status)
        self.assertTrue(report.passed)
        self.assertEqual(
            {"duplicate_body_blocks", "pdf_hyphenation"},
            {issue.code for issue in report.issues},
        )
        self.assertEqual(("intro-1", "intro-2", "method-1"), report.usable_block_ids)

    def test_pipeline_rejects_bad_source_before_any_model_call(self) -> None:
        paper = CanonicalPaperIR(
            source_id="paper-3",
            title="Abstract only",
            blocks=(PaperIRBlock("abstract", "paragraph", "Abstract", "Only an abstract was parsed.", 0),),
        )

        with self.assertRaises(SourceQualityRejected) as caught:
            PedagogicalService(model=object()).run(paper)

        self.assertEqual("missing_substantive_body", caught.exception.report.issues[0].code)

    def test_marks_partially_unavailable_body_as_degraded(self) -> None:
        paper = CanonicalPaperIR(
            source_id="paper-5",
            title="Partial parse",
            blocks=(
                PaperIRBlock("intro", "paragraph", "Introduction", "The problem and motivation are available.", 0),
                PaperIRBlock("method", "paragraph", "Method", "Method parsing failed.", 1, parse_status="degraded"),
                PaperIRBlock("results", "paragraph", "Results", "Results parsing failed.", 2, parse_status="unavailable"),
            ),
        )

        report = SourceQualityGate().evaluate(paper)

        self.assertEqual("degraded", report.status)
        self.assertEqual(("intro",), report.usable_block_ids)
        issue = next(issue for issue in report.issues if issue.code == "unavailable_body_blocks")
        self.assertEqual(("method", "results"), issue.block_ids)

    def test_marks_blocks_with_dense_replacement_characters_as_degraded(self) -> None:
        paper = CanonicalPaperIR(
            source_id="paper-6",
            title="Garbled parse",
            blocks=(
                PaperIRBlock("intro", "paragraph", "Introduction", "This paragraph is readable and explains the problem.", 0),
                PaperIRBlock("method", "paragraph", "Method", "The method output is ���������� and cannot be decoded.", 1),
            ),
        )

        report = SourceQualityGate().evaluate(paper)

        self.assertEqual("degraded", report.status)
        issue = next(issue for issue in report.issues if issue.code == "garbled_body_text")
        self.assertEqual(("method",), issue.block_ids)

    def test_marks_explicitly_unavailable_body_tail_as_truncated(self) -> None:
        paper = CanonicalPaperIR(
            source_id="paper-7",
            title="Truncated parse",
            blocks=(
                PaperIRBlock("intro", "paragraph", "Introduction", "The problem is fully described.", 0),
                PaperIRBlock("method", "paragraph", "Method", "The method is fully described.", 1),
                PaperIRBlock("conclusion", "paragraph", "Conclusion", "The parser stopped here", 2, parse_status="unavailable"),
            ),
        )

        report = SourceQualityGate().evaluate(paper)

        self.assertEqual("degraded", report.status)
        issue = next(issue for issue in report.issues if issue.code == "truncated_body_tail")
        self.assertEqual(("conclusion",), issue.block_ids)

    def test_marks_short_text_repeated_across_pages_as_boilerplate(self) -> None:
        blocks = [
            PaperIRBlock(f"body-{index}", "paragraph", "Method", f"Unique substantive paragraph number {index} explains the method in detail.", index)
            for index in range(10)
        ]
        blocks.extend(
            PaperIRBlock(f"header-{index}", "paragraph", "body", "Research Conference 2026", 10 + index)
            for index in range(3)
        )
        paper = CanonicalPaperIR("paper-8", "Repeated header", tuple(blocks))

        report = SourceQualityGate().evaluate(paper)

        self.assertEqual("degraded", report.status)
        issue = next(issue for issue in report.issues if issue.code == "repeated_boilerplate")
        self.assertEqual(("header-0", "header-1", "header-2"), issue.block_ids)

    def test_marks_non_monotonic_source_block_order_as_degraded(self) -> None:
        paper = CanonicalPaperIR(
            "paper-9",
            "Out of order",
            (
                PaperIRBlock("intro", "paragraph", "Introduction", "First paragraph.", 0),
                PaperIRBlock("results", "paragraph", "Results", "Third paragraph arrived early.", 2),
                PaperIRBlock("method", "paragraph", "Method", "Second paragraph arrived late.", 1),
            ),
        )

        report = SourceQualityGate().evaluate(paper)

        self.assertEqual("degraded", report.status)
        issue = next(issue for issue in report.issues if issue.code == "non_monotonic_block_order")
        self.assertEqual(("results", "method"), issue.block_ids)

    def test_marks_long_body_without_section_structure_as_degraded(self) -> None:
        blocks = tuple(
            PaperIRBlock(
                f"body-{index}",
                "paragraph",
                "body",
                (f"Substantive paragraph {index}. " + "Detailed technical explanation. " * 24),
                index,
            )
            for index in range(7)
        )
        paper = CanonicalPaperIR("paper-10", "Flattened structure", blocks)

        report = SourceQualityGate().evaluate(paper)

        self.assertEqual("degraded", report.status)
        self.assertIn("missing_section_structure", {issue.code for issue in report.issues})

    def test_production_marks_rejected_source_for_review_without_generic_fallback(self) -> None:
        paper = CanonicalPaperIR(
            source_id="paper-4",
            title="Abstract only",
            blocks=(PaperIRBlock("abstract", "paragraph", "Abstract", "Only an abstract was parsed.", 0),),
        )
        pipeline = PedagogicalService(model=object())
        with mock.patch.object(DeepSeekPaperReadingModel, "from_environment", return_value=object()):
            service = ReaderProductionService(
                ReaderConfig(normalized_root=Path("tmp"), mode="pedagogical"),
                Path("tmp/source-quality-unused-vault"),
                pedagogical=pipeline,
            )
        service.reader.resolver.resolve = lambda candidate: paper  # type: ignore[method-assign]
        service.reader.read = lambda candidate: self.fail("源材料拒绝后不得回退 generic")  # type: ignore[method-assign]

        result = service.process(PaperCandidate("paper-4", "Abstract only", "https://example.test", "ai"))

        self.assertEqual("needs_review", result["status"])
        self.assertEqual("source_quality:missing_substantive_body", result["stop_reason"])
        self.assertIsNone(result["published_path"])


if __name__ == "__main__":
    unittest.main()
