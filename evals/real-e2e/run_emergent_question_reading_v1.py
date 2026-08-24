from __future__ import annotations

"""Throwaway experiment: let reading questions emerge while reading.

This file deliberately does not use or modify the production PaperReader loop.
It tests one design question only: is a skim with 1-2 entry questions followed
by evidence-driven question discovery better than generating a fixed question
list up front?
"""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
import html
import json
import os
import re
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from research_pulse.production.normalized import load_normalized_jsonl
from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import CanonicalPaperIR, DeepSeekPaperReadingModel, PaperIRBlock


SOURCE_ID = "2608.18351v1"
TITLE = "Task-Conditioned Least-Privilege Learning for Executable Terminal and MCP Agents"
SOURCE_URL = "https://arxiv.org/abs/2608.18351v1"
NORMALIZED = ROOT / "tmp" / "reading-experiment-root" / SOURCE_ID / "normalized" / "blocks.jsonl"
IMAGE_ROOT = ROOT / "tmp" / "reading-experiment-root" / SOURCE_ID
OUTPUT = ROOT / "evals" / "real-e2e" / "human-like-emergent-questions-v2"
V7_NOTE = ROOT / "evals" / "real-e2e" / "human-like-experiment-v7" / "reading-note.md"
GOLDEN_NOTE = ROOT / "evals" / "real-e2e" / "2608.18351v1-golden-human-note.md"
MAX_ROUNDS = 6
MAX_SELECTED_BLOCKS = 8


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
    started = datetime.now(timezone.utc).isoformat()

    candidate = PaperCandidate(SOURCE_ID, TITLE, SOURCE_URL, "security")
    normalized = load_normalized_jsonl(NORMALIZED)
    paper = CanonicalPaperIR.from_normalized(
        source_id=SOURCE_ID,
        title=TITLE,
        source_url=SOURCE_URL,
        blocks=normalized,
    )
    paper = replace(
        paper,
        blocks=tuple(
            replace(block, image_path=str(resolved))
            if block.image_path and (resolved := resolve_image(block.image_path))
            else block
            for block in paper.blocks
        ),
    )
    model = DeepSeekPaperReadingModel.from_environment()
    write_json("provider-probe.json", model.probe())

    catalog = build_catalog(paper)
    orientation = response_body(call_json(
        model,
        "emergent_orientation",
        {
            "task": "快速浏览论文，只建立导航和阅读入口，不假装已经理解全文。",
            "rules": [
                "只能提出 1-2 个入口问题；它们应帮助开始阅读，不能预先列出完整问题清单。",
                "candidate_thesis 只是作者声称的暂定主线，不是已经验证的结论。",
                "问题用通俗中文，优先追问论文为什么存在、它声称解决的核心矛盾。",
            ],
            "title": TITLE,
            "navigation_material": navigation_material(paper),
            "return": {
                "paper_navigation": "章节及其可能作用",
                "candidate_thesis": "暂定主线",
                "entry_questions": [
                    {"text": "问题", "priority": "high", "rationale": "为什么现在值得读"}
                ],
            },
        },
    ), "candidate_thesis", "entry_questions")
    questions = normalize_entry_questions(orientation)
    question_history: list[dict[str, Any]] = [snapshot_questions(0, "global_skim", questions)]
    records: list[dict[str, Any]] = []
    selections: list[dict[str, Any]] = []
    argument_state: dict[str, Any] = {
        "version": 0,
        "candidate_thesis": orientation.get("candidate_thesis", ""),
        "supported_chain": [],
        "revisions": [],
        "unresolved": [item["text"] for item in questions],
    }

    for round_number in range(1, MAX_ROUNDS + 1):
        active = [item for item in questions if item["status"] in {"candidate", "active", "partial"}]
        if not active:
            break
        selection = response_body(call_json(
            model,
            f"emergent_select_{round_number}",
            {
                "task": "选择这一轮最值得解决的一个问题，并从论文目录中选择联合阅读材料。",
                "rules": [
                    "不要按章节顺序机械阅读；可以联合选择不连续的引言、方法、公式、图表和实验。",
                    "最多选择 8 个 block，必须返回目录中存在的精确 block_id。",
                    "优先解决能改变整篇论文理解的问题，而不是补无关细节。",
                    "只有真实像素对当前问题不可替代时，才把图片 block 放入 inspect_visual_ids。",
                ],
                "current_questions": active,
                "current_argument_state": argument_state,
                "previous_records": compact_records(records),
                "paper_catalog": catalog,
                "return": {
                    "question_id": "当前问题 ID",
                    "reading_target": "这一轮具体要验证的关系",
                    "success_condition": "什么证据出现才算回答",
                    "selected_block_ids": ["精确 block_id"],
                    "selection_reasons": {"block_id": "为何需要"},
                    "inspect_visual_ids": ["仅必要图片"],
                },
            },
        ), "selected_block_ids", "question_id")
        selection = validate_selection(selection, active, paper)
        selections.append(selection)
        selected_blocks = [paper.block_by_id[item] for item in selection["selected_block_ids"]]

        visual_context: list[dict[str, str]] = []
        for block_id in selection["inspect_visual_ids"][:1]:
            block = paper.block_by_id[block_id]
            if not block.image_path or not Path(block.image_path).is_file():
                continue
            interpretation = response_body(call_json(
                model,
                f"emergent_visual_{round_number}",
                {
                    "task": "只解释这张图如何帮助回答当前阅读目标；不要补造图中没有的信息。",
                    "reading_target": selection["reading_target"],
                    "block_id": block.block_id,
                    "caption": block.caption or block.text,
                    "neighboring_context": neighboring_context(block, paper),
                    "return": {"interpretation": "图对机制或结果的解释", "remaining_uncertainty": "仍看不出的内容"},
                },
                image=block.image_path,
            ), "interpretation")
            visual_context.append({
                "block_id": block.block_id,
                "interpretation": str(interpretation.get("interpretation", "")),
                "remaining_uncertainty": str(interpretation.get("remaining_uncertainty", "")),
            })

        raw_record = call_json(
            model,
            f"emergent_read_{round_number}",
            {
                "task": "联合阅读这些材料，回答当前问题，并指出由阅读真正产生的下一步问题。",
                "rules": [
                    "先解释材料之间的关系，不要把各 block 分别摘要后拼接。",
                    "source_facts 必须是原子事实并引用本轮提供的精确 block_id；精确数字不得改写或取整。",
                    "material_unknown 只表示本轮材料未覆盖，不能写成论文缺陷。",
                    "source_limitation 只有作者明确写出的局限才可使用。",
                    "新问题必须由本轮发现触发，说明 trigger；最多 2 个，不要重复已有问题。",
                    "如果当前问题已回答，answer_status=answered；部分回答则 partial；材料不对则 unresolved。",
                ],
                "question": next(item for item in questions if item["id"] == selection["question_id"]),
                "reading_target": selection["reading_target"],
                "success_condition": selection["success_condition"],
                "blocks": [full_block(block) for block in selected_blocks],
                "visual_context": visual_context,
                "return": {
                    "answer_status": "answered|partial|unresolved",
                    "plain_answer": "通俗中文回答",
                    "source_facts": [{"statement": "事实", "facet": "problem|method|experiment|limitation", "source_block_ids": ["id"]}],
                    "relationships": [{"from": "概念", "relation": "关系", "to": "概念", "source_block_ids": ["id"]}],
                    "source_limitations": [{"statement": "作者明确限制", "source_block_ids": ["id"]}],
                    "material_unknowns": ["当前材料还不知道什么"],
                    "new_questions": [{"text": "由本轮阅读产生的问题", "priority": "high|medium|low", "trigger": "哪项发现触发"}],
                    "argument_update": {"supported_chain_additions": ["论证链新增关系"], "revisions": ["对暂定理解的修订"]},
                },
            },
        )
        write_json(f"raw-read-{round_number}.json", raw_record)
        record = validate_record(response_body(raw_record, "answer_status", "source_facts"), selection, selected_blocks, round_number)
        records.append(record)
        update_question_state(questions, selection["question_id"], record["answer_status"])
        add_emergent_questions(questions, record.get("new_questions", ()), round_number)
        argument_state = update_argument_state(argument_state, record, questions, round_number)
        question_history.append(snapshot_questions(round_number, "after_reading", questions))

        if coverage_ready(records, questions):
            break

    final_note = response_body(call_json(
        model,
        "emergent_final_note",
        {
            "task": "根据已经形成的阅读状态，写一份让没读过论文的人也能读懂的中文精读笔记。",
            "rules": [
                "重点讲清背景、具体问题、既有方法为什么不够、核心机制如何回应、关键实验如何验证、结论边界。",
                "不要按阅读轮次或 block 顺序写，不要出现 block ID、ReadingRecord 等内部术语。",
                "保留 source_facts 中关键模型名、机制名、公式和精确实验数字，不得取整或补造。",
                "material_unknown 不能被写成论文缺陷；只把 source_limitations 写成作者承认的限制。",
                "语言自然、信息密度高，避免重复标题和证据摘录腔。",
            ],
            "title": TITLE,
            "argument_state": argument_state,
            "reading_records": records,
            "questions": questions,
            "return": {"markdown": "完整 Markdown 笔记"},
        },
    ), "markdown")
    markdown = str(final_note.get("markdown", "")).strip() + "\n"
    (OUTPUT / "reading-note.md").write_text(markdown, encoding="utf-8")
    write_json("orientation.json", orientation)
    write_json("question-history.json", question_history)
    write_json("selections.json", selections)
    write_json("reading-records.json", records)
    write_json("argument-state.json", argument_state)
    comparison = compare_notes(markdown, records, question_history)
    write_json("comparison.json", comparison)
    (OUTPUT / "comparison.md").write_text(comparison_markdown(comparison), encoding="utf-8")
    (OUTPUT / "trace-review.html").write_text(trace_html(orientation, question_history, selections, records, argument_state), encoding="utf-8")
    write_json(
        "run-meta.json",
        {
            "started_at": started,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "source_id": SOURCE_ID,
            "rounds": len(records),
            "questions_total": len(questions),
            "questions_from_skim": sum(item["origin"] == "skim" for item in questions),
            "questions_emerged_during_reading": sum(item["origin"] != "skim" for item in questions),
            "text_calls": model.text_call_count,
            "vision_calls": model.vision_call_count,
            "resolved_text_model": model.resolved_text_model,
            "resolved_vision_model": model.resolved_vision_model,
            "fallbacks": model.fallbacks,
            "stop_reason": "coverage" if coverage_ready(records, questions) else "budget",
        },
    )
    print(json.dumps({"output": str(OUTPUT), "rounds": len(records), "questions": len(questions), "comparison": comparison}, ensure_ascii=False, indent=2))
    return 0


