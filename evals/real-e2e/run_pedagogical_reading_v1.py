from __future__ import annotations

"""PROTOTYPE: teaching-style paper note with an explicit experiment map."""

from datetime import datetime, timezone
from html import escape, unescape
from pathlib import Path
from typing import Any, Iterable, Mapping
import base64
import hashlib
import importlib.util
import json
import re
import shutil
import sys

import markdown


ROOT = Path(__file__).resolve().parents[2]
BASE_SCRIPT = ROOT / "evals" / "real-e2e" / "run_integrated_b_deepening_v1.py"
OUTPUT = ROOT / "evals" / "real-e2e" / "pedagogical-reading-v1"
FULL_MODEL = ROOT / "evals" / "real-e2e" / "full-paper-b-production-v5"
READING_STATE = ROOT / "evals" / "real-e2e" / "integrated-b-deepening-v1"
PREVIOUS = ROOT / "evals" / "real-e2e" / "integrated-b-deepening-v4"


def _load_base() -> Any:
    spec = importlib.util.spec_from_file_location("integrated_reading_base", BASE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the integrated experiment helpers.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.OUTPUT = OUTPUT
    return module


base = _load_base()


REQUIRED_EXPERIMENTS = [
    {"key": "main_held_out", "question": "后训练能否同时提高任务成功与安全成功？", "anchors": ["normalized:2608.18351v1:table:867444f70fd8", "normalized:2608.18351v1:text:0dd1c9eb88d8"]},
    {"key": "training_progress", "question": "收益在训练的哪个阶段出现，继续训练还带来什么？", "anchors": ["normalized:2608.18351v1:table:90f635985e69", "normalized:2608.18351v1:text:222a8776bb85"]},
    {"key": "family_holdout", "question": "在同一 broker 约定下，未见任务族是否仍能泛化？", "anchors": ["normalized:2608.18351v1:text:dfc47506459a"]},
    {"key": "prompt_ablation", "question": "行为是训练内化的，还是依赖完整安全提示词？", "anchors": ["normalized:2608.18351v1:text:1bf0d063dc1d", "normalized:2608.18351v1:text:1ffc11a2ee4b", "normalized:2608.18351v1:text:11a655dd021d"]},
    {"key": "external_benchmarks", "question": "离开内部任务分布后，能力保留和权限选择如何？", "anchors": ["normalized:2608.18351v1:table:7c19dad5859b", "normalized:2608.18351v1:text:d7e90e43f60b"]},
    {"key": "continuation", "question": "增加新任务族后能否改善目标行为并保留旧能力？", "anchors": ["normalized:2608.18351v1:table:e6156094ff03", "normalized:2608.18351v1:text:c8d814dfbbd0"]},
    {"key": "stability_and_boundary", "question": "跨 seed、陌生接口和失败升级任务上的边界是什么？", "anchors": ["normalized:2608.18351v1:text:d079a8837ec6", "normalized:2608.18351v1:text:7d25afb1fc29", "normalized:2608.18351v1:text:ff80b1cc256c"]},
]


def main() -> int:
    base._load_dotenv()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    paper = base._load_paper(base.PaperCandidate(base.SOURCE_ID, base.TITLE, "https://arxiv.org/abs/2608.18351v1", "security"))
    paper_model = _read_json(FULL_MODEL / "paper-model.json")
    records = _read_json(READING_STATE / "deepening-records.json")
    visual_results = _read_json(READING_STATE / "visual-results.json")
    model = base.DeepSeekPaperReadingModel.from_environment()

    source_blocks = _teaching_source_blocks(paper)
    source_ids = {item["block_id"] for item in source_blocks}
    experiment_raw = base._call_json(model, "pedagogical-experiment-map", {
        "operation": "build_complete_experiment_map_for_teaching_note",
        "task": "Reconstruct what experiments the paper ran so a new reader understands setup, comparison, results, and meaning before any prose is written.",
        "required_experiments": REQUIRED_EXPERIMENTS,
        "rules": [
            "Return exactly one entry for every required experiment key.",
            "For each entry explain why it was run, setup and denominator, comparison, exact results, interpretation, and what it cannot establish.",
            "Every factual field must cite exact supplied block_ids; preserve every reported number exactly.",
            "Do not turn a missing ablation or missing error breakdown into an experiment result. Record it under missing_evidence.",
            "Distinguish the selected Seed 1 result under the same broker conventions from cross-interface and cross-seed robustness.",
        ],
        "blocks": source_blocks,
        "return": {
            "global_setup": [{"statement": "atomic setup fact", "source_block_ids": ["exact id"]}],
            "experiments": [{
                "key": "required key", "question": "plain question", "why": "reason",
                "setup": ["details"], "comparison": ["groups"], "results": ["exact findings"],
                "interpretation": "what it supports", "boundary": "what it does not support",
                "source_block_ids": ["exact id"],
            }],
            "missing_evidence": [{"question": "important unanswered issue", "reason": "not reported", "source_block_ids": ["limitation id when available"]}],
        },
    })
    experiment_map = _validate_experiment_map(experiment_raw, source_ids)
    _write_json("experiment-map.json", experiment_map)

    plan_raw = base._call_json(model, "pedagogical-note-plan", {
        "operation": "plan_a_teaching_style_chinese_paper_reading_note",
        "reader": "A technical reader who has not read the paper and does not already know agent privilege alignment or GRPO.",
        "goal": "Make the paper understandable without turning it into either a terse outline or a near-translation.",
        "required_order": [
            "先用通俗例子说明越权问题", "补充最小权限、权限门控与任务相对权限的必要背景",
            "用同一个例子逐步引出风险向量、充分权限包络、超额权限、broker 与奖励",
            "完整交代训练与评估设置", "按 ExperimentMap 逐组解释为什么做、怎么做、结果和含义",
            "综合判断论文证明了什么", "最后说明局限和仍未回答的问题",
        ],
        "asset_contract": {
            "method": ["{{asset:figure-1}}"],
            "experiment_setup": ["{{asset:table-task-split}}"],
            "main_results": ["{{asset:table-main-results}}"],
            "training_progress": ["{{asset:table-checkpoints}}"],
        },
        "paper_overview": _paper_overview(paper_model),
        "deepening_records": records,
        "experiment_map": experiment_map,
        "return": {
            "running_example": "one source-supported example reused across method explanation",
            "prerequisites": [{"concept": "name", "plain_explanation": "only what the reader needs"}],
            "sections": [{"heading": "Chinese heading", "reader_question": "what this section answers", "teaching_moves": ["ordered moves"], "asset_markers": ["marker"]}],
            "transition_strategy": ["how adjacent sections connect"],
        },
    })
    teaching_plan = base._response_body(plan_raw, "sections", "running_example")
    _write_json("teaching-plan.json", teaching_plan)

    writer_raw = base._call_json(model, "pedagogical-long-writer", {
        "operation": "write_grounded_pedagogical_chinese_paper_note",
        "task": "Write a fluent teaching-style Chinese deep-reading note. A reader should understand the prerequisites, method logic, full experiment setup, each experiment, and the conclusion without opening the paper.",
        "length_guidance": "Usually 5,500-8,500 Chinese characters; this is guidance, not a compression target.",
        "rules": [
            "Write connected explanatory prose, not a dense checklist. Before introducing a formula or module, explain what problem makes it necessary.",
            "Use the running example repeatedly so risk vector, envelope, excess authority, broker audits, and reward form one causal story.",
            "Explain only prerequisite knowledge needed for this paper; do not write a generic security or reinforcement-learning textbook.",
            "Create a distinct training-and-evaluation-setup section before reporting results.",
            "Cover every ExperimentMap entry. For every experiment state why it was run, setup/comparison, exact result, and interpretation.",
            "Do not hide negative results: selected-seed dependence, unfamiliar-interface weakness, and missing ablations belong near the relevant experiment.",
            "Use exact source numbers. State that 4.56% is 132 of all 2,896 evaluation episodes that succeeded with excess authority, not a per-action rate.",
            "Explain E as evidence existence and sufficiency, never efficiency.",
            "Insert all four asset markers at the planned semantic locations. Do not reproduce the table cells yourself.",
            "Do not expose block ids, raw evidence excerpts, model-provider details, or internal pipeline state.",
            "Return JSON with markdown and used_experiment_keys.",
        ],
        "paper_overview": _paper_overview(paper_model),
        "deepening_records": records,
        "experiment_map": experiment_map,
        "teaching_plan": teaching_plan,
        "visual_results": list(visual_results.values()),
        "source_material": source_blocks,
    })
    draft = base._response_body(writer_raw, "markdown")
    note = str(draft.get("markdown", "")).strip()
    if not note:
        raise RuntimeError("Pedagogical writer returned no note.")
    issues = _validate_note(note, paper, experiment_map, visual_results)
    repair_calls = 0
    if issues:
        repair_calls = 1
        repair_raw = base._call_json(model, "pedagogical-local-repair", {
            "operation": "repair_teaching_note_without_compressing_it",
            "draft": note,
            "issues": issues,
            "experiment_map": experiment_map,
            "teaching_plan": teaching_plan,
            "source_material": source_blocks,
            "rules": [
                "Repair every listed issue locally while preserving explanations, transitions, exact numbers, and all correct content.",
                "Do not shorten the note to solve an issue.",
                "Keep all four asset markers and return JSON with markdown only.",
            ],
        })
        repaired = str(base._response_body(repair_raw, "markdown").get("markdown", "")).strip()
        if repaired:
            note = repaired
            issues = _validate_note(note, paper, experiment_map, visual_results)

    asset_receipt = _publish_figure_asset(visual_results)
    rendered = _render_assets(note, paper)
    (OUTPUT / "reading-note.md").write_text(rendered + "\n", encoding="utf-8")
    html_check = _write_review_html(rendered, experiment_map, teaching_plan, issues, asset_receipt)
    comparison = _compare(rendered, experiment_map)
    receipt = {
        "route": "full_paper_schema_plus_experiment_map_plus_pedagogical_writer",
        "status": "completed" if not issues and html_check["passed"] else "needs_review",
        "reused_full_paper_model": str(FULL_MODEL / "paper-model.json"),
        "reused_deepening_state": str(READING_STATE),
        "model_calls": {"experiment_map": 1, "teaching_plan": 1, "writer": 1, "repair": repair_calls, "text_total": model.text_call_count, "vision_total": model.vision_call_count},
        "visual_input": base._visual_receipt(visual_results),
        "published_asset": asset_receipt,
        "html_check": html_check,
        "validation_issues": issues,
        "comparison": comparison,
        "started_at": started,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json("reading-receipt.json", receipt)
    _write_json("final-validation.json", {"issues": issues, "html_check": html_check})
    print(json.dumps({"output": str(OUTPUT), "receipt": receipt}, ensure_ascii=False, indent=2))
    return 0


def _teaching_source_blocks(paper: Any) -> list[dict[str, Any]]:
    wanted = (
        "INTRODUCTION", "Task Relative Authority", "Brokered Execution", "Modular Framework",
        "Reward and Training", "Tasks and Evaluation", "Internal Results", "Prompt Ablation",
        "External Evaluation", "Prompt and Policy", "Continuation Study", "Interpretation and Discussion",
        "LIMITATIONS AND FUTURE WORK", "CONCLUSION",
    )
    result = []
    for block in paper.ordered_blocks:
        section = block.section
        if any(value.casefold() in section.casefold() for value in wanted):
            result.append(base._full_block(block))
    return result


def _paper_overview(model: Mapping[str, Any]) -> dict[str, Any]:
    return {key: model.get(key) for key in ("paper_type", "thesis", "research_question", "conclusion_scope", "source_facts", "source_limitations", "agent_syntheses") if key in model}


def _validate_experiment_map(raw: Mapping[str, Any], allowed_ids: set[str]) -> dict[str, Any]:
    body = base._response_body(raw, "experiments", "global_setup")
    experiments = []
    for item in body.get("experiments", ()) if isinstance(body.get("experiments"), list) else ():
        if not isinstance(item, Mapping):
            continue
        ids = list(dict.fromkeys(str(value) for value in item.get("source_block_ids", ()) if str(value) in allowed_ids))
        key = str(item.get("key", ""))
        if key and ids:
            experiments.append({**dict(item), "source_block_ids": ids})
    by_key = {str(item["key"]): item for item in experiments}
    missing = [item["key"] for item in REQUIRED_EXPERIMENTS if item["key"] not in by_key]
    if missing:
        raise RuntimeError(f"ExperimentMap missed required entries: {missing}")
    return {
        "global_setup": body.get("global_setup", ()),
        "experiments": [by_key[item["key"]] for item in REQUIRED_EXPERIMENTS],
        "missing_evidence": body.get("missing_evidence", ()),
    }


def _validate_note(note: str, paper: Any, experiment_map: Mapping[str, Any], visuals: Mapping[str, Mapping[str, Any]]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if len(note) < 5000:
        issues.append({"type": "too_compressed", "detail": str(len(note))})
    required = {
        "background": ("背景", "前提"), "running_example": ("bug", "补丁"), "envelope": ("充分权限包络",),
        "broker": ("broker",), "training_setup": ("训练", "评估设置"), "task_catalog": ("2,000", "2000"),
        "train_tasks": ("1,500", "1500"), "within": ("300",), "excluded": ("200",), "episodes": ("2,896", "2896"),
        "three_seeds": ("三个", "3个", "3 个"), "prompt_ablation": ("提示词消融", "Prompt 消融", "prompt 消融"),
        "external": ("MetaTool",), "fortis": ("FORTIS",), "toolpriv": ("ToolPrivBench",), "continuation": ("延续", "continuation"),
        "boundary": ("不能替代",),
    }
    folded = note.casefold()
    for name, alternatives in required.items():
        if not any(value.casefold() in folded for value in alternatives):
            issues.append({"type": "missing_teaching_content", "detail": name})
    for marker in ("{{asset:figure-1}}", "{{asset:table-task-split}}", "{{asset:table-main-results}}", "{{asset:table-checkpoints}}"):
        if marker not in note:
            issues.append({"type": "missing_asset_marker", "detail": marker})
    if re.search(r"E\s*[^。；\n]{0,20}(?:效率|efficiency)", note, flags=re.I):
        issues.append({"type": "wrong_formula_meaning", "detail": "E"})
    if "normalized:" in note:
        issues.append({"type": "raw_block_id", "detail": "normalized:"})
    if any(item.get("status") == "success" for item in visuals.values()):
        for term in ("遥测", "验证器", "训练更新"):
            if term not in note:
                issues.append({"type": "missing_visual_explanation", "detail": term})
    numeric_source = " ".join(filter(None, (block.text or block.latex or block.table_html for block in paper.ordered_blocks)))
    allowed = {base._normalize_number(value) for value in base._numeric_tokens(base._replace_number_words(numeric_source))}
    numeric_note = re.sub(r"\{\{asset:[^}]+\}\}", "", note)
    numeric_note = re.sub(r"(?m)^\s*\d+\.(?=\s)", "", numeric_note)
    for value in dict.fromkeys(base._numeric_tokens(numeric_note)):
        if base._normalize_number(value) not in allowed:
            issues.append({"type": "unsupported_number", "detail": value})
    used_keys = {str(item.get("key")) for item in experiment_map.get("experiments", ())}
    if used_keys != {item["key"] for item in REQUIRED_EXPERIMENTS}:
        issues.append({"type": "experiment_map_incomplete", "detail": ",".join(sorted(used_keys))})
    return issues


def _publish_figure_asset(visuals: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    selected = next((item for item in visuals.values() if item.get("status") == "success" and item.get("image_path")), None)
    if not selected:
        return {"published": False, "reason": "no_successful_visual"}
    source = Path(str(selected["image_path"]))
    asset_dir = OUTPUT / "assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    target = asset_dir / "figure-1.jpg"
    shutil.copy2(source, target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return {"published": True, "relative_path": "assets/figure-1.jpg", "exists": target.is_file(), "bytes": target.stat().st_size, "sha256": digest, "source_block_id": selected.get("block_id")}


def _render_assets(note: str, paper: Any) -> str:
    figure = "\n\n![图1：broker 强化学习闭环](assets/figure-1.jpg)\n\n*图1：策略提出动作，broker 负责解析与执行前审计；终端或 MCP 服务执行后，遥测与验证器形成确定性奖励并用于训练更新。*\n\n"
    note = note.replace("{{asset:figure-1}}", figure)
    tables = {
        "{{asset:table-task-split}}": ("normalized:2608.18351v1:table:5665e95f40fd", "表 I：任务目录构成"),
        "{{asset:table-main-results}}": ("normalized:2608.18351v1:table:867444f70fd8", "表 II：完整 500 任务保留集评估"),
        "{{asset:table-checkpoints}}": ("normalized:2608.18351v1:table:90f635985e69", "表 III：训练进程检查"),
    }
    for marker, (block_id, title) in tables.items():
        block = paper.block_by_id[block_id]
        note = note.replace(marker, f"\n\n**{title}**\n\n{_localized_table(block_id, block.table_html or '')}\n\n")
    return note


def _localized_table(block_id: str, table_html: str) -> str:
    rows = []
    for raw_row in re.findall(r"<tr>(.*?)</tr>", table_html, flags=re.I | re.S):
        rows.append([unescape(re.sub(r"<[^>]+>", "", cell)).strip() for cell in re.findall(r"<td>(.*?)</td>", raw_row, flags=re.I | re.S)])
    translations = {
        "Metric": "指标", "Base": "基础模型", "Seed 1": "Seed 1", "Change": "变化",
        "Task episode success": "任务成功", "Safe success": "安全成功", "Excess-authority success": "成功但越权",
        "Policy": "策略", "Verifier success": "验证器成功", "Over-privilege": "越权",
        "Checkpoint 500": "第 500 步", "Checkpoint 1000": "第 1,000 步", "Checkpoint 1,500": "第 1,500 步",
        "Behavior group": "任务类型", "Train": "训练", "Within Excluded Family": "整族保留",
        "Reading and localization": "读取与定位", "Code, change, and tests": "代码、修改与测试",
        "Recovery and escalation": "恢复与权限升级", "Adversarial restraint": "对抗性克制",
        "MCP and multi-tool": "MCP 与多工具", "Unseen behavior families": "未见任务族", "Total": "合计",
    }
    rows = [[translations.get(cell, cell) for cell in row] for row in rows]
    if block_id.endswith("5665e95f40fd") and rows:
        rows[0] = ["任务类型", "训练", "同族验证", "整族保留"]
    return base._markdown_table(rows)


def _write_review_html(note: str, experiment_map: Mapping[str, Any], plan: Mapping[str, Any], issues: list[dict[str, str]], asset: Mapping[str, Any]) -> dict[str, Any]:
    note_html = markdown.markdown(note, extensions=["tables", "fenced_code"])
    asset_path = OUTPUT / "assets" / "figure-1.jpg"
    if asset_path.is_file():
        data_uri = "data:image/jpeg;base64," + base64.b64encode(asset_path.read_bytes()).decode("ascii")
        note_html = note_html.replace('src="assets/figure-1.jpg"', f'src="{data_uri}"')
    panels = {
        "最终笔记": note_html,
        "实验地图": f"<pre>{escape(json.dumps(experiment_map, ensure_ascii=False, indent=2))}</pre>",
        "教学计划": f"<pre>{escape(json.dumps(plan, ensure_ascii=False, indent=2))}</pre>",
        "验收状态": f"<pre>{escape(json.dumps({'issues': issues, 'asset': asset}, ensure_ascii=False, indent=2))}</pre>",
    }
    buttons = "".join(f'<button data-tab="p{i}" class="{("active" if i == 0 else "")}">{escape(name)}</button>' for i, name in enumerate(panels))
    sections = "".join(f'<section id="p{i}" class="panel {("active" if i == 0 else "")}">{body}</section>' for i, body in enumerate(panels.values()))
    html_value = f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>教学式精读实验</title><style>
body{{font:16px/1.8 system-ui;margin:0;background:#f3f5f8;color:#172033}}main{{max-width:1050px;margin:28px auto;padding:0 22px}}header{{background:#172033;color:white;padding:24px 30px;border-radius:16px}}nav{{display:flex;gap:8px;margin:18px 0;flex-wrap:wrap}}button{{border:1px solid #ccd3df;background:white;padding:9px 15px;border-radius:999px;cursor:pointer}}button.active{{background:#3559d8;color:white;border-color:#3559d8}}.panel{{display:none;background:white;padding:28px 38px;border-radius:16px;box-shadow:0 6px 24px #18243b12}}.panel.active{{display:block}}img{{display:block;max-width:100%;margin:24px auto;border-radius:10px}}table{{border-collapse:collapse;width:100%;margin:20px 0}}th,td{{border:1px solid #d8deea;padding:8px 10px;text-align:left}}pre{{white-space:pre-wrap;background:#f6f8fb;padding:18px;border-radius:10px;overflow:auto}}h2{{margin-top:1.8em}}blockquote{{border-left:4px solid #5574e8;margin-left:0;padding-left:18px;color:#42506a}}
</style></head><body><main><header><h1>教学式论文精读实验</h1><p>验证问题：补充必要前提、贯穿案例和完整实验地图后，笔记是否比压缩摘要更容易读懂？</p></header><nav>{buttons}</nav>{sections}</main><script>document.querySelectorAll('button[data-tab]').forEach(b=>b.onclick=()=>{{document.querySelectorAll('button,.panel').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.tab).classList.add('active')}})</script></body></html>"""
    path = OUTPUT / "trace-review.html"
    path.write_text(html_value, encoding="utf-8")
    check = {"html_exists": path.is_file(), "contains_img": "<img" in note_html, "embedded_image": "data:image/jpeg;base64," in html_value, "published_asset_exists": asset_path.is_file(), "note_uses_relative_asset": "assets/figure-1.jpg" in note, "note_has_no_tmp_asset_path": "tmp/reading-experiment-root" not in note}
    check["passed"] = all(check.values())
    return check


def _compare(note: str, experiment_map: Mapping[str, Any]) -> dict[str, Any]:
    previous = (PREVIOUS / "reading-note.md").read_text(encoding="utf-8")
    experiment_terms = ("2,000", "1,500", "300", "200", "2,896", "Seed", "Prompt", "MetaTool", "FORTIS", "ToolPrivBench", "延续")
    return {
        "characters": {"pedagogical": len(note), "integrated_v4": len(previous)},
        "headings": re.findall(r"^#{1,3}\s+(.+)$", note, flags=re.M),
        "experiment_term_hits": {term: term.casefold() in note.casefold() for term in experiment_terms},
        "experiment_map_entries": len(experiment_map.get("experiments", ())),
    }


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(name: str, value: Any) -> None:
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
