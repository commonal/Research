from __future__ import annotations

"""PROTOTYPE: semantic SectionDepthPlan over an existing generic reading run."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
import argparse
import importlib.util
import json
import shutil
import sys


ROOT = Path(__file__).resolve().parents[2]
V2_SCRIPT = ROOT / "evals" / "real-e2e" / "run_adaptive_pedagogical_generalization_v2.py"


def _load_v2() -> Any:
    spec = importlib.util.spec_from_file_location("adaptive_pedagogical_generalization_v2", V2_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load generalization v2 helpers.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


v2 = _load_v2()
base = v2.base


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    base_output = Path(args.base_output).resolve()
    config = v2.ExperimentConfig(
        source_id=args.source_id,
        title=args.title,
        source_url=args.source_url,
        material_root=Path(args.material_root).resolve(),
        output=Path(args.output).resolve(),
    )
    if config.output == base_output:
        raise RuntimeError("SectionDepthPlan experiment must preserve the base run in a different output directory.")
    config.output.mkdir(parents=True, exist_ok=True)
    for name in ("paper-model.json", "asset-plan.json", "definition-briefs.json", "visual-results.json"):
        source = base_output / name
        if not source.is_file():
            raise FileNotFoundError(source)
        shutil.copy2(source, config.output / name)

    v2._bind(config)
    base._load_dotenv()
    paper = v2.v1._load_paper(v2.PaperCandidate(config.source_id, config.title, config.source_url, "generalization"))
    blocks = v2._main_paper_blocks(paper.ordered_blocks)
    handles = {f"b{block.order}": block for block in blocks}
    paper_text = v2._paper_text(blocks)
    paper_model = v2._read(config.output / "paper-model.json")
    decisions = v2._read(config.output / "asset-plan.json")["decisions"]
    briefs = v2._read(config.output / "definition-briefs.json")["briefs"]
    visuals = v2._read(config.output / "visual-results.json")
    model = base.DeepSeekPaperReadingModel.from_environment()
    calls = {"section_depth_plan": 0, "writer": 0, "blind_reader": 0, "vision": 0}

    evidence_handles = _relevant_handles(paper_model, decisions, handles, blocks)
    coverage_ledger = _coverage_ledger(paper_model, decisions)
    depth_raw = _call_cached(config, model, "section-depth-plan", {
        "operation": "plan_semantic_explanation_depth_without_length_quotas",
        "goal": "Decide what each section must teach so an unfamiliar technical reader can reconstruct the paper's argument, not merely receive a longer summary.",
        "paper_model": paper_model,
        "coverage_ledger": coverage_ledger,
        "selected_assets": [item for item in decisions if item["decision"] == "inline"],
        "definition_briefs": briefs,
        "evidence_catalog": [v2._candidate(handle, handles[handle]) for handle in evidence_handles],
        "rules": [
            "Return every existing section_id exactly once and in the existing order; do not add a paper-type-specific template.",
            "Plan from reader knowledge transitions: state what the reader does not yet understand and what they must understand after the section.",
            "Use explanation_mode to express the intellectual job, not a word-count tier. Choose orient, reconstruct, compare, connect_evidence, or qualify.",
            "answer_contract must contain the concrete questions whose answers make the section complete. Avoid generic requests such as explain more or be detailed.",
            "prerequisite_bridges may add only broadly established concepts needed to follow the source; label their purpose and never present them as this paper's contribution.",
            "reasoning_steps must connect problem, design choice, mechanism, evidence, and boundary where applicable; do not turn the section into isolated facts.",
            "For every linked experiment specify why it exists, setup, comparison, result, interpretation, and boundary fields that the Writer must preserve.",
            "Assign each selected asset a unique explanatory job. Assets do not create an obligation to repeat every cell, curve, or symbol.",
            "Assign every coverage_ledger obligation_id to exactly one section. You may integrate overlapping obligations into one explanation, but may not omit any argument node, experiment, must-preserve fact, limitation, or inline asset. PaperModel prerequisites are suggestions that may be refined through prerequisite_bridges.",
            "stop_conditions describe observable reader understanding. Never use character counts, token counts, paragraph counts, or vague completeness claims.",
            "Use only supplied evidence handles. Put unsupported questions in open_questions rather than inventing an answer.",
        ],
        "return": {
            "reader_path": [{"section_id": "existing id", "knowledge_before": "reader state", "knowledge_after": "reader state"}],
            "sections": [{
                "section_id": "existing id",
                "explanation_mode": "orient|reconstruct|compare|connect_evidence|qualify",
                "answer_contract": ["concrete reader question"],
                "prerequisite_bridges": [{"concept": "concept", "purpose": "why needed", "source_type": "pedagogical_synthesis|source_fact"}],
                "reasoning_steps": ["ordered conceptual step"],
                "experiment_contracts": [{"experiment_key": "paper-model key", "must_cover": ["why", "setup", "comparison", "result", "interpretation", "boundary"]}],
                "asset_jobs": [{"handle": "inline bN", "job": "specific explanatory job"}],
                "coverage_obligation_ids": ["exact obligation_id from coverage_ledger"],
                "transition_in": "connection from previous section",
                "transition_out": "question prepared for next section",
                "stop_conditions": ["observable understanding outcome"],
                "evidence_handles": ["bN"],
                "open_questions": ["unsupported but useful question"],
            }],
            "cross_section_guardrails": ["anti-repetition or narrative guardrail"],
        },
    }, calls)
    depth_plan = _validate_depth_plan(depth_raw, paper_model, handles, decisions, coverage_ledger)
    v2._write(config, "section-depth-plan.json", depth_plan)

    writer_raw = _call_cached(config, model, "depth-aware-writer", {
        "operation": "write_one_depth_planned_pedagogical_chinese_note",
        "goal": "Write a standalone Chinese paper reading note by fulfilling each section's understanding contract. Depth must follow conceptual and evidential load, not uniform compression or padding.",
        "paper_model": paper_model,
        "section_depth_plan": depth_plan,
        "definition_briefs": briefs,
        "visual_interpretations": visuals,
        "selected_assets": [item for item in decisions if item["decision"] == "inline"],
        "ordered_paper": paper_text,
        "rules": [
            "Return one section for every planned section_id in exactly the planned order, without headings inside markdown.",
            "Fulfil every answer_contract, reasoning_steps, experiment_contract, transition, and stop_condition that is supported by the paper.",
            "Fulfil every coverage obligation assigned to the section. Integrate overlapping facts naturally instead of repeating them, but never silently drop an experiment, key fact, limitation, prerequisite bridge, argument node, or inline asset job.",
            "Write as a patient technical explainer: establish why a concept is needed before naming details, and connect each design choice to the problem it addresses.",
            "Treat prerequisite_bridges as pedagogical synthesis, not as claims of novelty by the authors.",
            "For experiments, reconstruct purpose, setup, comparison, exact result, interpretation, and boundary; do not collapse them into a leaderboard or list of numbers.",
            "Explain an inline visual, formula, or table only for its assigned asset_job and in its semantic section. Do not emit asset markers or raw tables.",
            "Preserve exact source-supported quantities and stable names from must_preserve_facts. Do not add unsupported numbers, mechanisms, definitions, or causal claims.",
            "Use cross-section guardrails to avoid repeated introductions, repeated conclusions, and abrupt topic jumps.",
            "Open questions must remain explicit questions or unknowns; never answer them from outside knowledge.",
            "Do not expose evidence handles, block IDs, prompts, parser/provider names, or local paths.",
        ],
        "return": {"sections": [{"section_id": "exact planned id", "markdown": "connected Chinese prose"}]},
    }, calls)
    sections = v2._validate_sections(writer_raw, paper_model)
    v2._write(config, "writer-output.json", {
        "sections": sections,
        "resolved_model": writer_raw.get("_resolved_model"),
        "revision": "semantic_section_depth_plan",
    })

    assets = v2._publish_assets(config, decisions, handles)
    note = v2._render(config, sections, paper_model, decisions, handles, briefs, assets)
    (config.output / "reading-note.md").write_text(note + "\n", encoding="utf-8")
    observations = v2._validate_note(note, paper_model, blocks, decisions, briefs, assets)
    non_blocking_observations = [item for item in observations if item["type"] == "too_compressed"]
    issues = [item for item in observations if item["type"] != "too_compressed"]
    html_check = v2._write_html(config, note, paper_model, decisions, briefs, visuals, issues, assets)
    blind_raw = _call_cached(config, model, "depth-aware-blind-reader", _blind_request(note, depth_plan), calls)
    blind = v2._validate_blind(blind_raw)
    v2._write(config, "blind-reader-review.json", blind)

    status = "generalization_passed" if not issues and html_check["passed"] and blind["overall"] == "pass" and not blind["critical_missing_information"] else "needs_review"
    old_note = (base_output / "reading-note.md").read_text(encoding="utf-8")
    receipt = {
        "source_id": config.source_id,
        "title": config.title,
        "route": "existing_generic_reading_state_to_semantic_section_depth_plan_to_single_writer",
        "status": status,
        "model_calls_this_invocation": {**calls, "text_total": calls["section_depth_plan"] + calls["writer"] + calls["blind_reader"]},
        "experiment_model_calls": {
            "section_depth_plan": int((config.output / "section-depth-plan-provider.json").is_file()),
            "writer": int((config.output / "depth-aware-writer-provider.json").is_file()),
            "blind_reader": int((config.output / "depth-aware-blind-reader-provider.json").is_file()),
            "vision": 0,
            "text_total": sum(int((config.output / name).is_file()) for name in ("section-depth-plan-provider.json", "depth-aware-writer-provider.json", "depth-aware-blind-reader-provider.json")),
        },
        "models": {"text": model.text_model, "vision": model.vision_model},
        "comparison": {
            "base_output": str(base_output),
            "base_note_characters": len(old_note),
            "depth_planned_note_characters": len(note),
            "uniform_length_quota_used": False,
        },
        "validation_issues": issues,
        "non_blocking_observations": non_blocking_observations,
        "blind_reader": {
            "overall": blind["overall"],
            "answers": {item["id"]: item["status"] for item in blind["answers"]},
            "critical_missing_information": blind["critical_missing_information"],
        },
        "html_check": html_check,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    v2._write(config, "reading-receipt.json", receipt)
    v2._write(config, "final-validation.json", {"status": status, "issues": issues, "non_blocking_observations": non_blocking_observations, "blind_reader": receipt["blind_reader"], "html_check": html_check})
    v2._inject_verdict(config, receipt, blind)
    print(json.dumps({"output": str(config.output), "status": status, "calls": calls, "comparison": receipt["comparison"], "issues": issues, "blind": receipt["blind_reader"]}, ensure_ascii=False, indent=2))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--material-root", required=True)
    parser.add_argument("--base-output", required=True)
    parser.add_argument("--output", required=True)
    return parser


def _call_cached(config: Any, model: Any, name: str, request: Mapping[str, Any], calls: dict[str, int]) -> Mapping[str, Any]:
    provider = config.output / f"{name}-provider.json"
    key = {"section-depth-plan": "section_depth_plan", "depth-aware-writer": "writer", "depth-aware-blind-reader": "blind_reader"}[name]
    if provider.is_file():
        return v2.v1._provider_response(provider)
    calls[key] += 1
    return base._call_json(model, name, request)


def _relevant_handles(paper_model: Mapping[str, Any], decisions: Sequence[Mapping[str, Any]], handles: Mapping[str, Any], blocks: Sequence[Any]) -> list[str]:
    values: list[str] = []
    for group in ("sections", "argument_chain", "experiments", "must_preserve_facts", "limitations"):
        for item in paper_model.get(group, []):
            values.extend(str(value) for value in item.get("evidence_handles", []))
    values.extend(str(item["handle"]) for item in decisions if item["decision"] == "inline")
    return v2._expand_handles(values, handles, blocks)


def _coverage_ledger(paper_model: Mapping[str, Any], decisions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ledger: list[dict[str, Any]] = []
    groups = (
        ("argument", "argument_chain"),
        ("experiment", "experiments"),
        ("fact", "must_preserve_facts"),
        ("limitation", "limitations"),
    )
    for prefix, group in groups:
        for index, item in enumerate(paper_model.get(group, [])):
            stable = str(item.get("key") or item.get("concept") or index).strip().replace(" ", "_")
            ledger.append({"obligation_id": f"{prefix}:{stable}", "kind": prefix, "payload": item})
    for item in decisions:
        if item["decision"] == "inline":
            ledger.append({"obligation_id": f"asset:{item['handle']}", "kind": "asset", "payload": item})
    return ledger


def _validate_depth_plan(raw: Mapping[str, Any], paper_model: Mapping[str, Any], handles: Mapping[str, Any], decisions: Sequence[Mapping[str, Any]], coverage_ledger: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if raw.get("_fallback"):
        raise RuntimeError(f"SectionDepthPlan failed: {raw}")
    body = base._response_body(raw, "reader_path", "sections", "cross_section_guardrails")
    expected = [str(item["section_id"]) for item in paper_model["sections"]]
    sections = body.get("sections")
    actual = [str(item.get("section_id", "")) for item in sections] if isinstance(sections, list) else []
    if actual != expected:
        raise RuntimeError(f"SectionDepthPlan sections mismatch: expected={expected}, actual={actual}")
    allowed_modes = {"orient", "reconstruct", "compare", "connect_evidence", "qualify"}
    allowed_handles = set(handles)
    inline_handles = {str(item["handle"]) for item in decisions if item["decision"] == "inline"}
    assigned_assets: list[str] = []
    expected_obligations = {str(item["obligation_id"]) for item in coverage_ledger}
    obligations_by_id = {str(item["obligation_id"]): item for item in coverage_ledger}
    assigned_obligations: list[str] = []
    for section in sections:
        if str(section.get("explanation_mode", "")) not in allowed_modes:
            raise RuntimeError(f"Invalid explanation mode: {section}")
        for key in ("answer_contract", "reasoning_steps", "stop_conditions", "evidence_handles"):
            values = section.get(key)
            if not isinstance(values, list) or not values:
                raise RuntimeError(f"SectionDepthPlan requires non-empty {key}: {section.get('section_id')}")
        section_handles = {str(value) for value in section["evidence_handles"]}
        if not section_handles.issubset(allowed_handles):
            raise RuntimeError(f"SectionDepthPlan invented evidence handles: {section_handles - allowed_handles}")
        for asset in section.get("asset_jobs", []):
            raw_handle = str(asset.get("handle", "")).strip()
            normalized_handle = raw_handle.removeprefix("inline ").strip()
            if normalized_handle not in inline_handles:
                raise RuntimeError(f"SectionDepthPlan invented inline asset handle: {raw_handle}")
            asset["handle"] = normalized_handle
            assigned_assets.append(normalized_handle)
        obligations = section.get("coverage_obligation_ids")
        if not isinstance(obligations, list) or not obligations:
            raise RuntimeError(f"SectionDepthPlan requires coverage obligations: {section.get('section_id')}")
        assigned_obligations.extend(str(value) for value in obligations)
    if set(assigned_assets) != inline_handles or len(assigned_assets) != len(set(assigned_assets)):
        raise RuntimeError(f"Inline asset jobs mismatch: expected={inline_handles}, actual={assigned_assets}")
    missing = expected_obligations - set(assigned_obligations)
    for obligation_id in list(missing):
        payload = obligations_by_id[obligation_id].get("payload", {})
        source_handles = {str(value) for value in payload.get("evidence_handles", [])}
        candidates = [section for section in sections if source_handles & {str(value) for value in section.get("evidence_handles", [])}]
        if candidates:
            candidates[0]["coverage_obligation_ids"].append(obligation_id)
            assigned_obligations.append(obligation_id)
    if set(assigned_obligations) != expected_obligations or len(assigned_obligations) != len(set(assigned_obligations)):
        missing = expected_obligations - set(assigned_obligations)
        invented = set(assigned_obligations) - expected_obligations
        duplicates = sorted({value for value in assigned_obligations if assigned_obligations.count(value) > 1})
        raise RuntimeError(f"Coverage obligations mismatch: missing={missing}, invented={invented}, duplicates={duplicates}")
    path = body.get("reader_path")
    if not isinstance(path, list) or [str(item.get("section_id", "")) for item in path] != expected:
        raise RuntimeError("Reader path must cover every section in order.")
    guardrails = body.get("cross_section_guardrails")
    if not isinstance(guardrails, list) or not guardrails:
        raise RuntimeError("SectionDepthPlan requires cross-section guardrails.")
    body["coverage_ledger"] = list(coverage_ledger)
    body["resolved_model"] = raw.get("_resolved_model")
    return body


def _blind_request(note: str, depth_plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "operation": "blind_reader_review_of_semantically_depth_planned_note",
        "reader_role": "You understand modern machine learning but have not read this paper. Use only the supplied Chinese note.",
        "note": note,
        "depth_contract_summary": [{"section_id": item["section_id"], "answer_contract": item["answer_contract"], "stop_conditions": item["stop_conditions"]} for item in depth_plan["sections"]],
        "questions": [
            {"id": "background_problem", "question": "研究背景、具体问题以及这个问题为什么重要是什么？"},
            {"id": "prior_gap", "question": "已有方法为什么不足？"},
            {"id": "core_solution", "question": "论文的核心思想和主要机制是什么，各部分如何连接？"},
            {"id": "formalism", "question": "论文最关键的公式、结构化定义或评估流程是什么？若论文没有关键公式，应说明真正承担解释作用的结构。"},
            {"id": "experiments", "question": "作者为什么做这些关键实验，设置、比较、精确结果、解释和边界分别是什么？"},
            {"id": "visual_evidence", "question": "关键图表分别解决了什么理解问题，而不只是展示了什么？"},
            {"id": "boundaries", "question": "结果不能说明什么，主要局限和适用边界是什么？"},
        ],
        "rules": [
            "Do not use outside knowledge. Mark information absent from the note as missing.",
            "Judge whether the narrative teaches the causal argument and experimental logic, not whether it is long.",
            "Rate every answer clear, partial, or missing; distinguish critical gaps from optional depth.",
        ],
        "return": {"answers": [{"id": "question id", "status": "clear|partial|missing", "answer": "from note", "missing_information": ["items"]}], "narrative": {"status": "clear|partial|fragmented", "reason": "reason"}, "critical_missing_information": ["items"], "optional_details": ["items"], "overall": "pass|needs_targeted_revision|fail", "overall_reason": "reason"},
    }


if __name__ == "__main__":
    raise SystemExit(main())
