from __future__ import annotations

from unittest import TestCase

from research_pulse.production.reading import CoverageConflict, CoverageLedger


def _paper_model() -> dict:
    return {
        "argument_chain": [{"node_id": "argument:problem"}],
        "experiments": [{"experiment_id": "experiment:ablation"}],
        "must_preserve_facts": [
            {"fact_id": "fact:result", "eligible": True},
            {"fact_id": "fact:optional", "eligible": False},
        ],
        "limitations": [{"limitation_id": "limitation:scope"}],
        "sections": [
            {
                "section_id": "results",
                "coverage_obligation_ids": [
                    "argument:problem",
                    "experiment:ablation",
                    "fact:result",
                    "limitation:scope",
                    "asset:method-figure",
                ],
            }
        ],
    }


def _asset_plan() -> dict:
    return {
        "assets": [
            {"asset_id": "asset:method-figure", "decision": "inline"},
            {"asset_id": "asset:reference-table", "decision": "reference"},
        ]
    }


class CoverageLedgerInterfaceTests(TestCase):
    def test_build_assigns_each_eligible_obligation_exactly_once(self) -> None:
        ledger = CoverageLedger.build(_paper_model(), _asset_plan())

        self.assertIsInstance(ledger, CoverageLedger)
        self.assertEqual(
            ledger.assigned_obligation_ids,
            (
                "argument:problem",
                "experiment:ablation",
                "fact:result",
                "limitation:scope",
                "asset:method-figure",
            ),
        )
        self.assertNotIn("fact:optional", ledger.assigned_obligation_ids)

    def test_build_rejects_missing_obligation(self) -> None:
        paper_model = _paper_model()
        paper_model["sections"][0]["coverage_obligation_ids"].remove("experiment:ablation")

        conflict = CoverageLedger.build(paper_model, _asset_plan())

        self.assertIsInstance(conflict, CoverageConflict)
        self.assertEqual(conflict.missing, ("experiment:ablation",))

    def test_build_rejects_duplicate_obligation(self) -> None:
        paper_model = _paper_model()
        paper_model["sections"][0]["coverage_obligation_ids"].append("fact:result")

        conflict = CoverageLedger.build(paper_model, _asset_plan())

        self.assertIsInstance(conflict, CoverageConflict)
        self.assertEqual(conflict.duplicates, ("fact:result",))

    def test_build_rejects_invented_obligation(self) -> None:
        paper_model = _paper_model()
        paper_model["sections"][0]["coverage_obligation_ids"].append("experiment:invented")

        conflict = CoverageLedger.build(paper_model, _asset_plan())

        self.assertIsInstance(conflict, CoverageConflict)
        self.assertEqual(conflict.invented, ("experiment:invented",))

