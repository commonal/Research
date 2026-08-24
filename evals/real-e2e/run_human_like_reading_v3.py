from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from research_pulse.production.normalized import load_normalized_jsonl
from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import (
    CanonicalPaperIR,
    DeepSeekPaperReadingModel,
    PaperReader,
    ReadingIntent,
    _jsonable,
)


SOURCE_ID = "2608.18351v1"
NORMALIZED = ROOT / "tmp" / "reading-experiment-root" / SOURCE_ID / "normalized" / "blocks.jsonl"
IMAGE_ROOT = ROOT / "tmp" / "reading-experiment-root" / SOURCE_ID
OUTPUT = ROOT / "evals" / "real-e2e" / "human-like-experiment-v7"
OLD_NOTE = ROOT / "evals" / "real-e2e" / "2608.18351v1-ordered-units-note.md"
GOLDEN_NOTE = ROOT / "evals" / "real-e2e" / "2608.18351v1-golden-human-note.md"


def load_dotenv_without_printing() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    load_dotenv_without_printing()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if sys.argv[1:] == ["--compare-only"]:
        note = (OUTPUT / "reading-note.md").read_text(encoding="utf-8")
        comparison = _compare(note)
        _write("comparison.json", comparison)
        (OUTPUT / "comparison.md").write_text(_comparison_markdown(comparison), encoding="utf-8")
        return 0
    started = datetime.now(timezone.utc).isoformat()
    candidate = PaperCandidate(SOURCE_ID, "Task-Conditioned Least-Privilege Learning for Executable Terminal and MCP Agents", "https://arxiv.org/abs/2608.18351v1", "security")
    normalized = load_normalized_jsonl(NORMALIZED)
    paper_ir = CanonicalPaperIR.from_normalized(source_id=SOURCE_ID, title=candidate.title, source_url=candidate.source_url, blocks=normalized)
    paper_ir = replace(paper_ir, blocks=tuple(
        replace(block, image_path=str(_resolve_image(block.image_path)))
        if block.image_path and _resolve_image(block.image_path) is not None else block
        for block in paper_ir.blocks
    ))

    model = DeepSeekPaperReadingModel.from_environment()
    probe = model.probe()
    _write("provider-probe.json", probe)
    planner_input: dict[str, object] = {}
    planner_response: dict[str, object] = {}
    writer_input: dict[str, object] = {}

    def planner(value: dict) -> dict:
        planner_input.clear()
        planner_input.update(value)
        response = dict(model.plan_note(value))
        planner_response.clear()
        planner_response.update(response)
        return response

    def writer(value: dict) -> str:
        writer_input.clear()
        writer_input.update(value)
        return model.write_note(value)

    result = PaperReader(model, note_planner=planner, writer=writer).read(
        candidate,
        paper_ir,
        ReadingIntent(language="zh-CN", depth="deep", max_targets=6, max_expansions_per_target=1, max_text_calls=20, max_vision_calls=4),
    )
    trace = result.trace
    _write("paper-skeleton.json", trace.skeleton)
    _write("reading-questions.json", trace.questions)
    _write("reading-targets.json", trace.targets)
    _write("evidence-bundles.json", trace.bundles)
    _write("reading-records.json", trace.records)
    _write("argument-maps.json", trace.argument_maps)
    _write("visual-decisions.json", trace.visual_decisions)
    _write("reading-receipt.json", result.receipt)
    _write("reading-draft.json", result.draft)
    _write("note-plan.json", _note_plan_diagnostic(trace, result.draft.markdown, planner_input, planner_response, writer_input))
    (OUTPUT / "reading-note.md").write_text(result.draft.markdown, encoding="utf-8")
    comparison = _compare(result.draft.markdown)
    _write("comparison.json", comparison)
    (OUTPUT / "comparison.md").write_text(_comparison_markdown(comparison), encoding="utf-8")
    _write("run-meta.json", {"started_at": started, "completed_at": datetime.now(timezone.utc).isoformat(), "source_id": SOURCE_ID, "normalized": str(NORMALIZED), "block_count": len(paper_ir.blocks), "image_blocks": sum(block.is_visual and bool(block.image_path) for block in paper_ir.blocks)})
    return 0


