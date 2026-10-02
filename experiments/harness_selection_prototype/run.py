"""PROTOTYPE: compare a fixed paper-reading workflow with Deep Agents.

This file is intentionally throwaway. It does not import production services or
write to the knowledge vault.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BLOCKS = ROOT / "data/normalized/2608.22767/normalized/blocks.jsonl"
OUTPUT_ROOT = Path(__file__).resolve().parent / "outputs"
PROTOTYPE_DEPS = ROOT / ".prototype-deps/deepagents-0.7.11"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

NOTE_REQUEST = """请帮助一位刚进入 LLM Agent 记忆方向的硕士生理解这篇论文。
生成一篇结构清楚的中文笔记，必须包含：
1. 论文真正解决的问题
2. 核心直觉
3. 方法如何一步一步工作
4. 实验设置与主要结果如何解读
5. 局限与不能从论文推出的结论
6. 建议精读的章节及理由

每个关键论文事实后使用真实 block ID 引用，格式为 [block_id]。
不能读取到的内容请明确说不知道，不要用领域常识补齐论文事实。
"""


@dataclass(frozen=True)
class Block:
    block_id: str
    section_path: tuple[str, ...]
    kind: str
    text: str

    def render(self) -> str:
        section = " > ".join(self.section_path) or "(unknown section)"
        return f"[{self.block_id}]\nSECTION: {section}\nKIND: {self.kind}\n{self.text}"


def load_blocks(path: Path) -> list[Block]:
    blocks: list[Block] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = json.loads(line)
            text = (raw.get("text") or raw.get("latex") or raw.get("table_html") or "").strip()
            if not text:
                continue
            blocks.append(
                Block(
                    block_id=raw["block_id"],
                    section_path=tuple(raw.get("section_path") or ()),
                    kind=raw.get("kind", "text"),
                    text=text,
                )
            )
    return blocks


def keyword_score(block: Block, query: str) -> int:
    terms = {term.lower() for term in re.findall(r"[A-Za-z0-9_-]{3,}", query)}
    haystack = (" ".join(block.section_path) + " " + block.text).lower()
    return sum(haystack.count(term) for term in terms)


def search_blocks(blocks: list[Block], query: str, limit: int = 8) -> list[Block]:
    ranked = sorted(blocks, key=lambda block: keyword_score(block, query), reverse=True)
    return [block for block in ranked if keyword_score(block, query) > 0][:limit]


def workflow_context(blocks: list[Block], max_chars: int = 36_000) -> list[Block]:
    section_terms = (
        "abstract introduction method approach framework algorithm experiment result evaluation "
        "analysis conclusion limitation discussion"
    )
    selected: list[Block] = []
    seen: set[str] = set()
    for block in blocks[:2] + search_blocks(blocks, section_terms, limit=45):
        if block.block_id in seen:
            continue
        if sum(len(item.text) for item in selected) + len(block.text) > max_chars:
            break
        selected.append(block)
        seen.add(block.block_id)
    return selected


def deepseek_chat(*, system: str, user: str, model: str, api_key: str) -> tuple[str, dict[str, Any]]:
    started = time.perf_counter()
    response = httpx.post(
        DEEPSEEK_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": 7000,
        },
        timeout=180,
    )
    response.raise_for_status()
    payload = response.json()
    return payload["choices"][0]["message"]["content"], {
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "usage": payload.get("usage", {}),
        "model": payload.get("model", model),
        "model_calls": 1,
    }


def run_workflow(blocks: list[Block], model: str, api_key: str) -> tuple[str, dict[str, Any]]:
    selected = workflow_context(blocks)
    material = "\n\n---\n\n".join(block.render() for block in selected)
    note, receipt = deepseek_chat(
        system="你是严谨的论文阅读助手，只能使用用户提供的论文 blocks。",
        user=f"{NOTE_REQUEST}\n\n论文材料：\n{material}",
        model=model,
        api_key=api_key,
    )
    receipt.update(
        arm="workflow",
        tool_calls=0,
        selected_block_ids=[block.block_id for block in selected],
        selected_chars=sum(len(block.text) for block in selected),
    )
    return note, receipt


def install_prototype_path() -> None:
    if not PROTOTYPE_DEPS.exists():
        raise RuntimeError(f"prototype dependencies missing: {PROTOTYPE_DEPS}")
    sys.path.insert(0, str(PROTOTYPE_DEPS))


def run_deepagents(blocks: list[Block], model: str, api_key: str) -> tuple[str, dict[str, Any]]:
    install_prototype_path()
    from deepagents import create_deep_agent
    from deepagents.profiles import (
        GeneralPurposeSubagentProfile,
        HarnessProfile,
        register_harness_profile,
    )
    from langchain_core.tools import tool
    from langchain_openai import ChatOpenAI

    by_id = {block.block_id: block for block in blocks}
    calls: list[dict[str, Any]] = []

    @tool
    def search_paper(query: str, limit: int = 8) -> str:
        """Search the frozen paper blocks by keywords and return IDs with snippets."""
        hits = search_blocks(blocks, query, min(max(limit, 1), 12))
        calls.append({"tool": "search_paper", "query": query, "result_ids": [b.block_id for b in hits]})
        return "\n\n".join(
            f"[{block.block_id}] {' > '.join(block.section_path)}\n{block.text[:700]}" for block in hits
        ) or "NO_MATCH"

    @tool
    def read_blocks(block_ids: list[str]) -> str:
        """Read up to eight exact frozen paper blocks by their block IDs."""
        requested = block_ids[:8]
        found = [by_id[block_id] for block_id in requested if block_id in by_id]
        calls.append({"tool": "read_blocks", "requested_ids": requested, "result_ids": [b.block_id for b in found]})
        return "\n\n---\n\n".join(block.render() for block in found) or "NO_VALID_BLOCKS"

    chat_model = ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url="https://api.deepseek.com",
        temperature=0,
        max_tokens=7000,
        timeout=180,
    )
    profile_key = f"openai:{model}"
    register_harness_profile(
        profile_key,
        HarnessProfile(
            excluded_tools=frozenset({"write_file", "edit_file", "execute", "task", "write_todos"}),
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    agent = create_deep_agent(
        model=chat_model,
        tools=[search_paper, read_blocks],
        system_prompt=(
            "你是严谨的论文阅读助手。论文内容只能通过 search_paper 和 read_blocks 获取。"
            "关键论文事实必须引用实际读取到的 block ID；不知道就明确说明。"
        ),
    )
    started = time.perf_counter()
    result = agent.invoke({"messages": [{"role": "user", "content": NOTE_REQUEST}]})
    messages = result["messages"]
    note = next(
        (message.content for message in reversed(messages) if getattr(message, "type", "") == "ai" and message.content),
        "",
    )
    usage: dict[str, int] = {}
    for message in messages:
        metadata = getattr(message, "usage_metadata", None) or {}
        for key, value in metadata.items():
            if isinstance(value, int):
                usage[key] = usage.get(key, 0) + value
    return note, {
        "arm": "deepagents",
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "model": model,
        "model_calls": sum(1 for message in messages if getattr(message, "type", "") == "ai"),
        "tool_calls": len(calls),
        "calls": calls,
        "usage": usage,
    }


def citation_audit(note: str, blocks: list[Block]) -> dict[str, Any]:
    valid_ids = {block.block_id for block in blocks}
    bracketed = re.findall(r"\[([^\]\n]+)\]", note)
    cited = [
        value.strip()
        for value in bracketed
        if value.strip().startswith("normalized:")
        or re.fullmatch(r"[0-9a-f]{8,16}", value.strip(), flags=re.IGNORECASE)
    ]
    invalid = sorted({block_id for block_id in cited if block_id not in valid_ids})
    return {
        "citation_count": len(cited),
        "unique_citation_count": len(set(cited)),
        "invalid_citation_ids": invalid,
        "citation_validity": None if not cited else round(
            sum(1 for block_id in cited if block_id in valid_ids) / len(cited), 4
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=("workflow", "deepagents"))
    parser.add_argument("--audit-existing", action="store_true")
    parser.add_argument("--blocks", type=Path, default=DEFAULT_BLOCKS)
    args = parser.parse_args()

    blocks = load_blocks(args.blocks)
    if args.audit_existing:
        for arm in ("workflow", "deepagents"):
            output_dir = OUTPUT_ROOT / arm
            note = (output_dir / "note.md").read_text(encoding="utf-8")
            receipt_path = output_dir / "receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["citation_audit"] = citation_audit(note, blocks)
            receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({arm: json.loads((OUTPUT_ROOT / arm / "receipt.json").read_text(encoding="utf-8"))["citation_audit"] for arm in ("workflow", "deepagents")}, ensure_ascii=False, indent=2))
        return 0
    if not args.arm:
        parser.error("--arm is required unless --audit-existing is used")

    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY is not configured")

    if args.arm == "workflow":
        note, receipt = run_workflow(blocks, model, api_key)
    else:
        note, receipt = run_deepagents(blocks, model, api_key)

    receipt.update(
        source_blocks=str(args.blocks.relative_to(ROOT)),
        block_count=len(blocks),
        citation_audit=citation_audit(note, blocks),
        prototype=True,
    )
    output_dir = OUTPUT_ROOT / args.arm
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "note.md").write_text(note, encoding="utf-8")
    (output_dir / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