def call_json(model: DeepSeekPaperReadingModel, operation: str, payload: Mapping[str, Any], image: str | None = None) -> dict[str, Any]:
    return dict(model._json_call(operation, model.vision_model if image else model.text_model, json.dumps(payload, ensure_ascii=False), image=image))


def response_body(response: Mapping[str, Any], *expected_keys: str) -> dict[str, Any]:
    """Accept both direct JSON results and providers that fill the prompt's return object."""

    if any(key in response for key in expected_keys):
        return dict(response)
    nested = response.get("return")
    if isinstance(nested, Mapping) and any(key in nested for key in expected_keys):
        return dict(nested)
    return dict(response)


def navigation_material(paper: CanonicalPaperIR) -> list[dict[str, Any]]:
    blocks = list(paper.navigation_blocks())
    headings = [block for block in paper.ordered_blocks if len(block.text) < 120 and block.kind in {"heading", "paragraph"}]
    unique = {block.block_id: block for block in (*blocks, *headings[:20])}
    return [catalog_item(block, excerpt_chars=900) for block in sorted(unique.values(), key=lambda item: item.order)[:28]]


def build_catalog(paper: CanonicalPaperIR) -> list[dict[str, Any]]:
    return [catalog_item(block, excerpt_chars=420) for block in paper.ordered_blocks if useful_block(block)]


