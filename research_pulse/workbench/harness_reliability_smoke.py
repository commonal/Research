"""One-question, bounded live smoke for the Workbench Harness reliability gate."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import traceback

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

from research_pulse.workbench.agent_runtime import AgentRunInput, RunBudgets
from research_pulse.workbench.deepagents_v0 import (
    ALLOWED_TOOLS,
    DeepAgentsV0Runtime,
    extract_actual_block_ids,
)
from research_pulse.workbench.harness_v0_fixture import load_and_validate_fixture
from research_pulse.workbench.harness_v0_runner import MANIFEST, ROOT, _citation_ids, _tools_factory


RECEIPT = (
    ROOT
    / "openspec"
    / "changes"
    / "refactor-workbench-harness-lifecycle"
    / "evidence"
    / "11.5-real-service-smoke-2026-09-09.json"
)
SMOKE_TOOLS = ALLOWED_TOOLS
SMOKE_BUDGET = RunBudgets(
    model_rounds=6,
    tool_calls=10,
    block_reads=14,
    wall_seconds=120,
    input_tokens=20_000,
    output_tokens=4_000,
)


def _safe_failure(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, RuntimeError) and "not configured" in str(exc):
        return "configuration_missing", "真实模型凭证未配置"
    name = type(exc).__name__.lower()
    if any(token in name for token in ("timeout", "connection", "rate", "api")):
        return "external_service_unavailable", "真实外部服务调用未完成"
    return "harness_failure", "真实冒烟未完成；详细异常未写入脱敏回执"


def run_smoke() -> Path:
    load_dotenv(ROOT / ".env")
    fixture = load_and_validate_fixture(MANIFEST, project_root=ROOT)
    question_id = "q-single-source-verifier-signal-smoke"
    question_text = (
        "Using only the supplied managed source, briefly explain how persistent "
        "verifier signals govern long-term agent memory. Cite the managed block IDs used."
    )
    source_ids = ("2608.21867",)
    model_name = fixture["model"]["model"]
    receipt: dict[str, object] = {
        "schema_version": "workbench-harness-reliability-smoke-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "status": "blocked",
        "real_model": True,
        "real_research_tools": True,
        "question_id": question_id,
        "provider": fixture["model"]["provider"],
        "model": model_name,
        "limits": asdict(SMOKE_BUDGET),
        "effective_tools": list(SMOKE_TOOLS),
        "workspace_writes_allowed": False,
        "deterministic_contracts_replaced": False,
    }
    try:
        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        runtime = DeepAgentsV0Runtime(
            model_factory=lambda guard: ChatOpenAI(
                model=model_name,
                api_key=api_key,
                base_url=os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com"),
                temperature=0,
                max_completion_tokens=800,
                max_retries=0,
                timeout=45,
            ),
            tools_factory=_tools_factory,
        )
        events = []
        result = runtime.start(AgentRunInput(
            run_id="harness-reliability-live-smoke",
            question=question_text,
            allowed_tools=SMOKE_TOOLS,
            budgets=SMOKE_BUDGET,
            material_source_ids=source_ids,
            on_event=events.append,
        ))
        actual_ids = extract_actual_block_ids(events)
        cited_ids = _citation_ids(result.final_draft)
        passed = (
            result.stop_reason == "completed"
            and bool(actual_ids)
            and bool(cited_ids)
            and cited_ids <= actual_ids
        )
        receipt.update({
            "status": "passed" if passed else "failed",
            "stop_reason": result.stop_reason,
            "event_count": len(events),
            "event_types": [
                event.event_type.value
                if hasattr(event.event_type, "value") else str(event.event_type)
                for event in events
            ],
            "actual_block_ids": sorted(actual_ids),
            "resolved_citation_ids": sorted(cited_ids & actual_ids),
            "unresolved_citation_ids": sorted(cited_ids - actual_ids),
            "citation_closed_loop": bool(cited_ids) and cited_ids <= actual_ids,
        })
    except Exception as exc:
        category, message = _safe_failure(exc)
        last_frame = traceback.extract_tb(exc.__traceback__)[-1]
        receipt.update({
            "status": "blocked",
            "blocker_category": category,
            "safe_message": message,
            "diagnostic_type": type(exc).__name__,
            "diagnostic_location": {
                "file": Path(last_frame.filename).name,
                "line": last_frame.lineno,
                "function": last_frame.name,
            },
        })
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return RECEIPT


if __name__ == "__main__":
    print(run_smoke().relative_to(ROOT))
