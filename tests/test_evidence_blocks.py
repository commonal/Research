from __future__ import annotations

from unittest import TestCase

from research_pulse.production.evidence import (
    EvidenceBlock,
    EvidenceCandidate,
    RejectingSupplementalEvidenceResolver,
    classify_candidate,
    classify_with_optional_supplement,
)


def _candidate(**changes) -> EvidenceCandidate:
    values = {
        "block_id": "block:1",
        "kind": "text",
        "text": "The method reduces excess authority.",
        "source_url": "https://arxiv.org/abs/2608.18351v1",
        "parser": "fixture",
        "parse_status": "available",
        "locator_completeness": "exact",
        "section_path": ("Introduction",),
        "page_start": 1,
    }
    values.update(changes)
    return EvidenceCandidate(**values)


class EvidenceBlockContractTests(TestCase):
    def test_framework_free_candidate_represents_supported_content_types(self) -> None:
        kinds = ("text", "formula", "table", "figure", "caption")

        candidates = [_candidate(block_id=f"block:{kind}", kind=kind) for kind in kinds]

        self.assertEqual([candidate.kind for candidate in candidates], list(kinds))
        self.assertTrue(all(candidate.parser == "fixture" for candidate in candidates))

    def test_missing_locator_is_explicitly_degraded(self) -> None:
        candidate = _candidate(locator_completeness="missing", page_start=None)
        block = EvidenceBlock(candidate, eligible_for_fact=False, rejection_reason="locator_missing")

        self.assertEqual(block.candidate.locator_completeness, "missing")
        self.assertEqual(block.rejection_reason, "locator_missing")

    def test_only_available_candidate_can_be_a_fact(self) -> None:
        degraded = _candidate(kind="formula", parse_status="degraded")

        with self.assertRaisesRegex(ValueError, "Only available"):
            EvidenceBlock(degraded, eligible_for_fact=True)

    def test_default_fallback_is_scoped_and_does_not_invent_evidence(self) -> None:
        resolver = RejectingSupplementalEvidenceResolver()
        degraded_table = _candidate(kind="table", parse_status="degraded")

        self.assertIsNone(resolver.resolve(degraded_table))
        with self.assertRaisesRegex(ValueError, "non-text"):
            resolver.resolve(_candidate(kind="text", parse_status="degraded"))
        with self.assertRaisesRegex(ValueError, "requires a degraded"):
            resolver.resolve(_candidate(kind="table"))

    def test_classification_rejects_noise_and_unusable_multimodal_blocks(self) -> None:
        cases = (
            (_candidate(text="Ada Researcher, Department of Computing, Example University"), "bibliographic_noise"),
            (_candidate(text="Smith et al. A cited paper.", section_path=("References",)), "bibliographic_noise"),
            (_candidate(kind="formula", text="<!-- formula-not-decoded -->", parse_status="unparsed"), "parse_unparsed"),
            (_candidate(kind="table", text="| Evaluation | Parent vs continuation |"), "table_result_incomplete"),
            (_candidate(kind="figure", text="A diagram"), "figure_caption_missing"),
        )

        results = [(classify_candidate(candidate).eligible_for_fact, classify_candidate(candidate).rejection_reason) for candidate, _ in cases]

        self.assertEqual(results, [(False, reason) for _, reason in cases])

    def test_classification_accepts_self_contained_table_result(self) -> None:
        candidate = _candidate(
            kind="table",
            text="| Model | Accuracy |\n| Parent | 67.75% |\n| Continuation | 90.50% |",
        )

        block = classify_candidate(candidate)
        self.assertTrue(block.eligible_for_fact)
        self.assertEqual(block.supported_facets, ("experiment",))

    def test_table_needs_metric_comparison_objects_and_multiple_values(self) -> None:
        candidate = _candidate(
            kind="table",
            text="| Metric | Model | Value |\n| Accuracy | Proposed | 90.50% |",
        )

        block = classify_candidate(candidate)

        self.assertFalse(block.eligible_for_fact)
        self.assertEqual(block.rejection_reason, "table_result_incomplete")

    def test_optional_supplement_is_disabled_by_default_and_can_repair_one_block(self) -> None:
        candidate = _candidate(kind="table", text="| Result | only heading |")

        self.assertEqual(
            classify_with_optional_supplement(candidate, resolver=None).rejection_reason,
            "table_result_incomplete",
        )

        class CompleteThisBlockOnly:
            def resolve(self, degraded: EvidenceCandidate) -> EvidenceCandidate:
                self.seen = degraded
                return EvidenceCandidate(
                    **{
                        **degraded.__dict__,
                        "text": "| Model | Accuracy |\n| Parent | 67.75% |\n| Continuation | 90.50% |",
                        "parse_status": "available",
                    }
                )

        resolver = CompleteThisBlockOnly()
        repaired = classify_with_optional_supplement(candidate, resolver=resolver)

        self.assertEqual(resolver.seen.parse_status, "degraded")
        self.assertEqual(resolver.seen.block_id, candidate.block_id)
        self.assertTrue(repaired.eligible_for_fact)

    def test_optional_supplement_failure_or_cross_paper_result_stays_ineligible(self) -> None:
        candidate = _candidate(kind="formula", text="formula-not-decoded", parse_status="unparsed")

        class FailingResolver:
            def resolve(self, degraded: EvidenceCandidate) -> EvidenceCandidate:
                raise RuntimeError("adapter unavailable")

        class CrossPaperResolver:
            def resolve(self, degraded: EvidenceCandidate) -> EvidenceCandidate:
                return EvidenceCandidate(
                    **{
                        **degraded.__dict__,
                        "source_url": "https://arxiv.org/abs/9999.99999",
                        "parse_status": "available",
                    }
                )

        self.assertEqual(
            classify_with_optional_supplement(candidate, resolver=FailingResolver()).rejection_reason,
            "supplementation_failed",
        )
        self.assertEqual(
            classify_with_optional_supplement(candidate, resolver=CrossPaperResolver()).rejection_reason,
            "supplementation_invalid_result",
        )