def useful_block(block: PaperIRBlock) -> bool:
    text = re.sub(r"\s+", " ", block.text).strip()
    return block.kind in {"figure", "table", "formula"} or len(text) >= 45


def catalog_item(block: PaperIRBlock, excerpt_chars: int) -> dict[str, Any]:
    content = block.latex or block.table_html or block.caption or block.text
    content = re.sub(r"\s+", " ", content).strip()
    return {
        "block_id": block.block_id,
        "order": block.order,
        "section": block.section,
        "kind": block.kind,
        "excerpt": content[:excerpt_chars],
        "has_real_image": bool(block.image_path and Path(block.image_path).is_file()),
    }


def full_block(block: PaperIRBlock) -> dict[str, Any]:
    return {
        "block_id": block.block_id,
        "order": block.order,
        "section": block.section,
        "kind": block.kind,
        "text": block.text[:8000],
        "caption": block.caption,
        "latex": block.latex,
        "table_html": block.table_html[:12000] if block.table_html else None,
    }


def normalize_entry_questions(orientation: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = orientation.get("entry_questions", ())
    result = []
    for index, item in enumerate(raw[:2] if isinstance(raw, list) else ()):
        if not isinstance(item, Mapping) or not str(item.get("text", "")).strip():
            continue
        result.append({
            "id": f"q{index + 1}",
            "text": str(item["text"]).strip(),
            "priority": str(item.get("priority", "high")),
            "status": "active" if index == 0 else "candidate",
            "origin": "skim",
            "rationale": str(item.get("rationale", "阅读入口")),
        })
    if not result:
        result = [{"id": "q1", "text": "作者为什么认为这项工作有必要？", "priority": "high", "status": "active", "origin": "skim", "rationale": "默认入口"}]
    return result


def validate_selection(selection: Mapping[str, Any], active: list[dict[str, Any]], paper: CanonicalPaperIR) -> dict[str, Any]:
    active_ids = {item["id"] for item in active}
    question_id = str(selection.get("question_id", ""))
    if question_id not in active_ids:
        question_id = active[0]["id"]
    valid_ids = []
    for block_id in selection.get("selected_block_ids", ()):
        block_id = str(block_id)
        if block_id in paper.block_by_id and block_id not in valid_ids and useful_block(paper.block_by_id[block_id]):
            valid_ids.append(block_id)
    if not valid_ids:
        valid_ids = [block.block_id for block in paper.navigation_blocks()[:4]]
    visual_ids = [
        str(item) for item in selection.get("inspect_visual_ids", ())
        if str(item) in valid_ids and paper.block_by_id[str(item)].is_visual
    ]
    return {
        "question_id": question_id,
        "reading_target": str(selection.get("reading_target", next(item["text"] for item in active if item["id"] == question_id))),
        "success_condition": str(selection.get("success_condition", "找到能够直接回答问题的来源事实和关系。")),
        "selected_block_ids": valid_ids[:MAX_SELECTED_BLOCKS],
        "selection_reasons": dict(selection.get("selection_reasons", {})) if isinstance(selection.get("selection_reasons"), Mapping) else {},
        "inspect_visual_ids": visual_ids,
    }


def validate_record(record: Mapping[str, Any], selection: Mapping[str, Any], blocks: list[PaperIRBlock], round_number: int) -> dict[str, Any]:
    valid_ids = {block.block_id for block in blocks}
    facts = []
    for item in record.get("source_facts", ()):
        if not isinstance(item, Mapping) or not str(item.get("statement", "")).strip():
            continue
        ids = [str(value) for value in item.get("source_block_ids", ()) if str(value) in valid_ids]
        if ids:
            facts.append({"statement": str(item["statement"]).strip(), "facet": str(item.get("facet", "problem")), "source_block_ids": ids})
    relationships = []
    for item in record.get("relationships", ()):
        if not isinstance(item, Mapping):
            continue
        ids = [str(value) for value in item.get("source_block_ids", ()) if str(value) in valid_ids]
        if ids and item.get("from") and item.get("relation") and item.get("to"):
            relationships.append({"from": str(item["from"]), "relation": str(item["relation"]), "to": str(item["to"]), "source_block_ids": ids})
    limitations = []
    for item in record.get("source_limitations", ()):
        if isinstance(item, Mapping):
            ids = [str(value) for value in item.get("source_block_ids", ()) if str(value) in valid_ids]
            if ids and item.get("statement"):
                limitations.append({"statement": str(item["statement"]), "source_block_ids": ids})
    status = str(record.get("answer_status", "unresolved"))
    if status not in {"answered", "partial", "unresolved"}:
        status = "partial"
    if status == "answered" and not (facts or relationships):
        status = "partial"
    return {
        "round": round_number,
        "question_id": selection["question_id"],
        "reading_target": selection["reading_target"],
        "answer_status": status,
        "plain_answer": str(record.get("plain_answer", "")),
        "source_facts": facts,
        "relationships": relationships,
        "source_limitations": limitations,
        "material_unknowns": [str(item) for item in record.get("material_unknowns", ()) if str(item).strip()],
        "new_questions": [dict(item) for item in record.get("new_questions", ())[:2] if isinstance(item, Mapping) and str(item.get("text", "")).strip()],
        "argument_update": dict(record.get("argument_update", {})) if isinstance(record.get("argument_update"), Mapping) else {},
        "source_block_ids": selection["selected_block_ids"],
    }


def update_question_state(questions: list[dict[str, Any]], question_id: str, answer_status: str) -> None:
    mapped = {"answered": "answered", "partial": "partial", "unresolved": "active"}[answer_status]
    for item in questions:
        if item["id"] == question_id:
            item["status"] = mapped
            return


def add_emergent_questions(questions: list[dict[str, Any]], new_questions: Any, round_number: int) -> None:
    existing = [normalize_text(item["text"]) for item in questions]
    for item in new_questions:
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("text", "")).strip()
        if not text or any(similar_question(normalize_text(text), old) for old in existing):
            continue
        questions.append({
            "id": f"q{len(questions) + 1}",
            "text": text,
            "priority": str(item.get("priority", "medium")),
            "status": "candidate",
            "origin": f"round-{round_number}",
            "rationale": str(item.get("trigger", "由本轮阅读产生")),
        })
        existing.append(normalize_text(text))


