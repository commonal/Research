"""Run the frozen Deep Agents V0 questions and write a safe JSON receipt."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

from research_pulse.workbench.agent_runtime import AgentRunInput, RunBudgets
from research_pulse.workbench.deepagents_v0 import ALLOWED_TOOLS, DeepAgentsV0Runtime, extract_actual_block_ids
from research_pulse.workbench.harness_v0_fixture import load_and_validate_fixture
from research_pulse.workbench.research_tools import ReadOnlyResearchTools


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "experiments" / "workbench-harness-v0" / "manifest.json"
RECEIPTS = MANIFEST.parent / "receipts"
_CITATION = re.compile(
    r"normalized:[A-Za-z0-9._-]+:(?:text|figure|table|equation):[A-Za-z0-9]+"
)


def _tools_factory(run_id, status_reader):
    return ReadOnlyResearchTools.from_fixture(
        MANIFEST, run_id=run_id, status_reader=status_reader, project_root=ROOT
    )


def _budgets(raw: dict[str, int]) -> RunBudgets:
    return RunBudgets(**raw)


def _citation_ids(draft: str | None) -> frozenset[str]:
    return frozenset(_CITATION.findall(draft or ""))


def revalidate_receipt(path: Path) -> Path:
    receipt = json.loads(path.read_text(encoding="utf-8"))
    for item in receipt["runs"]:
        actual_ids = frozenset(item["actual_block_ids"])
        cited_ids = _citation_ids(item.get("final_draft"))
        item["resolved_citation_ids"] = sorted(cited_ids & actual_ids)
        item["unresolved_citation_ids"] = sorted(cited_ids - actual_ids)
        item["citation_closed_loop"] = bool(cited_ids) and cited_ids <= actual_ids
    receipt["passed"] = all(
        item["stop_reason"] == "completed"
        and item["actual_block_ids"]
        and item["citation_closed_loop"]
        for item in receipt["runs"]
    )
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_v0() -> Path:
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")
    fixture = load_and_validate_fixture(MANIFEST, project_root=ROOT)
    model_name = fixture["model"]["model"]
    budget = _budgets(fixture["budget"])
    results = []
    for index, question in enumerate(fixture["questions"], start=1):
        runtime = DeepAgentsV0Runtime(
            model_factory=lambda guard: ChatOpenAI(
                model=model_name,
                api_key=api_key,
                base_url=os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com"),
                temperature=0,
                max_completion_tokens=1000,
                max_retries=1,
                timeout=60,
            ),
            tools_factory=_tools_factory,
        )
        run_id = f"v0-live-{index}"
        events = []
        result = runtime.start(AgentRunInput(
            run_id=run_id,
            question=question["text"],
            allowed_tools=ALLOWED_TOOLS,
            budgets=budget,
            material_source_ids=tuple(question["source_ids"]),
            on_event=events.append,
        ))
        actual_ids = extract_actual_block_ids(events)
        cited_ids = _citation_ids(result.final_draft)
        results.append({
            "run_id": run_id,
            "question_id": question["question_id"],
            "stop_reason": result.stop_reason,
            "events": [asdict(event) for event in events],
            "final_draft": result.final_draft,
            "actual_block_ids": sorted(actual_ids),
            "resolved_citation_ids": sorted(cited_ids & actual_ids),
            "unresolved_citation_ids": sorted(cited_ids - actual_ids),
            "citation_closed_loop": bool(cited_ids) and cited_ids <= actual_ids,
        })
    receipt = {
        "schema_version": "workbench-harness-v0-live-receipt-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "harness": fixture["harness"],
        "model": {"provider": fixture["model"]["provider"], "model": model_name},
        "backend": "deepagents.backends.StateBackend",
        "checkpointer": None,
        "effective_tools": list(ALLOWED_TOOLS),
        "budget": fixture["budget"],
        "runs": results,
        "passed": all(
            item["stop_reason"] == "completed"
            and item["actual_block_ids"]
            and item["citation_closed_loop"]
            for item in results
        ),
    }
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    path = RECEIPTS / "v0-live-latest.json"
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    receipt_path = (
        revalidate_receipt(RECEIPTS / "v0-live-latest.json")
        if "--revalidate" in sys.argv
        else run_v0()
    )
    print(receipt_path.relative_to(ROOT))
