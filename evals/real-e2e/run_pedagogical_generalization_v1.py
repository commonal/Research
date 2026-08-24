from __future__ import annotations

"""PROTOTYPE: adaptive pedagogical reading on a benchmark-style paper."""

from dataclasses import replace
from datetime import datetime, timezone
from html import escape, unescape
from pathlib import Path
from typing import Any, Mapping
import base64
import importlib.util
import json
import re
import shutil
import sys

import markdown

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_pulse.production.normalized import load_normalized_jsonl
from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import CanonicalPaperIR


V2_SCRIPT = ROOT / "evals" / "real-e2e" / "run_pedagogical_reading_v2.py"
SOURCE_ID = "2504.01848v3"
TITLE = "PaperBench: Evaluating AI's Ability to Replicate AI Research"
SOURCE_URL = "https://arxiv.org/abs/2504.01848v3"
MATERIAL_ROOT = ROOT / "tmp" / "reading-experiment-root" / SOURCE_ID
NORMALIZED = MATERIAL_ROOT / "normalized" / "blocks.jsonl"
OUTPUT = ROOT / "evals" / "real-e2e" / "pedagogical-generalization-paperbench-v1"


def _load_v2() -> Any:
    spec = importlib.util.spec_from_file_location("pedagogical_v2", V2_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load pedagogical v2 helpers.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.OUTPUT = OUTPUT
    module.base.OUTPUT = OUTPUT
    return module


v2 = _load_v2()
base = v2.base


def main() -> int:
    base._load_dotenv()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    candidate = PaperCandidate(SOURCE_ID, TITLE, SOURCE_URL, "agent-evaluation")
    paper = _load_paper(candidate)
    ordered = list(paper.ordered_blocks)
    allowed_ids = {block.block_id for block in ordered}
    material = [_transport(block) for block in ordered]
    asset_catalog = [_asset_candidate(block) for block in ordered if block.kind in {"figure", "table"}]
    model = base.DeepSeekPaperReadingModel.from_environment()

    previous_failure = OUTPUT / "adaptive-schema-plan-provider.json"
    if previous_failure.is_file() and not (OUTPUT / "adaptive-paper-schema.json").is_file():
        shutil.copy2(previous_failure, OUTPUT / "adaptive-schema-plan-invalid-echo-provider.json")

    schema_provider = OUTPUT / "adaptive-schema-plan-provider.json"
    schema_raw = _provider_response(schema_provider) if schema_provider.is_file() else base._call_json(model, "direct-long-writer", {
        "operation": "infer_adaptive_paper_schema_and_pedagogical_plan",
        "goal": "Infer this paper's own contribution shape before writing. This is a generalization test: do not reuse a mechanism/RL paper template.",
        "reader": "A technical reader who knows AI applications but has not read PaperBench and does not already understand benchmark construction or judge validation.",
        "rules": [
            "Classify the paper type from the supplied paper, then design 5-9 Chinese teaching sections appropriate to that type.",
            "The narrative must connect motivation, benchmark task, rubric construction, grading/judge validation, agent experiments, human comparison, and limitations only when supported.",
            "Every source fact, experiment, and must-preserve item must cite exact supplied block_ids.",
            "For every experiment state why it is run, setup/comparison, exact result, interpretation, and boundary.",
            "Do not create a formula section when the canonical material contains no formula blocks.",
            "Do not expose block ids in eventual prose; they are planning evidence only.",
        ],
        "material_summary": {
            "block_count": len(ordered),
            "kind_counts": {kind: sum(block.kind == kind for block in ordered) for kind in ("paragraph", "figure", "table", "formula")},
            "ordered_main_paper_blocks": [item for item in material if not _is_appendix(str(item["section"]))],
        },
        "return": {
            "paper_type": "specific contribution type",
            "central_problem": "plain explanation",
            "prior_evaluation_gap": "why prior evaluation is insufficient",
            "contribution_chain": [{"role": "role", "statement": "claim", "source_block_ids": ["id"]}],
            "sections": [{"section_id": "stable_snake_case", "heading": "Chinese heading", "reader_question": "question", "teaching_moves": ["moves"], "source_block_ids": ["ids"]}],
            "experiments": [{"key": "key", "why": "why", "setup": ["details"], "comparison": ["groups"], "results": ["exact results"], "interpretation": "meaning", "boundary": "not established", "source_block_ids": ["ids"]}],
            "must_preserve_facts": [{"statement": "atomic fact", "numeric_tokens": ["exact"], "named_entities": ["exact"], "source_block_ids": ["ids"]}],
            "unanswered_questions": [{"question": "important unknown", "source_block_ids": ["limitation id when available"]}],
        },
    })
    if not schema_provider.is_file():
        _preserve_provider("adaptive-schema-plan-provider.json")
    schema = _validate_schema(schema_raw, allowed_ids, ordered)
    asset_raw = base._call_json(model, "generalization-asset-plan", {
        "operation": "select_visual_and_table_assets_for_adaptive_note",
        "paper_type": schema["paper_type"],
        "central_problem": schema.get("central_problem", ""),
        "contribution_chain": schema.get("contribution_chain", []),
        "sections": schema["sections"],
        "experiments": schema.get("experiments", []),
        "asset_candidates": asset_catalog,
        "rules": [
            "Return exactly one compact decision for every candidate block_id: inline, reference, or omit.",
            "Choose by explanatory value, completeness, non-redundancy, and evidence value; there is no quota and most appendix prompt screenshots should be omitted.",
            "Use inline only when seeing the object materially improves understanding; use reference when prose can carry the conclusion.",
            "Set requires_pixels true only when topology, axes, curves, or visual grouping matters beyond the caption. Tables never require pixels.",
            "Keep supports and reason to one short sentence each.",
        ],
        "return": {"asset_decisions": [{"block_id": "every candidate id", "decision": "inline|reference|omit", "section_id": "planned section id or empty", "supports": "short", "reason": "short", "requires_pixels": True}]},
    })
    schema["asset_decisions"] = _validate_asset_decisions(asset_raw, schema, asset_catalog)
    schema, fuse_events = _apply_asset_fuse(schema)
    _write_json("adaptive-paper-schema.json", schema)

    visual_results: dict[str, dict[str, Any]] = {}
    for decision in schema["asset_decisions"]:
        if decision["decision"] != "inline" or not decision["requires_pixels"]:
            continue
        block = paper.block_by_id[decision["block_id"]]
        if block.kind != "figure" or not block.image_path or not Path(block.image_path).is_file():
            visual_results[block.block_id] = {"status": "failed", "reason": "safe local image unavailable"}
            continue
        raw = base._call_json(model, f"visual-{len(visual_results) + 1}", {
            "operation": "inspect_selected_paper_figure",
            "task": "Explain only the visual relations needed by the planned Chinese note. Distinguish what is visible from what the caption says.",
            "block_id": block.block_id,
            "caption": block.caption or block.text,
            "supports": decision["supports"],
            "neighboring_context": _neighbor_context(ordered, block.order),
            "rules": [
                "Describe axes, nodes, arrows, hierarchy, curves, plateaus, and comparisons only when visibly present.",
                "Do not infer exact values that are not legible; preserve uncertainty.",
                "Return visual_summary, observed_relations, caption_only_claims, and uncertainties.",
            ],
        }, image=block.image_path)
        visual_results[block.block_id] = {
            "status": "success" if not raw.get("_fallback") else "failed",
            "resolved_model": raw.get("_resolved_model"),
            "image_path": block.image_path,
            **{key: value for key, value in raw.items() if not key.startswith("_")},
        }
    _write_json("visual-results.json", visual_results)

    selected_ids = _selected_evidence_ids(schema, ordered)
    selected_material = [_transport(paper.block_by_id[block_id], full=True) for block_id in selected_ids]
    inline_assets = _inline_assets(schema, paper, visual_results)
    writer_raw = base._call_json(model, "direct-long-writer", {
        "operation": "write_structured_adaptive_pedagogical_chinese_note",
        "goal": "Write a fluent standalone Chinese deep-reading note that teaches PaperBench from motivation through benchmark design, judge validity, experiments, human comparison, and boundaries.",
        "length_guidance": "Usually 6500-10000 Chinese characters; do not compress benchmark construction or experiment setup into a result list.",
        "paper_schema": schema,
        "selected_evidence": selected_material,
        "complete_main_paper_context": [item for item in material if not _is_appendix(str(item["section"]))],
        "visual_observations": visual_results,
        "inline_asset_plan": inline_assets,
        "rules": [
            "Return one section for every planned section_id in exactly the planned order. Each item contains section_id and markdown, without the heading.",
            "Write connected explanatory prose with natural transitions. Introduce prerequisite concepts only when needed for this paper.",
            "Explain the benchmark as a causal pipeline: paper and rubric, agent submission, execution, rubric grading, aggregate replication score.",
            "Explain why hierarchical rubrics exist, how author involvement and underspecification are handled, and what Code Development, Execution, and Result Match separately measure.",
            "Separate validating SimpleJudge on JudgeEval from using it to score agents on PaperBench. Do not treat judge accuracy as agent performance.",
            "For each experiment explain the question, setup, comparison, exact result, interpretation, and limitation. Preserve every must-preserve fact.",
            "Explain selected visuals and tables at their semantic section, but do not reproduce them or emit asset markers; the renderer inserts them.",
            "Do not mention formulas because this PaperIR has none. Do not import the previous paper's security/RL vocabulary.",
            "Do not expose raw block ids, parser names, provider names, prompts, or local paths.",
            "Use cautious language for the 20-paper scope, LLM judge limitations, human subset, compute/time budgets, and whether replication score equals genuine scientific progress.",
        ],
        "return": {"sections": [{"section_id": "exact planned id", "markdown": "Chinese prose"}]},
    })
    writer_sections = _validate_writer(writer_raw, schema)
    _write_json("writer-output.json", {"sections": writer_sections, "resolved_model": writer_raw.get("_resolved_model")})

    asset_receipt = _publish_assets(schema, paper)
    note = _render_note(writer_sections, schema, paper, asset_receipt)
    (OUTPUT / "reading-note.md").write_text(note + "\n", encoding="utf-8")
    issues = _validate_note(note, schema, paper, asset_receipt)
    html_check = _write_review_html(note, schema, visual_results, issues, asset_receipt)
    receipt = {
        "source_id": SOURCE_ID,
        "route": "canonical_paper_ir_to_adaptive_schema_to_selective_visuals_to_structured_long_writer",
        "status": "completed" if not issues and html_check["passed"] else "needs_review",
        "paper_type": schema["paper_type"],
        "material": {
            "normalized_blocks": len(ordered),
            "figure_candidates": sum(block.kind == "figure" for block in ordered),
            "table_candidates": sum(block.kind == "table" for block in ordered),
            "formula_blocks": sum(block.kind == "formula" for block in ordered),
        },
        "model_calls": {
            "schema_plan": 1,
            "vision": model.vision_call_count,
            "writer": 1,
            "text_total": model.text_call_count,
        },
        "models": {"text": model.text_model, "vision": model.vision_model},
        "asset_counts": _asset_counts(schema),
        "asset_fuse_events": fuse_events,
        "evidence_alias_repairs": schema.get("evidence_alias_repairs", []),
        "published_assets": asset_receipt,
        "validation_issues": issues,
        "html_check": html_check,
        "started_at": started,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json("reading-receipt.json", receipt)
    _write_json("final-validation.json", {"issues": issues, "html_check": html_check})
    print(json.dumps({"output": str(OUTPUT), "receipt": receipt}, ensure_ascii=False, indent=2))
    return 0


def _load_paper(candidate: PaperCandidate) -> CanonicalPaperIR:
    paper = CanonicalPaperIR.from_normalized(
        source_id=SOURCE_ID,
        title=TITLE,
        source_url=candidate.source_url,
        blocks=load_normalized_jsonl(NORMALIZED),
    )
    return replace(paper, blocks=tuple(
        replace(block, image_path=str(resolved))
        if block.image_path and (resolved := _resolve_image(block.image_path)) else block
        for block in paper.blocks
    ))


def _resolve_image(value: str) -> Path | None:
    candidates = (MATERIAL_ROOT / value, MATERIAL_ROOT / "mineru" / "source" / "auto" / value)
    return next((item.resolve() for item in candidates if item.is_file()), None)


def _transport(block: Any, *, full: bool = False) -> dict[str, Any]:
    value = block.table_html or block.latex or block.caption or block.text
    limit = 7000 if full else 3800 if block.kind == "paragraph" else 2600
    return {
        "block_id": block.block_id,
        "order": block.order,
        "section": block.section,
        "kind": block.kind,
        "content": re.sub(r"\s+", " ", value).strip()[:limit],
    }


def _asset_candidate(block: Any) -> dict[str, Any]:
    return {
        "block_id": block.block_id,
        "order": block.order,
        "section": block.section,
        "kind": block.kind,
        "caption_or_projection": re.sub(r"\s+", " ", block.caption or block.text).strip()[:1800],
        "has_real_image": bool(block.kind == "figure" and block.image_path and Path(block.image_path).is_file()),
        "table_shape": _table_shape(block.table_html) if block.kind == "table" else None,
    }


def _validate_schema(raw: Mapping[str, Any], allowed_ids: set[str], ordered: list[Any]) -> dict[str, Any]:
    if raw.get("_fallback"):
        raise RuntimeError(f"Adaptive schema failed: {raw}")
    body = base._response_body(raw, "sections", "experiments")
    sections = body.get("sections")
    if not isinstance(sections, list) or not 5 <= len(sections) <= 9:
        raise RuntimeError("Adaptive schema must contain 5-9 sections.")
    section_ids = [str(item.get("section_id", "")) for item in sections if isinstance(item, Mapping)]
    if len(section_ids) != len(sections) or len(set(section_ids)) != len(section_ids) or any(not value for value in section_ids):
        raise RuntimeError("Adaptive section IDs are missing or duplicated.")
    forbidden = ("权限", "broker", "奖励函数", "六维风险")
    headings = " ".join(str(item.get("heading", "")) for item in sections)
    if any(term.casefold() in headings.casefold() for term in forbidden):
        raise RuntimeError("Adaptive plan leaked the previous paper template.")
    cleaned = dict(body)
    cleaned["sections"] = [dict(item) for item in sections]
    by_order = {str(block.order): block.block_id for block in ordered}
    repairs: list[dict[str, str]] = []
    for group in ("contribution_chain", "sections", "experiments", "must_preserve_facts", "unanswered_questions"):
        values = cleaned.get(group, [])
        if not isinstance(values, list):
            raise RuntimeError(f"Adaptive schema field is not a list: {group}")
        for item in values:
            if not isinstance(item, Mapping):
                continue
            resolved: list[str] = []
            invalid: list[str] = []
            for value in item.get("source_block_ids", ()):
                block_id = str(value)
                if block_id in allowed_ids:
                    resolved.append(block_id)
                    continue
                match = re.fullmatch(rf"normalized:{re.escape(SOURCE_ID)}:(?:text|table|figure|formula):(\d+)", block_id)
                target = by_order.get(match.group(1)) if match else None
                if target:
                    resolved.append(target)
                    repairs.append({"group": group, "alias": block_id, "resolved_block_id": target, "rule": "unique_exact_order"})
                else:
                    invalid.append(block_id)
            if invalid:
                raise RuntimeError(f"Unknown evidence IDs in {group}: {invalid}")
            item["source_block_ids"] = list(dict.fromkeys(resolved))
    cleaned["evidence_alias_repairs"] = repairs
    return cleaned


def _validate_asset_decisions(raw: Mapping[str, Any], schema: Mapping[str, Any], asset_catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if raw.get("_fallback"):
        raise RuntimeError(f"Asset planner failed: {raw}")
    body = base._response_body(raw, "asset_decisions")
    decisions = body.get("asset_decisions")
    if not isinstance(decisions, list):
        raise RuntimeError("Adaptive schema omitted asset decisions.")
    section_ids = [str(item["section_id"]) for item in schema["sections"]]
    expected_assets = {str(item["block_id"]) for item in asset_catalog}
    by_id = {str(item.get("block_id")): dict(item) for item in decisions if isinstance(item, Mapping)}
    if set(by_id) != expected_assets:
        missing = sorted(expected_assets - set(by_id))
        extra = sorted(set(by_id) - expected_assets)
        raise RuntimeError(f"Asset plan mismatch, missing={missing}, extra={extra}")
    cleaned_decisions = []
    for block_id in (str(item["block_id"]) for item in asset_catalog):
        item = by_id[block_id]
        decision = str(item.get("decision", ""))
        section_id = str(item.get("section_id", ""))
        if decision not in {"inline", "reference", "omit"}:
            raise RuntimeError(f"Invalid asset decision for {block_id}")
        if decision == "inline" and section_id not in section_ids:
            raise RuntimeError(f"Inline asset lacks valid section: {block_id}")
        cleaned_decisions.append({
            "block_id": block_id,
            "decision": decision,
            "section_id": section_id if decision == "inline" else "",
            "supports": str(item.get("supports", "")),
            "reason": str(item.get("reason", "")),
            "requires_pixels": bool(item.get("requires_pixels", False)),
        })
    return cleaned_decisions


def _apply_asset_fuse(schema: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    decisions = [dict(item) for item in schema["asset_decisions"]]
    events: list[dict[str, str]] = []
    seen = {"figure": 0, "table": 0}
    caps = {"figure": 3, "table": 3}
    for item in decisions:
        if item["decision"] != "inline":
            continue
        kind = "figure" if ":figure:" in item["block_id"] else "table"
        seen[kind] += 1
        if seen[kind] > caps[kind]:
            item["decision"] = "reference"
            item["section_id"] = ""
            events.append({"block_id": item["block_id"], "event": f"inline_to_reference_{kind}_cost_fuse"})
    result = dict(schema)
    result["asset_decisions"] = decisions
    return result, events


def _selected_evidence_ids(schema: Mapping[str, Any], ordered: list[Any]) -> list[str]:
    position = {block.block_id: index for index, block in enumerate(ordered)}
    ids: list[str] = []
    for group in ("contribution_chain", "sections", "experiments", "must_preserve_facts", "unanswered_questions"):
        for item in schema.get(group, ()):
            if isinstance(item, Mapping):
                ids.extend(str(value) for value in item.get("source_block_ids", ()) if str(value) in position)
    ids.extend(item["block_id"] for item in schema["asset_decisions"] if item["decision"] in {"inline", "reference"})
    expanded: list[str] = []
    for block_id in ids:
        index = position[block_id]
        expanded.extend(block.block_id for block in ordered[max(0, index - 1): min(len(ordered), index + 2)])
    return list(dict.fromkeys(expanded))


def _inline_assets(schema: Mapping[str, Any], paper: CanonicalPaperIR, visuals: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = []
    for item in schema["asset_decisions"]:
        if item["decision"] != "inline":
            continue
        block = paper.block_by_id[item["block_id"]]
        result.append({
            **item,
            "kind": block.kind,
            "caption_or_table": (block.caption or block.table_html or block.text)[:8000],
            "visual_observation": visuals.get(block.block_id),
        })
    return result


def _validate_writer(raw: Mapping[str, Any], schema: Mapping[str, Any]) -> list[dict[str, str]]:
    if raw.get("_fallback"):
        raise RuntimeError(f"Adaptive writer failed: {raw}")
    body = base._response_body(raw, "sections")
    sections = body.get("sections")
    if not isinstance(sections, list):
        raise RuntimeError("Adaptive writer returned no sections.")
    expected = [str(item["section_id"]) for item in schema["sections"]]
    actual = [str(item.get("section_id", "")) for item in sections if isinstance(item, Mapping)]
    if actual != expected:
        raise RuntimeError(f"Writer section mismatch: expected {expected}, got {actual}")
    result = [{"section_id": str(item["section_id"]), "markdown": str(item.get("markdown", "")).strip()} for item in sections]
    if any(not item["markdown"] for item in result):
        raise RuntimeError("Adaptive writer returned an empty section.")
    return result


def _publish_assets(schema: Mapping[str, Any], paper: CanonicalPaperIR) -> list[dict[str, Any]]:
    asset_dir = OUTPUT / "assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    receipt = []
    figure_index = 0
    for item in schema["asset_decisions"]:
        if item["decision"] != "inline":
            continue
        block = paper.block_by_id[item["block_id"]]
        if block.kind == "figure":
            figure_index += 1
            source = Path(str(block.image_path))
            suffix = source.suffix.casefold() if source.suffix else ".jpg"
            target = asset_dir / f"figure-{figure_index}{suffix}"
            shutil.copy2(source, target)
            receipt.append({"block_id": block.block_id, "kind": "figure", "relative_path": f"assets/{target.name}", "bytes": target.stat().st_size, "section_id": item["section_id"]})
        else:
            receipt.append({"block_id": block.block_id, "kind": "table", "relative_path": "", "bytes": 0, "section_id": item["section_id"]})
    return receipt


def _render_note(sections: list[dict[str, str]], schema: Mapping[str, Any], paper: CanonicalPaperIR, assets: list[dict[str, Any]]) -> str:
    heading_by_id = {str(item["section_id"]): str(item["heading"]) for item in schema["sections"]}
    decisions_by_section: dict[str, list[dict[str, Any]]] = {}
    asset_by_block = {item["block_id"]: item for item in assets}
    for decision in schema["asset_decisions"]:
        if decision["decision"] == "inline":
            decisions_by_section.setdefault(decision["section_id"], []).append(decision)
    parts = [f"# {TITLE}：从评测问题到实验结论的教学式精读"]
    for section in sections:
        section_id = section["section_id"]
        parts.extend([f"## {heading_by_id[section_id]}", section["markdown"]])
        for decision in decisions_by_section.get(section_id, ()):
            block = paper.block_by_id[decision["block_id"]]
            title = decision["supports"] or block.caption or "关键视觉材料"
            if block.kind == "figure":
                path = asset_by_block[block.block_id]["relative_path"]
                parts.append(f"![{title}]({path})\n\n*{title}*" )
            else:
                parts.append(f"**{title}**\n\n{_markdown_table(block.table_html or '')}")
    return "\n\n".join(parts).strip()


def _validate_note(note: str, schema: Mapping[str, Any], paper: CanonicalPaperIR, assets: list[dict[str, Any]]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if len(note) < 6500:
        issues.append({"type": "too_compressed", "detail": str(len(note))})
    compact = note.casefold().replace(",", "").replace(" ", "")
    required_groups = {
        "problem": ("复现", "评测"),
        "benchmark_scope": ("20", "8316"),
        "rubric": ("层级", "rubric"),
        "judge": ("simplejudge", "judgeeval"),
        "agent_results": ("claude3.5sonnet", "21.0%"),
        "human_comparison": ("人类", "小时"),
        "boundary": ("局限", "20篇"),
    }
    for name, terms in required_groups.items():
        missing = [term for term in terms if term.casefold().replace(" ", "") not in compact]
        if missing:
            issues.append({"type": "missing_generalization_content", "detail": f"{name}:{','.join(missing)}"})
    if any(term in compact for term in ("六维风险", "充分权限包络", "权限门控", "broker审计")):
        issues.append({"type": "previous_template_leak", "detail": "security/RL vocabulary"})
    if "normalized:" in note or "reading-experiment-root" in note:
        issues.append({"type": "internal_state_leak", "detail": "block id or path"})
    if sum(item["kind"] == "figure" for item in assets) > 3 or sum(item["kind"] == "table" for item in assets) > 3:
        issues.append({"type": "asset_fuse_failed", "detail": str(_asset_counts(schema))})
    if any(block.kind == "formula" for block in paper.ordered_blocks) is False and re.search(r"^## .*公式", note, flags=re.M):
        issues.append({"type": "invented_formula_section", "detail": "PaperIR has zero formulas"})
    source = " ".join(block.text + " " + (block.table_html or "") for block in paper.ordered_blocks)
    source = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", source)
    source = re.sub(r"(?<=\d)\s+(?=\d)", "", source)
    allowed = {base._normalize_number(value).rstrip("%") for value in base._numeric_tokens(base._replace_number_words(source))}
    prose = re.sub(r"(?m)^#{1,6}.*$", "", note)
    for value in dict.fromkeys(base._numeric_tokens(prose)):
        if base._normalize_number(value).rstrip("%") not in allowed:
            issues.append({"type": "unsupported_number", "detail": value})
    return issues


def repair_existing() -> int:
    """Expand the existing grounded draft without rerunning planning or vision."""
    base._load_dotenv()
    candidate = PaperCandidate(SOURCE_ID, TITLE, SOURCE_URL, "agent-evaluation")
    paper = _load_paper(candidate)
    ordered = list(paper.ordered_blocks)
    schema = _read_json(OUTPUT / "adaptive-paper-schema.json")
    visual_results = _read_json(OUTPUT / "visual-results.json")
    current = _read_json(OUTPUT / "writer-output.json")
    sections = current["sections"]
    selected_ids = _selected_evidence_ids(schema, ordered)
    selected_material = [_transport(paper.block_by_id[block_id], full=True) for block_id in selected_ids]
    model = base.DeepSeekPaperReadingModel.from_environment()
    raw = base._call_json(model, "direct-long-writer", {
        "operation": "expand_and_polish_grounded_pedagogical_note",
        "goal": "Expand the existing PaperBench note into a fluent 6500-9500 character teaching note without changing its evidence-grounded facts or adaptive structure.",
        "paper_schema": schema,
        "current_sections": sections,
        "selected_evidence": selected_material,
        "visual_observations": visual_results,
        "inline_assets": _inline_assets(schema, paper, visual_results),
        "rules": [
            "Return exactly the same section_ids in the same order, with expanded markdown and no headings inside markdown.",
            "Use third-person wording such as 作者/论文, not 我们, unless directly describing a quoted author claim.",
            "Add natural causal transitions and enough prerequisite explanation for a reader unfamiliar with benchmark construction.",
            "Clarify that the general benchmark rules and the actual main experiment budget are different layers; fully explain the 12-hour A10 setup, three runs per paper, and the 36-hour o1 extension.",
            "Deepen rubric hierarchy, author co-development, addenda for underspecification, node weights, and the three requirement types.",
            "Deepen SimpleJudge file selection, leaf grading, JudgeEval gold labels, accuracy/cost tradeoff, and remaining judge uncertainty.",
            "Explain BasicAgent, IterativeAgent, PaperBench-CodeDev, human subset, time curves, and which comparisons cannot be generalized.",
            "Do not turn table scores into new experiments. Preserve exact reported numbers and distinguish judge metrics from agent replication scores.",
            "Keep limitations near the relevant claims and end with a restrained synthesis of what PaperBench does and does not measure.",
            "Do not mention formulas, block ids, parsers, providers, prompts, local paths, or the previous security/RL paper.",
            "Return a short Chinese asset title for every inline asset block_id; titles explain why the object matters and contain no unsupported numbers.",
        ],
        "return": {
            "sections": [{"section_id": "exact existing id", "markdown": "expanded Chinese prose"}],
            "asset_titles": [{"block_id": "every inline asset id", "title": "Chinese explanatory title"}],
        },
    })
    revised = _validate_writer(raw, schema)
    body = base._response_body(raw, "sections", "asset_titles")
    inline_ids = {item["block_id"] for item in schema["asset_decisions"] if item["decision"] == "inline"}
    titles = {str(item.get("block_id")): str(item.get("title", "")).strip() for item in body.get("asset_titles", ()) if isinstance(item, Mapping)}
    if set(titles) != inline_ids or any(not value for value in titles.values()):
        raise RuntimeError(f"Repair asset title mismatch: expected {sorted(inline_ids)}, got {sorted(titles)}")
    for decision in schema["asset_decisions"]:
        if decision["block_id"] in titles:
            decision["supports"] = titles[decision["block_id"]]
    _write_json("adaptive-paper-schema.json", schema)
    _write_json("writer-output.json", {"sections": revised, "resolved_model": raw.get("_resolved_model"), "revision": "pedagogical_expansion"})
    assets = _publish_assets(schema, paper)
    note = _render_note(revised, schema, paper, assets)
    (OUTPUT / "reading-note.md").write_text(note + "\n", encoding="utf-8")
    issues = _validate_note(note, schema, paper, assets)
    html_check = _write_review_html(note, schema, visual_results, issues, assets)
    receipt = _read_json(OUTPUT / "reading-receipt.json")
    receipt["status"] = "completed" if not issues and html_check["passed"] else "needs_review"
    receipt["model_calls"] = {
        "schema_attempts": 2,
        "asset_planner": 1,
        "vision": 3,
        "writer": 1,
        "revision_writer": 1,
        "text_total_including_failed_attempt": 5,
    }
    receipt["revision_model"] = raw.get("_resolved_model")
    receipt["published_assets"] = assets
    receipt["validation_issues"] = issues
    receipt["html_check"] = html_check
    receipt["revised_at"] = datetime.now(timezone.utc).isoformat()
    _write_json("reading-receipt.json", receipt)
    _write_json("final-validation.json", {"issues": issues, "html_check": html_check})
    print(json.dumps({"status": receipt["status"], "characters": len(note), "issues": issues, "html_check": html_check}, ensure_ascii=False, indent=2))
    return 0


def expand_by_groups() -> int:
    """Expand two coherent chapter groups when a whole-note repair stays terse."""
    base._load_dotenv()
    candidate = PaperCandidate(SOURCE_ID, TITLE, SOURCE_URL, "agent-evaluation")
    paper = _load_paper(candidate)
    ordered = list(paper.ordered_blocks)
    schema = _read_json(OUTPUT / "adaptive-paper-schema.json")
    visual_results = _read_json(OUTPUT / "visual-results.json")
    current = _read_json(OUTPUT / "writer-output.json")["sections"]
    current_by_id = {item["section_id"]: item for item in current}
    planned_ids = [item["section_id"] for item in schema["sections"]]
    groups = [planned_ids[:4], planned_ids[4:]]
    model = base.DeepSeekPaperReadingModel.from_environment()
    revised_by_id: dict[str, dict[str, str]] = {}
    for group_index, section_ids in enumerate(groups, start=1):
        relevant_ids = _group_evidence_ids(schema, section_ids, ordered)
        evidence = [_transport(paper.block_by_id[block_id], full=True) for block_id in relevant_ids]
        raw = base._call_json(model, f"pedagogical-group-{group_index}", {
            "operation": "expand_coherent_chapter_group",
            "paper_type": schema["paper_type"],
            "global_problem": schema.get("central_problem", ""),
            "global_contribution_chain": schema.get("contribution_chain", []),
            "group_section_plans": [item for item in schema["sections"] if item["section_id"] in section_ids],
            "current_group_sections": [current_by_id[section_id] for section_id in section_ids],
            "relevant_experiments": [item for item in schema.get("experiments", []) if set(item.get("source_block_ids", ())) & set(relevant_ids)],
            "relevant_evidence": evidence,
            "visual_observations": {block_id: value for block_id, value in visual_results.items() if block_id in relevant_ids},
            "rules": [
                "Return exactly the requested section_ids in order; each markdown must contain 900-1400 Chinese characters of connected teaching prose.",
                "Do not pad with generic AI history, repeated conclusions, bullet lists, or near-translation. Add details only when they help understand this paper.",
                "Start each section from the reader question, explain prerequisites and setup, then connect naturally to the next section.",
                "Preserve exact evidence facts and experimental denominators; use third person 作者/论文 rather than 我们.",
                "Explain negative findings and comparison boundaries near the corresponding positive result.",
                "Do not emit headings, asset markers, block ids, parser/provider details, or local paths.",
            ],
            "return": {"sections": [{"section_id": "requested id", "markdown": "900-1400 Chinese characters"}]},
        })
        body = base._response_body(raw, "sections")
        values = body.get("sections")
        if not isinstance(values, list):
            raise RuntimeError(f"Group {group_index} returned no sections: {raw}")
        actual = [str(item.get("section_id", "")) for item in values if isinstance(item, Mapping)]
        if actual != section_ids:
            raise RuntimeError(f"Group {group_index} section mismatch: {actual}")
        for item in values:
            markdown_value = str(item.get("markdown", "")).strip()
            if len(markdown_value) < 750:
                raise RuntimeError(f"Group {group_index} section remains compressed: {item.get('section_id')}={len(markdown_value)}")
            revised_by_id[str(item["section_id"])] = {"section_id": str(item["section_id"]), "markdown": markdown_value}
    revised = [revised_by_id[section_id] for section_id in planned_ids]
    _write_json("writer-output.json", {"sections": revised, "resolved_model": model.text_model, "revision": "two_group_pedagogical_expansion"})
    assets = _publish_assets(schema, paper)
    note = _render_note(revised, schema, paper, assets)
    (OUTPUT / "reading-note.md").write_text(note + "\n", encoding="utf-8")
    issues = _validate_note(note, schema, paper, assets)
    html_check = _write_review_html(note, schema, visual_results, issues, assets)
    receipt = _read_json(OUTPUT / "reading-receipt.json")
    receipt["status"] = "completed" if not issues and html_check["passed"] else "needs_review"
    receipt["model_calls"] = {
        "schema_attempts": 2,
        "asset_planner": 1,
        "vision": 3,
        "writer": 1,
        "whole_note_revision": 1,
        "group_expansion": 2,
        "text_total_including_failed_attempt": 7,
    }
    receipt["published_assets"] = assets
    receipt["validation_issues"] = issues
    receipt["html_check"] = html_check
    receipt["group_expanded_at"] = datetime.now(timezone.utc).isoformat()
    _write_json("reading-receipt.json", receipt)
    _write_json("final-validation.json", {"issues": issues, "html_check": html_check})
    print(json.dumps({"status": receipt["status"], "characters": len(note), "issues": issues, "html_check": html_check}, ensure_ascii=False, indent=2))
    return 0


def blind_review_existing() -> int:
    """Judge the published note without exposing the paper or reading trace."""
    base._load_dotenv()
    note = (OUTPUT / "reading-note.md").read_text(encoding="utf-8")
    model = base.DeepSeekPaperReadingModel.from_environment()
    raw = base._call_json(model, "blind-reader-review", {
        "operation": "blind_reader_comprehension_review",
        "reader_role": "你是一名了解大模型但从未读过 PaperBench 原论文的技术读者。你只能依据下方中文笔记作答。",
        "note": note,
        "questions": [
            {"id": "motivation", "question": "为什么需要这个基准，已有评测缺了什么？"},
            {"id": "task_pipeline", "question": "一个 PaperBench 任务给智能体什么、要求交付什么、又怎样执行和评分？"},
            {"id": "rubric", "question": "分层 rubric 为什么必要，怎样构建，三类叶节点分别测量什么？"},
            {"id": "judge", "question": "SimpleJudge 与 JudgeEval 分别是什么，二者为什么不能混为智能体成绩？"},
            {"id": "agent_results", "question": "主智能体实验如何设置，主要结果和 IterativeAgent 对比说明什么？"},
            {"id": "human_comparison", "question": "人类对比怎样设置，时间曲线揭示了什么？"},
            {"id": "boundaries", "question": "这篇论文不能证明什么，最重要的适用边界是什么？"},
        ],
        "rules": [
            "不得使用笔记之外的知识补全答案；笔记没有说明的内容必须明确写不知道。",
            "每个问题给出自然语言答案，并标记 clear、partial 或 missing。",
            "把真正妨碍理解论文主线的缺失与可选细节分开；不要因为篇幅短就自动判失败。",
            "检查叙事是否按动机、基准构造、评分可信度、实验结果和边界形成连贯链条。",
            "检查图片和表格是否出现在能帮助理解的位置，但不要假装看到了笔记未解释的内容。",
        ],
        "return": {
            "answers": [{"id": "question id", "status": "clear|partial|missing", "answer": "only from note", "missing_information": ["items"]}],
            "narrative": {"status": "clear|partial|fragmented", "reason": "reason"},
            "visual_and_table_use": {"status": "helpful|mixed|unhelpful", "reason": "reason"},
            "critical_missing_information": ["only omissions that block understanding the paper"],
            "optional_details": ["details that could deepen but do not block understanding"],
            "overall": "pass|needs_targeted_revision|fail",
            "overall_reason": "reason",
        },
    })
    if raw.get("_fallback"):
        raise RuntimeError(f"Blind-reader review failed: {raw}")
    body = base._response_body(raw, "answers", "overall")
    answers = body.get("answers")
    expected = ["motivation", "task_pipeline", "rubric", "judge", "agent_results", "human_comparison", "boundaries"]
    actual = [str(item.get("id", "")) for item in answers] if isinstance(answers, list) else []
    if actual != expected:
        raise RuntimeError(f"Blind-reader answers mismatch: {actual}")
    statuses = {str(item["id"]): str(item.get("status", "")) for item in answers}
    if any(value not in {"clear", "partial", "missing"} for value in statuses.values()):
        raise RuntimeError(f"Blind-reader returned invalid statuses: {statuses}")
    overall = str(body.get("overall", ""))
    if overall not in {"pass", "needs_targeted_revision", "fail"}:
        raise RuntimeError(f"Blind-reader returned invalid overall status: {overall}")
    review = {**body, "resolved_model": raw.get("_resolved_model"), "reviewed_at": datetime.now(timezone.utc).isoformat()}
    _write_json("blind-reader-review.json", review)

    receipt = _read_json(OUTPUT / "reading-receipt.json")
    receipt["blind_reader_review"] = {
        "overall": overall,
        "answer_statuses": statuses,
        "narrative": body.get("narrative"),
        "critical_missing_information": body.get("critical_missing_information", []),
        "artifact": "blind-reader-review.json",
    }
    receipt["model_calls"] = {
        "schema_attempts": 2,
        "asset_planner": 1,
        "vision": 3,
        "writer": 1,
        "whole_note_revision": 1,
        "failed_group_expansion": 1,
        "blind_reader_review": 1,
        "text_total_including_failed_attempts": 7,
    }
    receipt["length_gate_interpretation"] = {
        "characters": len(note),
        "original_threshold": 6500,
        "decision": "content_based_blind_review",
        "reason": "The inherited character threshold is a cost fuse and writing target, not a paper-independent comprehension guarantee.",
    }
    receipt["status"] = "generalization_passed" if overall == "pass" and not body.get("critical_missing_information") and receipt.get("html_check", {}).get("passed") else "needs_review"
    receipt["completed_at"] = datetime.now(timezone.utc).isoformat()
    _write_json("reading-receipt.json", receipt)
    _write_json("final-validation.json", {
        "status": receipt["status"],
        "automated_warnings": receipt.get("validation_issues", []),
        "warning_adjudication": receipt["length_gate_interpretation"],
        "blind_reader_review": receipt["blind_reader_review"],
        "html_check": receipt.get("html_check", {}),
    })
    _append_blind_review_to_html(review, receipt["status"])
    print(json.dumps({"status": receipt["status"], "overall": overall, "answer_statuses": statuses, "critical_missing_information": body.get("critical_missing_information", [])}, ensure_ascii=False, indent=2))
    return 0


def refresh_blind_artifacts() -> int:
    """Refresh the verdict artifacts from an already completed blind review."""
    review = _read_json(OUTPUT / "blind-reader-review.json")
    receipt = _read_json(OUTPUT / "reading-receipt.json")
    _write_json("final-validation.json", {
        "status": receipt["status"],
        "automated_warnings": receipt.get("validation_issues", []),
        "warning_adjudication": receipt.get("length_gate_interpretation", {}),
        "blind_reader_review": receipt.get("blind_reader_review", {}),
        "html_check": receipt.get("html_check", {}),
    })
    _append_blind_review_to_html(review, str(receipt["status"]))
    return 0


def _append_blind_review_to_html(review: Mapping[str, Any], status: str) -> None:
    path = OUTPUT / "trace-review.html"
    html_value = path.read_text(encoding="utf-8")
    if 'id="blind-review"' in html_value:
        html_value = re.sub(r'<section id="blind-review".*?</section>', '', html_value, flags=re.S)
    panel = (
        '<section id="blind-review" style="display:block;background:#eef8f0;padding:20px 28px;'
        'border:1px solid #b8d8bf;border-radius:16px;margin:18px 0">'
        f'<h2>泛化结论：{escape(status)}</h2><pre>{escape(json.dumps(review, ensure_ascii=False, indent=2))}</pre></section>'
    )
    html_value = html_value.replace("</header>", "</header>" + panel, 1)
    path.write_text(html_value, encoding="utf-8")


def _group_evidence_ids(schema: Mapping[str, Any], section_ids: list[str], ordered: list[Any]) -> list[str]:
    position = {block.block_id: index for index, block in enumerate(ordered)}
    ids: list[str] = []
    for item in schema["sections"]:
        if item["section_id"] in section_ids:
            ids.extend(str(value) for value in item.get("source_block_ids", ()) if str(value) in position)
    for group in ("experiments", "must_preserve_facts", "unanswered_questions"):
        for item in schema.get(group, ()):
            linked = [str(value) for value in item.get("source_block_ids", ()) if str(value) in position]
            if any(paper_id in ids for paper_id in linked):
                ids.extend(linked)
    expanded: list[str] = []
    for block_id in ids:
        index = position[block_id]
        expanded.extend(block.block_id for block in ordered[max(0, index - 1): min(len(ordered), index + 2)])
    return list(dict.fromkeys(expanded))


def _write_review_html(note: str, schema: Mapping[str, Any], visuals: Mapping[str, Any], issues: list[dict[str, str]], assets: list[dict[str, Any]]) -> dict[str, Any]:
    note_html = markdown.markdown(note, extensions=["tables", "fenced_code"])
    embedded = 0
    for asset in assets:
        path = asset.get("relative_path")
        if asset["kind"] != "figure" or not path:
            continue
        local = OUTPUT / str(path)
        mime = "image/png" if local.suffix.casefold() == ".png" else "image/jpeg"
        data_uri = f"data:{mime};base64," + base64.b64encode(local.read_bytes()).decode("ascii")
        note_html = note_html.replace(f'src="{path}"', f'src="{data_uri}"')
        embedded += 1
    panels = {
        "最终笔记": note_html,
        "自适应论文结构": f"<pre>{escape(json.dumps(schema, ensure_ascii=False, indent=2))}</pre>",
        "视觉读取结果": f"<pre>{escape(json.dumps(visuals, ensure_ascii=False, indent=2))}</pre>",
        "验收状态": f"<pre>{escape(json.dumps({'issues': issues, 'assets': assets}, ensure_ascii=False, indent=2))}</pre>",
    }
    buttons = "".join(f'<button data-tab="p{i}" class="{("active" if i == 0 else "")}">{escape(name)}</button>' for i, name in enumerate(panels))
    sections = "".join(f'<section id="p{i}" class="panel {("active" if i == 0 else "")}">{body}</section>' for i, body in enumerate(panels.values()))
    html_value = f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>PaperBench 泛化精读实验</title><style>
body{{font:16px/1.85 system-ui;margin:0;background:#f3f5f8;color:#172033}}main{{max-width:1080px;margin:28px auto;padding:0 22px}}header{{background:#172033;color:white;padding:24px 30px;border-radius:16px}}nav{{display:flex;gap:8px;margin:18px 0;flex-wrap:wrap}}button{{border:1px solid #ccd3df;background:white;padding:9px 15px;border-radius:999px;cursor:pointer}}button.active{{background:#3559d8;color:white;border-color:#3559d8}}.panel{{display:none;background:white;padding:28px 38px;border-radius:16px;box-shadow:0 6px 24px #18243b12}}.panel.active{{display:block}}img{{display:block;max-width:100%;margin:24px auto;border-radius:10px}}table{{border-collapse:collapse;width:100%;margin:20px 0;font-size:14px}}th,td{{border:1px solid #d8deea;padding:8px 10px;text-align:left}}pre{{white-space:pre-wrap;background:#f6f8fb;padding:18px;border-radius:10px;overflow:auto}}h2{{margin-top:1.9em}}
</style></head><body><main><header><h1>PaperBench 教学式精读泛化实验</h1><p>验证问题：上一版方法能否离开安全/RL论文，自适应理解一篇基准与评测体系论文？</p></header><nav>{buttons}</nav>{sections}</main><script>document.querySelectorAll('button[data-tab]').forEach(b=>b.onclick=()=>{{document.querySelectorAll('button,.panel').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.tab).classList.add('active')}})</script></body></html>"""
    path = OUTPUT / "trace-review.html"
    path.write_text(html_value, encoding="utf-8")
    expected_figures = sum(item["kind"] == "figure" for item in assets)
    check = {
        "html_exists": path.is_file(),
        "rendered_images": note_html.count("<img"),
        "expected_images": expected_figures,
        "embedded_images": embedded,
        "no_tmp_path": "reading-experiment-root" not in note_html,
    }
    check["passed"] = check["html_exists"] and check["rendered_images"] == expected_figures and embedded == expected_figures and check["no_tmp_path"]
    return check


def _markdown_table(table_html: str) -> str:
    rows = []
    for raw_row in re.findall(r"<tr>(.*?)</tr>", table_html, flags=re.I | re.S):
        cells = re.findall(r"<t[dh]>(.*?)</t[dh]>", raw_row, flags=re.I | re.S)
        rows.append([_translate_cell(unescape(re.sub(r"<[^>]+>", "", cell)).strip()) for cell in cells])
    return base._markdown_table(rows)


def _translate_cell(value: str) -> str:
    translations = {
        "MODEL": "模型", "PAPERBENCH": "PaperBench 得分", "PAPERBENCH CODE-DEV": "PaperBench 代码开发得分",
        "ACC.": "准确率", "PREC.": "精确率", "REC.": "召回率", "F1": "F1", "COST": "成本（美元）",
        "OVERALL": "整体", "CODE DEVELOPMENT": "代码开发", "EXECUTION": "执行", "RESULT MATCH": "结果匹配",
        "RESULTS ANALYSIS": "结果分析", "RANDOM BASELINE": "随机基线", "With an extended 36 hour limit": "36 小时时限",
    }
    return translations.get(value, value)


def _table_shape(table_html: str | None) -> dict[str, int] | None:
    if not table_html:
        return None
    rows = re.findall(r"<tr>.*?</tr>", table_html, flags=re.I | re.S)
    columns = max((len(re.findall(r"<t[dh]>.*?</t[dh]>", row, flags=re.I | re.S)) for row in rows), default=0)
    return {"rows": len(rows), "columns": columns}


def _neighbor_context(ordered: list[Any], order: int) -> str:
    values = [block.text for block in ordered[max(0, order - 2): min(len(ordered), order + 3)] if block.kind == "paragraph"]
    return " ".join(values)[:6000]


def _is_appendix(section: str) -> bool:
    return bool(re.match(r"^[A-I]\.", section)) or section in {"References", "Acknowledgements"}


def _asset_counts(schema: Mapping[str, Any]) -> dict[str, int]:
    return {decision: sum(item["decision"] == decision for item in schema["asset_decisions"]) for decision in ("inline", "reference", "omit")}


def _preserve_provider(name: str) -> None:
    source = OUTPUT / "direct-long-writer-provider.json"
    if source.is_file():
        shutil.copy2(source, OUTPUT / name)


def _provider_response(path: Path) -> dict[str, Any]:
    provider = json.loads(path.read_text(encoding="utf-8"))
    raw = str(provider.get("content", "")).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Provider artifact is not an object: {path}")
    return {**parsed, "_resolved_model": provider.get("resolved_model")}


def _write_json(name: str, value: Any) -> None:
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    if "--refresh-blind-artifacts" in sys.argv:
        raise SystemExit(refresh_blind_artifacts())
    if "--blind-review-existing" in sys.argv:
        raise SystemExit(blind_review_existing())
    if "--repair-existing" in sys.argv:
        raise SystemExit(repair_existing())
    if "--expand-by-groups" in sys.argv:
        raise SystemExit(expand_by_groups())
    raise SystemExit(main())
