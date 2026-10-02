"""Summarize a paired baseline/workbench evaluation without exposing answers."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import statistics
import sys
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TASKS = ROOT / "evals" / "workbench_golden_tasks.jsonl"
DEFAULT_OUTPUT = ROOT / "evals" / "baseline" / "runs" / datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
SIDES = ("baseline", "workbench")
VALID_STATUSES = {"completed", "failed", "blocked", "pending"}


class InputError(ValueError):
    pass


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    kind: str
    expected_route: str


def _jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise InputError(f"无法读取文件：{path}") from exc
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise InputError(f"{path} 第 {line_no} 行不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise InputError(f"{path} 第 {line_no} 行必须是 JSON 对象")
        yield line_no, value


def load_tasks(path: Path) -> dict[str, TaskSpec]:
    tasks: dict[str, TaskSpec] = {}
    for line_no, item in _jsonl(path):
        task_id = item.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise InputError(f"黄金任务第 {line_no} 行缺少 task_id")
        if task_id in tasks:
            raise InputError(f"黄金任务重复：{task_id}")
        tasks[task_id] = TaskSpec(
            task_id=task_id,
            kind=str(item.get("kind", "unknown")),
            expected_route=str(item.get("expected_route", "")),
        )
    if not tasks:
        raise InputError("黄金任务集为空")
    return tasks


def _number(value: Any, *, field: str, line_no: int, minimum: float = 0) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise InputError(f"记录第 {line_no} 行的 {field} 必须是 >= {minimum} 的数字或 null")
    return float(value)


def _boolean(value: Any, *, field: str, line_no: int) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise InputError(f"记录第 {line_no} 行的 {field} 必须是布尔值或 null")
    return value


def _side_record(item: dict[str, Any], side: str, line_no: int) -> dict[str, Any]:
    record = item.get(side)
    if not isinstance(record, dict):
        raise InputError(f"记录第 {line_no} 行缺少 {side} 对象")
    status = record.get("status")
    if status not in VALID_STATUSES:
        raise InputError(f"记录第 {line_no} 行的 {side}.status 无效：{status!r}")
    route = record.get("route")
    if route is not None and not isinstance(route, str):
        raise InputError(f"记录第 {line_no} 行的 {side}.route 必须是字符串或 null")
    route_correct = _boolean(record.get("route_correct"), field=f"{side}.route_correct", line_no=line_no)
    focus_continuity = _boolean(record.get("focus_continuity"), field=f"{side}.focus_continuity", line_no=line_no)
    recovery_success = _boolean(record.get("recovery_success"), field=f"{side}.recovery_success", line_no=line_no)
    latency_ms = _number(record.get("latency_ms"), field=f"{side}.latency_ms", line_no=line_no)
    claims_total = _number(record.get("claims_total"), field=f"{side}.claims_total", line_no=line_no)
    claims_supported = _number(record.get("claims_supported"), field=f"{side}.claims_supported", line_no=line_no)
    if claims_total is not None and claims_supported is not None and claims_supported > claims_total:
        raise InputError(f"记录第 {line_no} 行的 {side}.claims_supported 不能大于 claims_total")
    checks = record.get("citation_checks", [])
    if not isinstance(checks, list):
        raise InputError(f"记录第 {line_no} 行的 {side}.citation_checks 必须是数组")
    normalized_checks: list[bool] = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict) or not isinstance(check.get("correct"), bool):
            raise InputError(f"记录第 {line_no} 行的 {side}.citation_checks[{index}] 缺少布尔 correct")
        normalized_checks.append(check["correct"])
    usage = record.get("usage", {})
    if not isinstance(usage, dict):
        raise InputError(f"记录第 {line_no} 行的 {side}.usage 必须是对象")
    input_tokens = _number(usage.get("input_tokens"), field=f"{side}.usage.input_tokens", line_no=line_no)
    output_tokens = _number(usage.get("output_tokens"), field=f"{side}.usage.output_tokens", line_no=line_no)
    return {
        "status": status,
        "route": route,
        "route_correct": route_correct,
        "focus_continuity": focus_continuity,
        "recovery_success": recovery_success,
        "latency_ms": latency_ms,
        "claims_total": claims_total,
        "claims_supported": claims_supported,
        "citation_checks": normalized_checks,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def load_records(path: Path, tasks: dict[str, TaskSpec]) -> dict[str, dict[str, dict[str, Any]]]:
    records: dict[str, dict[str, dict[str, Any]]] = {}
    for line_no, item in _jsonl(path):
        task_id = item.get("task_id")
        if not isinstance(task_id, str) or task_id not in tasks:
            raise InputError(f"记录第 {line_no} 行的 task_id 不在黄金任务集内：{task_id!r}")
        if task_id in records:
            raise InputError(f"记录重复：{task_id}")
        records[task_id] = {side: _side_record(item, side, line_no) for side in SIDES}
    return records


def _ratio(numerator: int | float, denominator: int | float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    rank = (len(ordered) - 1) * percentile
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 2)


def summarize_side(
    task_ids: Iterable[str],
    records: dict[str, dict[str, dict[str, Any]]],
    side: str,
) -> dict[str, Any]:
    selected = [records[task_id][side] for task_id in task_ids if task_id in records]
    completed = [item for item in selected if item["status"] == "completed"]
    citation_checks = [check for item in completed for check in item["citation_checks"]]
    supported = sum(item["claims_supported"] or 0 for item in completed)
    claims = sum(item["claims_total"] or 0 for item in completed)
    route_values = [item["route_correct"] for item in completed if item["route_correct"] is not None]
    focus_values = [item["focus_continuity"] for item in completed if item["focus_continuity"] is not None]
    recovery_values = [item["recovery_success"] for item in completed if item["recovery_success"] is not None]
    latencies = [item["latency_ms"] for item in completed if item["latency_ms"] is not None]
    input_tokens = [item["input_tokens"] for item in completed if item["input_tokens"] is not None]
    output_tokens = [item["output_tokens"] for item in completed if item["output_tokens"] is not None]
    return {
        "tasks_seen": len(selected),
        "completed": len(completed),
        "failed": sum(item["status"] == "failed" for item in selected),
        "blocked": sum(item["status"] == "blocked" for item in selected),
        "pending": sum(item["status"] == "pending" for item in selected),
        "citation_precision": _ratio(sum(citation_checks), len(citation_checks)),
        "citation_checks": len(citation_checks),
        "evidence_coverage": _ratio(supported, claims),
        "claims_supported": int(supported),
        "claims_total": int(claims),
        "route_accuracy": _ratio(sum(route_values), len(route_values)),
        "route_cases": len(route_values),
        "focus_continuity": _ratio(sum(focus_values), len(focus_values)),
        "focus_cases": len(focus_values),
        "recovery_success": _ratio(sum(recovery_values), len(recovery_values)),
        "recovery_cases": len(recovery_values),
        "latency_ms": {
            "median": round(statistics.median(latencies), 2) if latencies else None,
            "p95": _percentile(latencies, 0.95),
            "samples": len(latencies),
        },
        "usage": {
            "input_tokens_total": int(sum(input_tokens)),
            "output_tokens_total": int(sum(output_tokens)),
            "input_tokens_average": round(statistics.mean(input_tokens), 2) if input_tokens else None,
            "output_tokens_average": round(statistics.mean(output_tokens), 2) if output_tokens else None,
        },
    }


def build_summary(tasks: dict[str, TaskSpec], records: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    missing = sorted(set(tasks) - set(records))
    task_status = {
        task_id: {
            "kind": tasks[task_id].kind,
            "expected_route": tasks[task_id].expected_route,
            "baseline": records[task_id]["baseline"]["status"] if task_id in records else "missing",
            "workbench": records[task_id]["workbench"]["status"] if task_id in records else "missing",
        }
        for task_id in sorted(tasks)
    }
    return {
        "schema_version": "research-pulse-baseline-summary-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "task_count": len(tasks),
        "recorded_task_count": len(records),
        "complete": not missing and all(
            records[task_id][side]["status"] == "completed"
            for task_id in records
            for side in SIDES
        ),
        "missing_task_ids": missing,
        "sides": {
            side: summarize_side(tasks.keys(), records, side)
            for side in SIDES
        },
        "tasks": task_status,
        "interpretation": {
            "quality_claim": "UNKNOWN until human citation and evidence review is complete",
            "cost_tradeoff": "Report latency and token differences; do not treat lower cost as higher quality",
        },
    }


def render_report(summary: dict[str, Any]) -> str:
    lines = [
        "# Research Pulse 基线评估报告",
        "",
        f"- Schema: `{summary['schema_version']}`",
        f"- 任务数：{summary['task_count']}",
        f"- 已记录：{summary['recorded_task_count']}",
        f"- 完整评估：`{'是' if summary['complete'] else '否'}`",
        "",
        "> 本报告只汇总脱敏计数和运行指标，不包含完整回答或 provider 原始响应。",
        "",
        "## 侧别汇总",
        "",
        "| 指标 | 普通基线 | Research Pulse |",
        "| --- | ---: | ---: |",
    ]
    baseline = summary["sides"]["baseline"]
    workbench = summary["sides"]["workbench"]
    rows = [
        ("完成任务数", baseline["completed"], workbench["completed"]),
        ("引用准确率", baseline["citation_precision"], workbench["citation_precision"]),
        ("证据覆盖率", baseline["evidence_coverage"], workbench["evidence_coverage"]),
        ("路由正确率", baseline["route_accuracy"], workbench["route_accuracy"]),
        ("焦点连续性", baseline["focus_continuity"], workbench["focus_continuity"]),
        ("恢复成功率", baseline["recovery_success"], workbench["recovery_success"]),
        ("延迟中位数 ms", baseline["latency_ms"]["median"], workbench["latency_ms"]["median"]),
        ("延迟 P95 ms", baseline["latency_ms"]["p95"], workbench["latency_ms"]["p95"]),
        ("输入 token 总量", baseline["usage"]["input_tokens_total"], workbench["usage"]["input_tokens_total"]),
        ("输出 token 总量", baseline["usage"]["output_tokens_total"], workbench["usage"]["output_tokens_total"]),
    ]
    for label, left, right in rows:
        lines.append(f"| {label} | {left if left is not None else 'UNKNOWN'} | {right if right is not None else 'UNKNOWN'} |")
    lines.extend(["", "## 任务状态", "", "| 任务 | 类型 | 基线 | 工作台 |", "| --- | --- | --- | --- |"])
    for task_id, item in summary["tasks"].items():
        lines.append(f"| `{task_id}` | {item['kind']} | {item['baseline']} | {item['workbench']} |")
    lines.extend([
        "",
        "## 判定规则",
        "",
        "- 只有所有任务两侧都有真实回执后，`complete` 才会为 `true`。",
        "- 质量结论必须结合人工引用检查；本报告不会从回答长度推断质量。",
        "- Research Pulse 不需要在延迟或 token 上全面优于基线，必须公开质量提升和成本代价。",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汇总 Research Pulse 与普通基线的配对评估")
    parser.add_argument("--records", type=Path, required=True, help="配对评估 records.jsonl")
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS, help="黄金任务 JSONL")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--ignore-incomplete", action="store_true", help="调试格式时允许任务不完整")
    args = parser.parse_args(argv)
    try:
        tasks = load_tasks(args.tasks)
        records = load_records(args.records, tasks)
        summary = build_summary(tasks, records)
        if not summary["complete"] and not args.ignore_incomplete:
            missing = ", ".join(summary["missing_task_ids"]) or "存在 pending/failed/blocked 记录"
            raise InputError(f"评估尚未完整：{missing}")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (args.output_dir / "report.md").write_text(render_report(summary), encoding="utf-8")
        print(args.output_dir)
        return 0
    except InputError as exc:
        print(f"baseline evaluation blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
