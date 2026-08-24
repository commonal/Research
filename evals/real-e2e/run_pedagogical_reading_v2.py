from __future__ import annotations

"""PROTOTYPE: structured pedagogical writer with deterministic semantic assets."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
import importlib.util
import json
import re
import sys


ROOT = Path(__file__).resolve().parents[2]
V1_SCRIPT = ROOT / "evals" / "real-e2e" / "run_pedagogical_reading_v1.py"
V1_OUTPUT = ROOT / "evals" / "real-e2e" / "pedagogical-reading-v1"
OUTPUT = ROOT / "evals" / "real-e2e" / "pedagogical-reading-v2"
READING_STATE = ROOT / "evals" / "real-e2e" / "integrated-b-deepening-v1"
FULL_MODEL = ROOT / "evals" / "real-e2e" / "full-paper-b-production-v5"


def _load_v1() -> Any:
    spec = importlib.util.spec_from_file_location("pedagogical_v1", V1_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load pedagogical v1 helpers.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.OUTPUT = OUTPUT
    module.base.OUTPUT = OUTPUT
    return module


v1 = _load_v1()
base = v1.base


SECTION_PLAN = [
    {
        "section_id": "problem_and_background",
        "heading": "为什么任务做对了，仍然可能不安全",
        "reader_goal": "从一个贯穿全文的 bug.txt 例子理解任务相对越权、最小权限，以及门控和沙箱为什么仍不够。",
        "assets_after": [],
    },
    {
        "section_id": "method_causal_chain",
        "heading": "作者怎样把“少用权限”变成可训练信号",
        "reader_goal": "沿同一个例子依次解释六维风险、充分权限包络、超额权限、Broker 前后审计、安全成功和奖励函数。",
        "assets_after": ["figure-1"],
    },
    {
        "section_id": "training_and_evaluation_setup",
        "heading": "训练和评估究竟是怎样搭起来的",
        "reader_goal": "完整交代模型、训练算法、任务目录、训练/验证划分、episode 采样、评估口径和 seed 选择。",
        "assets_after": ["table-task-split"],
    },
    {
        "section_id": "main_results_and_learning_curve",
        "heading": "核心结果有多大，收益又在什么时候出现",
        "reader_goal": "先解释完整 500 任务主结果，再解释 checkpoint 曲线；区分任务成功、安全成功和成功但越权。",
        "assets_after": ["table-main-results", "table-checkpoints"],
    },
    {
        "section_id": "generalization_and_ablation",
        "heading": "模型真的学会了吗：提示消融、同环境泛化与外部基准",
        "reader_goal": "依次说明每组实验为什么做、设置、结果和能说明的边界，禁止把同 Broker 新任务族写成开放环境泛化。",
        "assets_after": [],
    },
    {
        "section_id": "continuation_and_stability",
        "heading": "继续训练能补短板吗，以及代价是什么",
        "reader_goal": "解释 400 步延续实验、能力保留、跨 seed 不稳定和陌生接口上的失败。",
        "assets_after": [],
    },
    {
        "section_id": "conclusion_and_boundaries",
        "heading": "这篇论文真正证明了什么，又没有证明什么",
        "reader_goal": "给出克制结论，集中说明包络标定、合成任务、单一模型、奖励权重、缺失消融和不能替代门控/沙箱。",
        "assets_after": [],
    },
]


def main() -> int:
    base._load_dotenv()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    paper = base._load_paper(base.PaperCandidate(base.SOURCE_ID, base.TITLE, "https://arxiv.org/abs/2608.18351v1", "security"))
    experiment_map = _read_json(V1_OUTPUT / "experiment-map.json")
    paper_model = _read_json(FULL_MODEL / "paper-model.json")
    formula_briefs = _read_json(READING_STATE / "formula-briefs.json")
    visual_results = _read_json(READING_STATE / "visual-results.json")
    source_blocks = v1._teaching_source_blocks(paper)
    model = base.DeepSeekPaperReadingModel.from_environment()

    plan = {
        "reader": "没读过原论文、了解大模型但不熟悉 Agent 权限对齐和 GRPO 的技术读者",
        "running_example": "让智能体只读取 bug.txt 并给修复建议；沿全文比较必要读取与无关写入、秘密访问、扩大作用域。",
        "sections": SECTION_PLAN,
        "writing_principles": [
            "先交代问题和必要背景，再讲抽象；每引入一个公式，都先说明它解决哪个判断困难。",
            "实验不是数字清单：每组都按动机、设置与比较、结果、解释、边界展开，并用过渡句连接。",
            "充分权限包络是任务设计和标注前提，不是模型自动知道的真值；论文没有充分回答自动构造、歧义和噪声。",
            "同族和整族保留评估仍共享 Broker、沙箱和验证器约定，不能写成开放环境或任意接口泛化。",
            "Fig.1 和原论文表格由 Renderer 放入固定语义位置；Writer 只解释它们，不复制表格。",
        ],
    }
    _write_json("teaching-plan.json", plan)

    raw = base._call_json(model, "direct-long-writer", {
        "operation": "write_structured_pedagogical_chinese_paper_note",
        "reader": plan["reader"],
        "goal": "写一篇像优秀人类精读作者写的中文长笔记：可独立读懂、过渡自然、实验完整，但不是逐段翻译。",
        "length_guidance": "七个章节正文合计约 6000 至 9000 个中文字符；不要为了短而压缩实验。",
        "section_plan": SECTION_PLAN,
        "running_example": plan["running_example"],
        "rules": [
            "严格返回七个 sections，section_id 与 section_plan 完全一致且顺序一致；每项只含 section_id 和 markdown。",
            "markdown 是连续解释性正文，不包含章节标题，不包含 block id，不包含 asset marker，不重抄表格。",
            "段落之间写出因果过渡。至少在问题、方法和结论三个位置回到 bug.txt 例子，但不要机械重复。",
            "方法部分先讲判断困难，再解释六维向量；明确 z_req(x) 由任务设计者预先定义，是系统成立的标注前提。",
            "完整解释 z(a_t)、轨迹逐分量 max、Delta、SafeSuccess 和奖励公式；E 是证据存在与充分性，不是效率。",
            "结合 Fig.1 说明 policy、Broker 执行前审计、终端/MCP 执行、执行后遥测与验证器、确定性奖励、训练更新的闭环，并指出图中没有画出六维向量和权限包络。",
            "训练设置和每组实验都必须交代为什么做、怎么设置和比较、精确结果、结果说明什么以及不能说明什么。",
            "保留 ExperimentMap 中全部七组实验。不得把 Seed 1 的选择性结果或同 Broker 的整族保留结果夸成稳定的开放环境泛化。",
            "明确 132/2896 的 4.56% 是成功但越权 episode 占全部评估 episode 的比例，不是动作越权率。",
            "缺失的风险维度消融、奖励权重敏感性、真实生产评估和充分权限包络自动构造只能写成未回答问题，不能虚构结果。",
            "结论必须写明该方法是安全前置的行为偏好层，不能替代权限门控和沙箱的强制边界。",
            "只根据提供的 source facts、ExperimentMap、公式 brief 和视觉观察写作，不添加没有证据的数字或实验。",
        ],
        "paper_overview": v1._paper_overview(paper_model),
        "experiment_map": experiment_map,
        "formula_briefs": formula_briefs,
        "visual_observation": [item for item in visual_results.values() if item.get("status") == "success"],
        "source_material": source_blocks,
        "return": {"sections": [{"section_id": "exact id", "markdown": "Chinese explanatory prose"}]},
    })
    if raw.get("_fallback"):
        raise RuntimeError(f"Writer failed instead of producing valid state: {raw}")
    sections = _validate_sections(base._response_body(raw, "sections"))
    _write_json("writer-output.json", {"sections": sections, "resolved_model": raw.get("_resolved_model")})

    asset_receipt = v1._publish_figure_asset(visual_results)
    note = _render_note(sections, paper)
    (OUTPUT / "reading-note.md").write_text(note + "\n", encoding="utf-8")
    issues = _validate_note(note, paper, experiment_map, formula_briefs)
    html_check = v1._write_review_html(note, experiment_map, plan, issues, asset_receipt)
    comparison = _compare(note, experiment_map)
    receipt = {
        "route": "reused_experiment_map_plus_structured_pedagogical_writer_plus_deterministic_assets",
        "status": "completed" if not issues and html_check["passed"] else "needs_review",
        "reused": {
            "experiment_map": str(V1_OUTPUT / "experiment-map.json"),
            "paper_model": str(FULL_MODEL / "paper-model.json"),
            "formula_briefs": str(READING_STATE / "formula-briefs.json"),
            "visual_result": str(READING_STATE / "visual-results.json"),
        },
        "model_calls": {"writer": 1, "text_total": model.text_call_count, "vision_total": model.vision_call_count},
        "writer_model": raw.get("_resolved_model"),
        "section_count": len(sections),
        "asset_placement": {item["section_id"]: item["assets_after"] for item in SECTION_PLAN if item["assets_after"]},
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


def _validate_sections(body: Mapping[str, Any]) -> list[dict[str, str]]:
    raw_sections = body.get("sections")
    if not isinstance(raw_sections, list):
        raise RuntimeError("Writer did not return structured sections.")
    expected = [item["section_id"] for item in SECTION_PLAN]
    actual = [str(item.get("section_id", "")) for item in raw_sections if isinstance(item, Mapping)]
    if actual != expected:
        raise RuntimeError(f"Writer section contract mismatch: expected {expected}, got {actual}")
    sections = [{"section_id": str(item["section_id"]), "markdown": str(item.get("markdown", "")).strip()} for item in raw_sections]
    if any(not item["markdown"] for item in sections):
        raise RuntimeError("Writer returned an empty section.")
    return sections


def _render_note(sections: list[dict[str, str]], paper: Any) -> str:
    plan_by_id = {item["section_id"]: item for item in SECTION_PLAN}
    parts = ["# 让智能体学会只用必要权限：一篇从问题到实验的精读"]
    for section in sections:
        plan = plan_by_id[section["section_id"]]
        parts.extend([f"## {plan['heading']}", section["markdown"]])
        for asset in plan["assets_after"]:
            parts.append(_asset_markdown(asset, paper))
    return "\n\n".join(parts).strip()


def _asset_markdown(asset: str, paper: Any) -> str:
    if asset == "figure-1":
        return "![图1：Broker 强化学习闭环](assets/figure-1.jpg)\n\n*图1不是装饰图：它把策略动作、Broker 执行前审计、终端或 MCP 执行、执行后遥测与验证器、确定性奖励和训练更新连成闭环。六维风险向量与充分权限包络并未直接画在图中，需要结合正文公式理解。*"
    tables = {
        "table-task-split": ("normalized:2608.18351v1:table:5665e95f40fd", "表 I：任务目录的训练与两类验证划分"),
        "table-main-results": ("normalized:2608.18351v1:table:867444f70fd8", "表 II：完整 500 任务保留集的核心结果"),
        "table-checkpoints": ("normalized:2608.18351v1:table:90f635985e69", "表 III：不同训练检查点的表现"),
    }
    block_id, title = tables[asset]
    block = paper.block_by_id[block_id]
    return f"**{title}**\n\n{v1._localized_table(block_id, block.table_html or '')}"


def _validate_note(note: str, paper: Any, experiment_map: Mapping[str, Any], formula_briefs: Any) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if len(note) < 5500:
        issues.append({"type": "too_compressed", "detail": str(len(note))})
    requirements = {
        "background": ("最小权限", "权限门控", "沙箱"),
        "running_example": ("bug.txt",),
        "envelope_is_assumption": ("任务设计者", "预先定义", "标注"),
        "broker_loop": ("执行前", "执行后", "遥测", "验证器", "训练更新"),
        "formula_chain": ("z(a_t)", "z_req", "Delta", "SAFESUCCESS", "0.60"),
        "training_setup": ("Qwen3.5-4B", "LoRA", "Dr. GRPO", "1500", "8条", "4096", "20"),
        "task_setup": ("2000", "300", "200", "500", "2896"),
        "main_results": ("68.92%", "99.27%", "64.36%", "98.48%", "4.56%", "0.79%"),
        "ablation_and_external": ("提示消融", "MetaTool", "FORTIS", "ToolPrivBench"),
        "continuation_and_seed": ("延续", "Seed 0", "Seed 1", "Seed 2"),
    }
    folded = note.casefold()
    compact = folded.replace(",", "").replace(" ", "")
    for name, terms in requirements.items():
        missing = [term for term in terms if term.casefold().replace(",", "").replace(" ", "") not in compact]
        if missing:
            issues.append({"type": "missing_teaching_content", "detail": f"{name}:{','.join(missing)}"})
    boundary_is_explicit = "不能替代" in compact or ("不是" in compact and "替代品" in compact)
    if not boundary_is_explicit:
        issues.append({"type": "missing_teaching_content", "detail": "boundary"})
    if note.count("bug.txt") < 2:
        issues.append({"type": "running_example_not_reused", "detail": str(note.count("bug.txt"))})
    if "normalized:" in note or "tmp/reading-experiment-root" in note:
        issues.append({"type": "internal_state_leak", "detail": "block id or tmp path"})
    if re.search(r"E\s*[^。；\n]{0,20}(?:效率|efficiency)", note, flags=re.I) and "e不是效率" not in compact:
        issues.append({"type": "wrong_formula_meaning", "detail": "E"})
    if not any(value in compact for value in ("同一broker", "相同broker", "同一套broker", "相同的broker")):
        issues.append({"type": "overclaim_risk", "detail": "family holdout lacks same-Broker boundary"})

    source = " ".join(filter(None, (block.text or block.latex or block.table_html for block in paper.ordered_blocks)))
    source += " " + json.dumps(experiment_map, ensure_ascii=False) + " " + json.dumps(formula_briefs, ensure_ascii=False)
    source = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", source)
    source = re.sub(r"(?<=\d)\s+(?=\d)", "", source)
    allowed = {base._normalize_number(value) for value in base._numeric_tokens(base._replace_number_words(source))}
    prose = re.sub(r"(?m)^#{1,6}.*$", "", note)
    for value in dict.fromkeys(base._numeric_tokens(prose)):
        if base._normalize_number(value) not in allowed:
            issues.append({"type": "unsupported_number", "detail": value})
    return issues


def _compare(note: str, experiment_map: Mapping[str, Any]) -> dict[str, Any]:
    previous = (v1.PREVIOUS / "reading-note.md").read_text(encoding="utf-8")
    compact = note.casefold().replace(",", "").replace(" ", "")
    concepts = {
        "task_catalog_2000": ("2000",),
        "training_tasks_1500": ("1500",),
        "within_family_300": ("300",),
        "excluded_family_200": ("200",),
        "evaluation_episodes_2896": ("2896",),
        "seed_analysis": ("seed0", "seed1", "seed2"),
        "prompt_ablation": ("提示消融",),
        "external_benchmarks": ("metatool", "fortis", "toolprivbench"),
        "continuation": ("延续",),
    }
    return {
        "characters": {"pedagogical": len(note), "integrated_v4": len(previous)},
        "headings": re.findall(r"^#{1,3}\s+(.+)$", note, flags=re.M),
        "concept_hits": {name: all(term in compact for term in terms) for name, terms in concepts.items()},
        "experiment_map_entries": len(experiment_map.get("experiments", ())),
    }


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(name: str, value: Any) -> None:
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def revalidate_existing() -> int:
    """Re-run local validation and HTML checks without another model call."""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    paper = base._load_paper(base.PaperCandidate(base.SOURCE_ID, base.TITLE, "https://arxiv.org/abs/2608.18351v1", "security"))
    note = (OUTPUT / "reading-note.md").read_text(encoding="utf-8")
    experiment_map = _read_json(V1_OUTPUT / "experiment-map.json")
    formula_briefs = _read_json(READING_STATE / "formula-briefs.json")
    plan = _read_json(OUTPUT / "teaching-plan.json")
    receipt = _read_json(OUTPUT / "reading-receipt.json")
    issues = _validate_note(note, paper, experiment_map, formula_briefs)
    html_check = v1._write_review_html(note, experiment_map, plan, issues, receipt["published_asset"])
    receipt["validation_issues"] = issues
    receipt["html_check"] = html_check
    receipt["comparison"] = _compare(note, experiment_map)
    receipt["status"] = "completed" if not issues and html_check["passed"] else "needs_review"
    receipt["revalidated_at"] = datetime.now(timezone.utc).isoformat()
    receipt["revalidation_model_calls"] = 0
    _write_json("reading-receipt.json", receipt)
    _write_json("final-validation.json", {"issues": issues, "html_check": html_check})
    print(json.dumps({"status": receipt["status"], "issues": issues, "html_check": html_check}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(revalidate_existing() if "--revalidate-existing" in sys.argv else main())