def _note_plan_diagnostic(trace: object, note: str, planner_input: dict[str, object], planner_response: dict[str, object], writer_input: dict[str, object]) -> dict[str, object]:
    record_fact_ids = sorted({fact.fact_id for record in trace.records for fact in record.source_facts})
    input_facts = planner_input.get("must_preserve_facts", {}) if isinstance(planner_input, dict) else {}
    input_fact_ids = sorted(str(item.get("fact_id")) for item in input_facts.get("facts", ()) if isinstance(item, dict))
    note_plan = writer_input.get("note_plan", {}) if isinstance(writer_input, dict) else {}
    planned_facts = note_plan.get("must_preserve_facts", {}) if isinstance(note_plan, dict) else {}
    planned_fact_ids = sorted(str(item.get("fact_id")) for item in planned_facts.get("facts", ()) if isinstance(item, dict))
    writer_facts = writer_input.get("must_preserve_facts", {}) if isinstance(writer_input, dict) else {}
    writer_fact_ids = sorted(str(item.get("fact_id")) for item in writer_facts.get("facts", ()) if isinstance(item, dict))
    missing_record_to_plan = sorted(set(record_fact_ids) - set(planned_fact_ids))
    missing_plan_to_writer = sorted(set(planned_fact_ids) - set(writer_fact_ids))
    missing_note_tokens = []
    for item in planned_facts.get("facts", ()) if isinstance(planned_facts, dict) else ():
        if not isinstance(item, dict):
            continue
        tokens = tuple(dict.fromkeys((*item.get("numeric_tokens", ()), *item.get("named_entities", ()), *item.get("mechanism_terms", ()))))
        missing = [token for token in tokens if token and str(token).casefold() not in note.casefold()]
        if missing:
            missing_note_tokens.append({"fact_id": item.get("fact_id"), "missing_tokens": missing})
    if missing_record_to_plan:
        loss_stage = "Record→Plan"
    elif missing_plan_to_writer:
        loss_stage = "Plan→Writer"
    elif missing_note_tokens:
        loss_stage = "Plan→Writer"
    else:
        loss_stage = "none_observed"
    return {
        "record_fact_ids": record_fact_ids,
        "planner_input_fact_ids": input_fact_ids,
        "planner_raw_response": planner_response,
        "planned_fact_ids": planned_fact_ids,
        "writer_fact_ids": writer_fact_ids,
        "missing_record_to_plan": missing_record_to_plan,
        "missing_plan_to_writer": missing_plan_to_writer,
        "missing_note_tokens": missing_note_tokens,
        "loss_stage": loss_stage,
        "planner_received_typed_state": "must_preserve_facts" in planner_input and "compressed_records" in planner_input,
        "writer_received_typed_state": "note_plan" in writer_input and "must_preserve_facts" in writer_input and "compressed_records" in writer_input,
    }


def _write(name: str, value: object) -> None:
    (OUTPUT / name).write_text(json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _resolve_image(image_path: str | None) -> Path | None:
    if not image_path:
        return None
    candidates = (
        IMAGE_ROOT / image_path,
        IMAGE_ROOT / "mineru" / "source" / "auto" / image_path,
    )
    return next((path.resolve() for path in candidates if path.is_file()), None)


def _compare(note: str) -> dict[str, object]:
    old = OLD_NOTE.read_text(encoding="utf-8") if OLD_NOTE.is_file() else ""
    golden = GOLDEN_NOTE.read_text(encoding="utf-8") if GOLDEN_NOTE.is_file() else ""
    concepts = ["Qwen3.5-4B", "安全成功", "64.36%", "98.48%", "4.56%", "0.79%", "broker", "前后置审计", "不能替代权限门禁和沙箱"]
    dimension_terms = {
        "main_story": ("问题", "方法", "实验"),
        "background_problem_gap": ("背景", "问题", "局限", "门控"),
        "method_hierarchy": ("核心", "机制", "broker", "六维"),
        "problem_to_method": ("问题", "后训练", "权限"),
        "method_to_experiment": ("机制", "实验", "验证"),
        "long_range_evidence": ("摘要", "实验", "局限"),
        "duplication_and_order": ("研究背景", "核心机制", "实验验证", "结论边界"),
        "explicit_unknowns": ("未知", "边界", "局限"),
    }
    dimensions = {
        name: {
            "new_term_hits": [term for term in terms if term in note],
            "golden_term_hits": [term for term in terms if term in golden],
            "status": "needs_review" if any(term not in note for term in terms) else "observed",
        }
        for name, terms in dimension_terms.items()
    }
    return {
        "old_unit_note": str(OLD_NOTE),
        "golden_note": str(GOLDEN_NOTE),
        "new_markdown_chars": len(note),
        "old_markdown_chars": len(old),
        "golden_markdown_chars": len(golden),
        "concept_hits": {concept: concept in note for concept in concepts},
        "dimensions": dimensions,
        "has_raw_block_id": "normalized:" in note,
        "has_provider_payload_marker": "choices" in note or "response_format" in note,
        "has_trace_marker": "ReadingRecord" in note or "ArgumentMap" in note or "TransportUnit" in note,
        "sections": [line[3:].strip() for line in note.splitlines() if line.startswith("## ")],
    }


def _comparison_markdown(value: dict[str, object]) -> str:
    hits = value["concept_hits"]
    lines = ["# 2608.18351v1 problem-driven reading comparison", "", f"- 新笔记字符数：{value['new_markdown_chars']}", f"- 旧 Unit 笔记字符数：{value['old_markdown_chars']}", f"- 黄金笔记字符数：{value['golden_markdown_chars']}", "", "## 概念命中", ""]
    lines.extend(f"- {key}: {'命中' if hit else '未命中'}" for key, hit in hits.items())
    lines.extend(["", "## 验收维度（程序化初筛，不等同盲读通过）", ""])
    for name, item in value["dimensions"].items():
        lines.append(f"- {name}: {item['status']}；新笔记命中 {item['new_term_hits']}；黄金笔记命中 {item['golden_term_hits']}")
    lines.extend(["", "## 知识层边界", "", f"- raw block ID：{value['has_raw_block_id']}", f"- provider payload：{value['has_provider_payload_marker']}", f"- trace internals：{value['has_trace_marker']}", ""])
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