def update_argument_state(state: Mapping[str, Any], record: Mapping[str, Any], questions: list[dict[str, Any]], round_number: int) -> dict[str, Any]:
    update = record.get("argument_update", {})
    additions = [str(item) for item in update.get("supported_chain_additions", ()) if str(item).strip()] if isinstance(update, Mapping) else []
    revisions = [str(item) for item in update.get("revisions", ()) if str(item).strip()] if isinstance(update, Mapping) else []
    return {
        "version": round_number,
        "candidate_thesis": state.get("candidate_thesis", ""),
        "supported_chain": list(dict.fromkeys([*state.get("supported_chain", ()), *additions])),
        "revisions": [*state.get("revisions", ()), *({"round": round_number, "statement": item} for item in revisions)],
        "unresolved": [item["text"] for item in questions if item["status"] != "answered"],
    }


def coverage_ready(records: list[dict[str, Any]], questions: list[dict[str, Any]]) -> bool:
    facets = {fact["facet"] for record in records for fact in record["source_facts"]}
    high_open = any(item["priority"] == "high" and item["status"] != "answered" for item in questions)
    has_chain = sum(bool(record["relationships"]) for record in records) >= 2
    return {"problem", "method", "experiment", "limitation"}.issubset(facets) and has_chain and not high_open


