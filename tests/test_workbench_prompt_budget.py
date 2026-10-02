from __future__ import annotations

from unittest import TestCase

from research_pulse.workbench.prompt_budget import (
    BudgetedBlock,
    BudgetedMessage,
    PromptBudgetError,
    PromptBudgeter,
)


class _WordCodec:
    def __init__(self) -> None:
        self.count_calls: list[str] = []

    def count(self, text: str) -> int:
        self.count_calls.append(text)
        return len(text.split())

    def truncate(self, text: str, max_tokens: int) -> str:
        return " ".join(text.split()[:max_tokens])


class WorkbenchPromptBudgetTests(TestCase):
    def test_keeps_visible_history_but_sends_only_recent_messages(self) -> None:
        codec = _WordCodec()
        history = (
            BudgetedMessage("m1", "user", "old one two"),
            BudgetedMessage("m2", "assistant", "middle one two"),
            BudgetedMessage("m3", "user", "recent one"),
        )
        allocation = PromptBudgeter(
            codec,
            context_window_tokens=12,
            reserved_output_tokens=3,
        ).allocate(
            system_text="system rule",
            question="current question",
            context_blocks=(BudgetedBlock("B-full", "paper evidence"),),
            history=history,
        )

        self.assertEqual(allocation.visible_history, history)
        self.assertEqual(
            tuple(message.message_id for message in allocation.model_history),
            ("m3",),
        )
        self.assertEqual(allocation.omitted_message_ids, ("m1", "m2"))
        self.assertTrue(allocation.history_truncated)
        self.assertGreater(len(codec.count_calls), 0)

    def test_paper_context_has_priority_and_records_partial_block_truncation(self) -> None:
        allocation = PromptBudgeter(
            _WordCodec(),
            context_window_tokens=10,
            reserved_output_tokens=2,
        ).allocate(
            system_text="system rule",
            question="current question",
            context_blocks=(
                BudgetedBlock("B1", "one two three four five six"),
                BudgetedBlock("B2", "later evidence"),
            ),
            history=(BudgetedMessage("m1", "user", "old chat"),),
        )

        self.assertEqual(len(allocation.context_blocks), 1)
        self.assertEqual(allocation.context_blocks[0].block_id, "B1")
        self.assertEqual(allocation.context_blocks[0].text, "one two three four")
        self.assertTrue(allocation.paper_context_truncated)
        self.assertEqual(allocation.omitted_block_ids, ("B2",))
        self.assertEqual(allocation.model_history, ())

    def test_mandatory_prompt_over_budget_fails_instead_of_silent_overflow(self) -> None:
        with self.assertRaises(PromptBudgetError):
            PromptBudgeter(
                _WordCodec(), context_window_tokens=4, reserved_output_tokens=1
            ).allocate(
                system_text="system has two",
                question="question has two",
                context_blocks=(),
                history=(),
            )
