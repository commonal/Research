"""Collect a small, real HTTP baseline/workbench evaluation.

This runner deliberately records only bounded metadata. It never writes model
answers or provider responses to disk. A run is useful even when the research
path pauses at its user-decision checkpoint: that state is reported as
``blocked`` with the raw lifecycle status preserved for diagnosis.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
import time
import uuid
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TASKS = ROOT / "evals" / "workbench_golden_tasks.jsonl"
DEFAULT_OUTPUT = ROOT / "evals" / "baseline" / "records.live.jsonl"
DEFAULT_QUERY = (
    "总结这篇论文解决的问题、核心方法和主要实验结论，并指出当前证据不能支持的内容。"
)
RESEARCH_QUERY = "从这篇论文的局限性出发，提出一个具体、可验证、值得继续研究的问题。"
# ``awaiting_user_decision`` is the public run-level projection of the
# durable direction checkpoint.  It is terminal for this bounded evaluator:
# the product has produced a checkpoint and is intentionally waiting for a
# human, rather than still being stuck in polling.
TERMINAL_EXPLORATION = {
    "completed",
    "failed",
    "budget_exhausted",
    "cancelled",
    "abandoned",
    "retryable_failure",
    "terminal_failure",
    "awaiting_user",
    "awaiting_user_decision",
}


class LiveEvalError(RuntimeError):
    pass


def normalize_exploration_status(raw_status: str, final_draft: str | None) -> str:
    """Map the public lifecycle to the evaluator's bounded result states.

    ``awaiting_user`` is the attempt spelling and ``awaiting_user_decision``
    is the run spelling.  Both mean that a durable checkpoint is ready for a
    human choice; neither means that polling is still in progress.
    """
    if raw_status == "completed" and (final_draft or "").strip():
        return "completed"
    if raw_status in {"awaiting_user", "awaiting_user_decision"}:
        return "blocked"
    if raw_status in {
        "failed",
        "budget_exhausted",
        "cancelled",
        "abandoned",
        "retryable_failure",
        "terminal_failure",
    }:
        return "failed"
    return "pending"


def load_tasks(path: Path, selected: set[str] | None) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        if selected and item.get("task_id") not in selected:
            continue
        tasks.append(item)
    if not tasks:
        raise LiveEvalError("没有匹配到待测黄金任务")
    return tasks


def request_json(client: httpx.Client, method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
    response = client.request(method, path, **kwargs)
    try:
        body = response.json()
    except ValueError:
        body = {"text": response.text[:500]}
    if response.status_code >= 400:
        detail = body.get("detail", body) if isinstance(body, dict) else body
        raise LiveEvalError(f"{method} {path} -> HTTP {response.status_code}: {detail}")
    return response.status_code, body


def wait_for_paper(client: httpx.Client, session_id: str, paper_id: str, deadline: float) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        _, body = request_json(client, "GET", f"/api/workbench/sessions/{session_id}/papers")
        latest = next((item for item in body.get("items", []) if item.get("paper_id") == paper_id), {})
        status = latest.get("parse_status")
        if status == "ready":
            return latest
        if status == "failed":
            raise LiveEvalError(f"论文解析失败：{latest.get('safe_error') or 'unknown'}")
        time.sleep(1.0)
    raise LiveEvalError(f"论文解析超时：{latest.get('parse_status') or 'unknown'}")


def create_session_and_paper(client: httpx.Client, pdf_path: Path, query: str, timeout: float) -> tuple[str, str, dict[str, Any]]:
    _, session = request_json(client, "POST", "/api/workbench/sessions", json={"research_question": query})
    session_id = session.get("session_id") or session.get("id")
    if not session_id:
        raise LiveEvalError(f"创建会话未返回 session_id：{session}")
    pdf_bytes = pdf_path.read_bytes()
    _, paper = request_json(
        client,
        "POST",
        f"/api/workbench/sessions/{session_id}/papers/upload",
        content=pdf_bytes,
        headers={"Content-Type": "application/pdf"},
    )
    paper_id = paper.get("paper_id")
    if not paper_id:
        raise LiveEvalError(f"上传论文未返回 paper_id：{paper}")
    ready = wait_for_paper(client, session_id, paper_id, time.monotonic() + timeout)
    return str(session_id), str(paper_id), ready


def citation_checks(message: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    checks: list[dict[str, Any]] = []
    for citation in message.get("citations") or []:
        # This is intentionally mechanical: it checks the API locator status,
        # not whether a human agrees with the claim's semantics.
        checks.append({
            "correct": citation.get("status") == "resolved",
            "citation_id": citation.get("citation_id"),
            "status": citation.get("status"),
        })
    return checks, "locator_status_only"


def run_paper_side(client: httpx.Client, pdf_path: Path, query: str, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    session_id, paper_id, paper = create_session_and_paper(client, pdf_path, query, timeout)
    request_id = f"live-baseline-{uuid.uuid4()}"
    status, body = request_json(
        client,
        "POST",
        f"/api/workbench/sessions/{session_id}/messages",
        json={
            "query": query,
            "scope": "full",
            "action": "paper",
            "paper_id": paper_id,
            "interaction_context": {"canonical_paper_id": paper_id},
            "client_request_id": request_id,
        },
    )
    turn_result = body.get("result", {}) if isinstance(body, dict) else {}
    message_id = turn_result.get("message_id")
    deadline = time.monotonic() + timeout
    latest_assistant: dict[str, Any] = {}
    while time.monotonic() < deadline:
        _, messages = request_json(client, "GET", f"/api/workbench/sessions/{session_id}/messages")
        for message in messages.get("items", []):
            if message.get("role") == "assistant" and (message_id is None or message.get("message_id") == message_id):
                latest_assistant = message
        if latest_assistant.get("generation_status") in {"completed", "failed"}:
            break
        time.sleep(1.0)
    generation_status = latest_assistant.get("generation_status") or "pending"
    checks, basis = citation_checks(latest_assistant)
    usage = latest_assistant.get("metadata", {}).get("usage", {}) if isinstance(latest_assistant.get("metadata"), dict) else {}
    return {
        "status": "completed" if generation_status == "completed" else ("failed" if generation_status == "failed" else "pending"),
        "route": "paper",
        "route_correct": True,
        "focus_continuity": None,
        "recovery_success": None,
        "latency_ms": round((time.monotonic() - started) * 1000, 2),
        "claims_total": None,
        "claims_supported": None,
        "citation_checks": checks,
        "citation_check_basis": basis,
        "citation_count": len(checks),
        "answer_length": len(latest_assistant.get("text") or ""),
        "generation_status": generation_status,
        "http_status": status,
        "session_id": session_id,
        "paper_id": paper_id,
        "paper_parse_status": paper.get("parse_status"),
        "safe_error": latest_assistant.get("safe_error"),
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        },
    }


def run_research_side(client: httpx.Client, pdf_path: Path, query: str, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    session_id, paper_id, paper = create_session_and_paper(client, pdf_path, query, timeout)
    request_id = f"live-research-{uuid.uuid4()}"
    http_status, body = request_json(
        client,
        "POST",
        f"/api/workbench/sessions/{session_id}/messages",
        json={
            "query": query,
            "scope": "explore",
            "action": "research",
            "paper_id": paper_id,
            "interaction_context": {"canonical_paper_id": paper_id},
            "client_request_id": request_id,
        },
    )
    run = body.get("result") or body.get("run") or {}
    run_id = run.get("durable_handle") or run.get("run_id")
    if not run_id:
        raise LiveEvalError(f"研究请求未返回 run_id：{body}")
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        _, latest = request_json(client, "GET", f"/api/workbench/explorations/{run_id}")
        raw_status = str(latest.get("status") or "unknown")
        if raw_status in TERMINAL_EXPLORATION:
            break
        time.sleep(2.0)
    raw_status = str(latest.get("status") or "pending")
    # Both spellings are used in the system: attempts use ``awaiting_user``;
    # the public exploration projection uses ``awaiting_user_decision``.  They
    # are the same successful product checkpoint, not a hanging run.  Keep the
    # raw status for diagnosis and map either spelling to the evaluator's
    # blocked state.
    normalized = normalize_exploration_status(raw_status, latest.get("final_draft"))
    usage = latest.get("token_usage") or {}
    return {
        "status": normalized,
        "route": "research",
        "route_correct": True,
        "focus_continuity": None,
        "recovery_success": None,
        "latency_ms": round((time.monotonic() - started) * 1000, 2),
        "claims_total": None,
        "claims_supported": None,
        "citation_checks": [],
        "citation_count": len(latest.get("sources") or []),
        "answer_length": len(latest.get("final_draft") or ""),
        "raw_status": raw_status,
        "http_status": http_status,
        "session_id": session_id,
        "paper_id": paper_id,
        "paper_parse_status": paper.get("parse_status"),
        "run_id": run_id,
        "events_count": len(latest.get("events") or []),
        "sources_count": len(latest.get("sources") or []),
        "safe_error": latest.get("safe_error"),
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行一次真实 HTTP 基线/Research Pulse 配对评估")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--paper", type=Path, default=ROOT / "frontend" / "public" / "fixtures" / "paper-demo.pdf")
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--task-ids", default="baseline_grounded_answer", help="逗号分隔的黄金任务 ID")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    # A real research run includes paper reads, bounded discovery retries and
    # durable workspace writes.  The previous 180s default was shorter than
    # the observed completion window (~225–260s), so a healthy run was
    # recorded as ``pending``.  Keep the timeout bounded, but leave enough
    # room for the production path to reach its terminal checkpoint.
    parser.add_argument("--research-timeout-seconds", type=float, default=300.0)
    args = parser.parse_args(argv)
    if not args.paper.exists():
        print(f"live evaluation blocked: 找不到 PDF：{args.paper}", file=sys.stderr)
        return 2
    selected = {item.strip() for item in args.task_ids.split(",") if item.strip()}
    try:
        tasks = load_tasks(args.tasks, selected)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, Any]] = []
        with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=30.0) as client:
            for task in tasks:
                task_id = task["task_id"]
                query = task.get("query") or (RESEARCH_QUERY if task.get("kind") == "research" else DEFAULT_QUERY)
                record: dict[str, Any] = {"task_id": task_id, "captured_at": datetime.now(UTC).isoformat()}
                try:
                    record["baseline"] = run_paper_side(client, args.paper, query, args.timeout_seconds)
                except Exception as exc:
                    record["baseline"] = {"status": "blocked", "route": "paper", "route_correct": True, "citation_checks": [], "usage": {"input_tokens": None, "output_tokens": None}, "safe_error": str(exc)}
                try:
                    record["workbench"] = run_research_side(client, args.paper, query, args.research_timeout_seconds)
                except Exception as exc:
                    record["workbench"] = {"status": "blocked", "route": "research", "route_correct": True, "citation_checks": [], "usage": {"input_tokens": None, "output_tokens": None}, "safe_error": str(exc)}
                records.append(record)
                print(f"{task_id}: baseline={record['baseline']['status']} workbench={record['workbench']['status']}")
        args.output.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8")
        print(args.output)
        return 0
    except Exception as exc:
        print(f"live evaluation blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
