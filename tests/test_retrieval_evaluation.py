from __future__ import annotations

from pathlib import Path
from unittest import TestCase

from research_pulse.evaluation.retrieval import evaluate_retrieval, load_golden_questions
from research_pulse.rag.contracts import EvidenceHit
from research_pulse.rag.sufficiency import assess_sufficiency


ROOT = Path(__file__).resolve().parents[1]


def _hit(*, knowledge_id: str = "kp:arxiv:2606.10677", version: str = "2026-08-22") -> EvidenceHit:
    return EvidenceHit(
        chunk_id="chunk:test",
        knowledge_id=knowledge_id,
        knowledge_version=version,
        title="Test asset",
        text="Long-term LLM agent memory.",
        source_url="https://arxiv.org/abs/2606.10677v1",
        anchor_id="anchor:test",
        dense_score=None,
        keyword_score=0.1,
        fused_score=0.1,
    )


class RetrievalEvaluationTests(TestCase):
    def test_golden_questions_measure_recall_and_unanswerable_noise(self) -> None:
        answerable, unanswerable = load_golden_questions(ROOT / "evals" / "golden_questions.jsonl")

        metrics = evaluate_retrieval([(answerable, [_hit()]), (unanswerable, [])], k=5)

        self.assertEqual(metrics.recall_at_k, 1.0)
        self.assertEqual(metrics.mean_reciprocal_rank, 1.0)
        self.assertEqual(metrics.unexpected_hit_rate_for_unanswerable, 0.0)

    def test_sufficiency_requires_multiple_anchored_hits(self) -> None:
        decision = assess_sufficiency([_hit()])

        self.assertFalse(decision.sufficient)
        self.assertIn("至少需要 2 条", decision.reason)

    def test_sufficiency_allows_two_anchored_hits(self) -> None:
        decision = assess_sufficiency([_hit(), _hit(knowledge_id="kp:arxiv:another")])

        self.assertTrue(decision.sufficient)
        self.assertEqual(decision.distinct_asset_count, 2)
