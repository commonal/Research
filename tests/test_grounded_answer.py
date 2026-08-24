from __future__ import annotations

from hashlib import sha256
from unittest import TestCase

from research_pulse.knowledge.models import DurableEvidenceAnchor
from research_pulse.rag.answer import DeepSeekGroundedAnswerGenerator
from research_pulse.rag.contracts import EvidenceHit


class GroundedAnswerTests(TestCase):
    def test_generator_sends_only_evidence_hits_to_the_model(self) -> None:
        captured = {}

        def post_json(url, payload, headers):
            captured["payload"] = payload
            return {"choices": [{"message": {"content": "结论。[kp:test@v1 | anchor:1]"}}]}

        excerpt = "The evidence text."
        source_anchor = DurableEvidenceAnchor(
            anchor_id="source:1",
            source_url="https://example.com",
            evidence_excerpt=excerpt,
            excerpt_sha256=sha256(excerpt.encode("utf-8")).hexdigest(),
            section="Results",
        )
        hit = EvidenceHit(
            chunk_id="chunk:1",
            knowledge_id="kp:test",
            knowledge_version="v1",
            title="Test",
            text="The evidence text.",
            source_url="https://example.com",
            anchor_id="anchor:1",
            dense_score=None,
            keyword_score=0.1,
            fused_score=0.1,
            claim_id="claim:1",
            claim_type="source_fact",
            source_anchors=(source_anchor,),
        )
        answer = DeepSeekGroundedAnswerGenerator("test-key", post_json=post_json).generate(
            query="What is known?", evidence=[hit]
        )

        self.assertEqual(captured["payload"]["thinking"], {"type": "enabled"})
        self.assertEqual(captured["payload"]["max_tokens"], 1_200)
        evidence_prompt = captured["payload"]["messages"][1]["content"]
        self.assertIn("The evidence text.", evidence_prompt)
        self.assertNotIn("Test\", \"text", evidence_prompt)
        self.assertIn("claim:1", evidence_prompt)
        self.assertIn("source:1", evidence_prompt)
        self.assertIn("anchor:1", answer)

    def test_agent_inference_is_explicitly_labeled_when_model_omits_label(self) -> None:
        hit = EvidenceHit(
            chunk_id="chunk:2",
            knowledge_id="kp:test",
            knowledge_version="v1",
            title="Test",
            text="This may generalize.",
            source_url="https://example.com",
            anchor_id="anchor:2",
            dense_score=None,
            keyword_score=0.1,
            fused_score=0.1,
            claim_id="claim:2",
            claim_type="agent_inference",
        )

        answer = DeepSeekGroundedAnswerGenerator(
            "test-key",
            post_json=lambda *_: {"choices": [{"message": {"content": "可能可以推广。"}}]},
        ).generate(query="Can it generalize?", evidence=[hit])

        self.assertTrue(answer.startswith("以下回答包含系统推断"))

    def test_reading_question_is_removed_from_model_context(self) -> None:
        captured = {}
        question = EvidenceHit(
            chunk_id="chunk:q",
            knowledge_id="kp:test",
            knowledge_version="v1",
            title="Test",
            text="Could this fail on another dataset?",
            source_url="https://example.com",
            anchor_id="anchor:q",
            dense_score=None,
            keyword_score=0.1,
            fused_score=0.1,
            claim_id="claim:q",
            claim_type="reading_question",
        )

        def post_json(url, payload, headers):
            captured["prompt"] = payload["messages"][1]["content"]
            return {"choices": [{"message": {"content": "证据不足。"}}]}

        DeepSeekGroundedAnswerGenerator("test-key", post_json=post_json).generate(
            query="What is known?", evidence=[question]
        )

        self.assertNotIn(question.text, captured["prompt"])