def compact_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "round": item["round"],
        "question_id": item["question_id"],
        "answer_status": item["answer_status"],
        "plain_answer": item["plain_answer"][:900],
        "material_unknowns": item["material_unknowns"],
        "new_questions": item["new_questions"],
    } for item in records]


def snapshot_questions(round_number: int, event: str, questions: list[dict[str, Any]]) -> dict[str, Any]:
    return {"round": round_number, "event": event, "questions": [dict(item) for item in questions]}


def normalize_text(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value.casefold())


def similar_question(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    left_pairs = {left[index:index + 2] for index in range(max(0, len(left) - 1))}
    right_pairs = {right[index:index + 2] for index in range(max(0, len(right) - 1))}
    return bool(left_pairs and right_pairs and len(left_pairs & right_pairs) / len(left_pairs | right_pairs) >= 0.62)


def neighboring_context(block: PaperIRBlock, paper: CanonicalPaperIR) -> str:
    ordered = list(paper.ordered_blocks)
    index = next(i for i, item in enumerate(ordered) if item.block_id == block.block_id)
    neighbors = ordered[max(0, index - 1): min(len(ordered), index + 2)]
    return "\n".join(item.text[:1200] for item in neighbors)


def resolve_image(image_path: str) -> Path | None:
    candidates = (IMAGE_ROOT / image_path, IMAGE_ROOT / "mineru" / "source" / "auto" / image_path)
    return next((path.resolve() for path in candidates if path.is_file()), None)


def compare_notes(note: str, records: list[dict[str, Any]], history: list[dict[str, Any]]) -> dict[str, Any]:
    v7 = V7_NOTE.read_text(encoding="utf-8") if V7_NOTE.is_file() else ""
    golden = GOLDEN_NOTE.read_text(encoding="utf-8") if GOLDEN_NOTE.is_file() else ""
    concepts = ["Qwen3.5-4B", "broker", "六维", "64.36%", "98.48%", "4.56%", "0.79%", "2,000", "1,500", "300", "200", "权限门禁", "沙箱"]
    facts = [fact for record in records for fact in record["source_facts"]]
    return {
        "note_chars": len(note),
        "v7_chars": len(v7),
        "golden_chars": len(golden),
        "concept_hits": {item: {"new": item.casefold() in note.casefold(), "v7": item.casefold() in v7.casefold(), "golden": item.casefold() in golden.casefold()} for item in concepts},
        "source_fact_facets": sorted({fact["facet"] for fact in facts}),
        "source_fact_count": len(facts),
        "question_snapshots": len(history),
        "emergent_questions": sum(item["origin"] != "skim" for item in history[-1]["questions"]),
        "answered_questions": sum(item["status"] == "answered" for item in history[-1]["questions"]),
        "open_high_priority_questions": [item["text"] for item in history[-1]["questions"] if item["priority"] == "high" and item["status"] != "answered"],
        "raw_block_id_in_note": "normalized:" in note,
    }


def comparison_markdown(value: Mapping[str, Any]) -> str:
    lines = [
        "# 动态问题阅读实验对比",
        "",
        f"- 新笔记字符数：{value['note_chars']}",
        f"- v7 笔记字符数：{value['v7_chars']}",
        f"- 黄金笔记字符数：{value['golden_chars']}",
        f"- 阅读中产生的新问题：{value['emergent_questions']}",
        f"- 已回答问题：{value['answered_questions']}",
        f"- source facts：{value['source_fact_count']}，facets={value['source_fact_facets']}",
        f"- 正文出现 raw block ID：{value['raw_block_id_in_note']}",
        "",
        "## 关键内容命中",
        "",
    ]
    for concept, hits in value["concept_hits"].items():
        lines.append(f"- {concept}: 新={hits['new']}；v7={hits['v7']}；黄金={hits['golden']}")
    lines.extend(["", "## 尚未回答的高优先级问题", ""])
    lines.extend(f"- {item}" for item in value["open_high_priority_questions"] or ["无"])
    return "\n".join(lines) + "\n"


def trace_html(orientation: Mapping[str, Any], history: list[dict[str, Any]], selections: list[dict[str, Any]], records: list[dict[str, Any]], argument_state: Mapping[str, Any]) -> str:
    payload = json.dumps({"orientation": orientation, "history": history, "selections": selections, "records": records, "argument": argument_state}, ensure_ascii=False)
    escaped = html.escape(payload)
    return f"""<!doctype html><meta charset='utf-8'><title>动态问题阅读轨迹</title>
<style>body{{font:15px/1.65 system-ui;margin:40px auto;max-width:1100px;color:#17202a}}h1{{color:#145a73}}pre{{white-space:pre-wrap;background:#f4f7f8;padding:20px;border-radius:10px}}.note{{background:#eaf5f7;padding:16px;border-left:4px solid #168aad}}</style>
<h1>动态问题阅读轨迹（一次性验证原型）</h1>
<p class='note'>验证问题：快速浏览只给出 1–2 个入口问题后，后续问题能否由实际阅读发现自然产生，并推动跨章节补读？本页只展示状态，不属于生产功能。</p>
<pre id='state'>{escaped}</pre>"""


def write_json(name: str, value: Any) -> None:
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
