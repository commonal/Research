"""Provider-aware prompt budgeting without hidden summarization calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


class TokenCodec(Protocol):
    def count(self, text: str) -> int: ...
    def truncate(self, text: str, max_tokens: int) -> str: ...


class PromptBudgetError(ValueError):
    pass


@dataclass(frozen=True)
class BudgetedBlock:
    block_id: str
    text: str


@dataclass(frozen=True)
class AllocatedBlock:
    block_id: str
    text: str
    truncated: bool = False


@dataclass(frozen=True)
class BudgetedMessage:
    message_id: str
    role: str
    text: str


@dataclass(frozen=True)
class PromptAllocation:
    context_blocks: tuple[AllocatedBlock, ...]
    visible_history: tuple[BudgetedMessage, ...]
    model_history: tuple[BudgetedMessage, ...]
    omitted_message_ids: tuple[str, ...]
    omitted_block_ids: tuple[str, ...]
    history_truncated: bool
    paper_context_truncated: bool
    used_input_tokens: int
    input_token_limit: int


class PromptBudgeter:
    def __init__(
        self,
        codec: TokenCodec,
        *,
        context_window_tokens: int,
        reserved_output_tokens: int,
    ) -> None:
        if context_window_tokens < 1:
            raise ValueError("context_window_tokens must be positive")
        if reserved_output_tokens < 0 or reserved_output_tokens >= context_window_tokens:
            raise ValueError("reserved_output_tokens must leave a positive input budget")
        self.codec = codec
        self.context_window_tokens = context_window_tokens
        self.reserved_output_tokens = reserved_output_tokens

    def allocate(
        self,
        *,
        system_text: str,
        question: str,
        context_blocks: Sequence[BudgetedBlock],
        history: Sequence[BudgetedMessage],
    ) -> PromptAllocation:
        input_limit = self.context_window_tokens - self.reserved_output_tokens
        mandatory_tokens = self.codec.count(system_text) + self.codec.count(question)
        if mandatory_tokens > input_limit:
            raise PromptBudgetError("system instructions and current question exceed input budget")
        remaining = input_limit - mandatory_tokens

        allocated_blocks: list[AllocatedBlock] = []
        omitted_block_ids: list[str] = []
        paper_truncated = False
        for index, block in enumerate(context_blocks):
            block_tokens = self.codec.count(block.text)
            if block_tokens <= remaining:
                allocated_blocks.append(AllocatedBlock(block.block_id, block.text))
                remaining -= block_tokens
                continue
            paper_truncated = True
            if remaining > 0:
                bounded_text = self.codec.truncate(block.text, remaining).strip()
                if bounded_text:
                    used = self.codec.count(bounded_text)
                    allocated_blocks.append(
                        AllocatedBlock(block.block_id, bounded_text, truncated=True)
                    )
                    remaining = max(0, remaining - used)
            omitted_block_ids.extend(item.block_id for item in context_blocks[index + 1 :])
            if not allocated_blocks or allocated_blocks[-1].block_id != block.block_id:
                omitted_block_ids.insert(0, block.block_id)
            break

        model_history_reversed: list[BudgetedMessage] = []
        first_omitted_index = -1
        for reverse_index, message in enumerate(reversed(history)):
            message_tokens = self.codec.count(message.text)
            if message_tokens > remaining:
                first_omitted_index = len(history) - reverse_index - 1
                break
            model_history_reversed.append(message)
            remaining -= message_tokens
        model_history = tuple(reversed(model_history_reversed))
        if first_omitted_index >= 0:
            omitted_messages = tuple(history[: first_omitted_index + 1])
        else:
            omitted_messages = ()

        used = input_limit - remaining
        return PromptAllocation(
            context_blocks=tuple(allocated_blocks),
            visible_history=tuple(history),
            model_history=model_history,
            omitted_message_ids=tuple(message.message_id for message in omitted_messages),
            omitted_block_ids=tuple(omitted_block_ids),
            history_truncated=bool(omitted_messages),
            paper_context_truncated=paper_truncated,
            used_input_tokens=used,
            input_token_limit=input_limit,
        )
