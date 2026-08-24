from __future__ import annotations

from unittest import TestCase

from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import (
    ArgumentMap,
    CanonicalPaperIR,
    EvidenceBundle,
    PaperReader,
    PaperSkeleton,
    PaperIRBlock,
    ReadingIntent,
    ReadingQuestion,
    ReadingRecord,
    ReadingReceipt,
    ReadingResult,
    ReadingTarget,
    ReadingTrace,
)


class _DeterministicReadingModel:
    text_model = "text-test-model"
    vision_model = "vision-test-model"


class PaperReaderContractTests(TestCase):
    def test_read_returns_one_result_with_typed_reading_trace(self) -> None:
        candidate = PaperCandidate(
            source_id="paper-1",
            title="Task-conditioned least privilege",
            source_url="https://example.com/paper-1",
            domain="agents",
        )
        paper_ir = CanonicalPaperIR(
            source_id="paper-1",
            title=candidate.title,
            blocks=(
                PaperIRBlock(
                    block_id="abstract-1",
                    kind="paragraph",
                    section="abstract",
                    text="A tool agent can finish a task while exercising unnecessary authority.",
                ),
            ),
        )

        result = PaperReader(_DeterministicReadingModel()).read(
            candidate,
            paper_ir,
            ReadingIntent(language="zh-CN", depth="deep"),
        )

        self.assertIsInstance(result, ReadingResult)
        self.assertIsNotNone(result.draft)
        self.assertIsInstance(result.receipt, ReadingReceipt)
        self.assertIsInstance(result.trace, ReadingTrace)

    def test_reading_state_contracts_are_typed_and_distinct(self) -> None:
        skeleton = PaperSkeleton(
            paper_type="method",
            section_roles={"introduction": "problem"},
            candidate_contributions=("task-conditioned control",),
            candidate_experiments=("held-out tasks",),
        )
        argument_map = ArgumentMap(version=0, nodes=())
        question = ReadingQuestion(
            question_id="q-problem",
            text="Why is the existing permission strategy insufficient?",
            priority="high",
        )
        target = ReadingTarget(
            target_id="t-mechanism",
            question_ids=(question.question_id,),
            argument_node_ids=(),
            task="Read the mechanism and test why it addresses the gap.",
            success_criteria="The mechanism and its evidence are linked.",
            failure_criteria="The evidence remains unresolved.",
        )
        bundle = EvidenceBundle(
            target_id=target.target_id,
            version=1,
            source_block_ids=("abstract-1",),
            selection_reasons={"abstract-1": "defines the problem"},
        )
        record = ReadingRecord(
            target_id=target.target_id,
            question_ids=target.question_ids,
            bundle_version=bundle.version,
            version=1,
            source_block_ids=bundle.source_block_ids,
        )

        self.assertIsInstance(skeleton, PaperSkeleton)
        self.assertIsInstance(argument_map, ArgumentMap)
        self.assertIsInstance(question, ReadingQuestion)
        self.assertIsInstance(target, ReadingTarget)
        self.assertIsInstance(bundle, EvidenceBundle)
        self.assertIsInstance(record, ReadingRecord)
        self.assertNotIsInstance(target, question.__class__)
        self.assertNotEqual(target.target_id, question.question_id)
