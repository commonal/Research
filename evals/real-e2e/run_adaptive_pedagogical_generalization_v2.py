from __future__ import annotations

"""PROTOTYPE: paper-independent pedagogical reading with typed semantic assets."""

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Mapping, Sequence
import argparse
import base64
import importlib.util
import json
import re
import shutil
import sys

import markdown


ROOT = Path(__file__).resolve().parents[2]
V1_SCRIPT = ROOT / "evals" / "real-e2e" / "run_pedagogical_generalization_v1.py"


def _load_v1() -> Any:
    spec = importlib.util.spec_from_file_location("pedagogical_generalization_v1", V1_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load generalization v1 helpers.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


v1 = _load_v1()
base = v1.base
PaperCandidate = v1.PaperCandidate


@dataclass(frozen=True)
class ExperimentConfig:
    source_id: str
    title: str
    source_url: str
    material_root: Path
    output: Path


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = ExperimentConfig(
        source_id=args.source_id,
        title=args.title,
        source_url=args.source_url,
        material_root=Path(args.material_root).resolve(),
        output=Path(args.output).resolve(),
    )
    _bind(config)
    base._load_dotenv()
    config.output.mkdir(parents=True, exist_ok=True)
    if args.refresh_local_artifacts:
        return _refresh_local_artifacts(config)
    if args.deepen_by_groups:
        return _deepen_by_groups(config)
    if args.repair_facts_existing:
        return _repair_facts_existing(config)
    if args.repair_existing:
        return _repair_existing(config)
    if args.resume_after_writer:
        return _resume_after_writer(config)
    started = datetime.now(timezone.utc).isoformat()
    paper = v1._load_paper(PaperCandidate(config.source_id, config.title, config.source_url, "generalization"))
    blocks = _main_paper_blocks(paper.ordered_blocks)
    handles = {f"b{block.order}": block for block in blocks}
    paper_text = _paper_text(blocks)
    model = base.DeepSeekPaperReadingModel.from_environment()

    paper_model_provider = config.output / "generic-paper-model-provider.json"
    paper_model_raw = v1._provider_response(paper_model_provider) if paper_model_provider.is_file() else base._call_json(model, "generic-paper-model", {
        "operation": "build_paper_independent_evidence_typed_paper_model",
        "reader": "A technical reader unfamiliar with this particular paper and possibly unfamiliar with its prerequisite concepts.",
        "goal": "Infer the paper's own argument and contribution shape before deciding how to teach it. Do not use a fixed paper-type template.",
        "source_title": config.title,
        "material_type_counts": {kind: sum(block.kind == kind for block in blocks) for kind in ("paragraph", "formula", "figure", "table")},
        "ordered_paper": paper_text,
        "evidence_handle_rule": "Every [bN|...] marker is an allowed evidence handle. Return only those exact bN handles; never invent block IDs.",
        "rules": [
            "Classify the paper type from the paper itself and plan 5-9 Chinese teaching sections appropriate to that type.",
            "Preserve the chain from background and concrete problem through prior gap, central idea, mechanism, evaluation logic, conclusions, and boundaries when supported.",
            "Identify prerequisites that a reader needs, but separate source facts from pedagogical synthesis.",
            "Select only supplied formula handles that are necessary to understand the contribution; when formula_count is zero, key_formulas must be empty and the note must use the paper's actual structured objects instead.",
            "For every major experiment capture its question, setup, comparison, exact result, interpretation, and boundary.",
            "Use exact evidence handles for every source fact, formula, experiment, limitation, and must-preserve item.",
        ],
        "return": {
            "paper_type": "specific contribution shape",
            "central_problem": "plain explanation",
            "prior_gap": "why existing approaches are insufficient",
            "central_idea": "plain explanation",
            "argument_chain": [{"role": "role", "statement": "claim", "evidence_handles": ["bN"]}],
            "prerequisites": [{"concept": "concept", "why_needed": "reason", "explanation_boundary": "what may safely be explained"}],
            "sections": [{"section_id": "stable_snake_case", "heading": "Chinese heading", "reader_question": "question", "teaching_moves": ["moves"], "evidence_handles": ["bN"]}],
            "key_formulas": [{"handle": "bN", "role": "why this formula is essential", "section_id": "planned section"}],
            "experiments": [{"key": "key", "why": "why", "setup": ["details"], "comparison": ["groups"], "results": ["exact results"], "interpretation": "meaning", "boundary": "not established", "evidence_handles": ["bN"]}],
            "must_preserve_facts": [{"statement": "atomic fact", "numeric_tokens": ["exact"], "named_entities": ["exact"], "evidence_handles": ["bN"]}],
            "limitations": [{"statement": "boundary", "evidence_handles": ["bN"]}],
        },
    })
    paper_model = _validate_paper_model(paper_model_raw, handles)
    _write(config, "paper-model.json", paper_model)

    candidate_handles = [handle for handle, block in handles.items() if block.kind in {"formula", "figure", "table"}]
    asset_raw = base._call_json(model, "generic-semantic-asset-plan", {
        "operation": "select_explanatory_assets_without_paper_specific_rules",
        "paper_model": paper_model,
        "candidates": [_candidate(handle, handles[handle]) for handle in candidate_handles],
        "rules": [
            "Return one decision for every candidate handle: inline, reference, or omit.",
            "Select by explanatory value, completeness, indispensability, and non-redundancy; most assets should be omitted.",
            "Use inline for at most 4 formulas, 3 figures, and 3 tables; these are cost fuses, not quotas.",
            "A complete LaTeX formula or structured table never requires pixels.",
            "A figure requires pixels only when topology, axes, curves, or visual grouping adds meaning beyond caption and prose.",
            "Each inline decision must target one planned section_id and state the exact explanatory job it performs.",
            "Write supports and reason in Chinese because supports becomes a reader-facing asset title.",
        ],
        "return": {"decisions": [{"handle": "bN", "decision": "inline|reference|omit", "section_id": "planned section or empty", "supports": "short", "reason": "short", "requires_pixels": True}]},
    })
    decisions, fuse_events = _validate_asset_plan(asset_raw, candidate_handles, paper_model, handles)
    _write(config, "asset-plan.json", {"decisions": decisions, "fuse_events": fuse_events})

    neighborhoods = [_neighborhood(item, handles, blocks) for item in decisions if item["decision"] in {"inline", "reference"} and handles[item["handle"]].kind in {"formula", "table"}]
    definition_raw = base._call_json(model, "generic-definition-briefs", {
        "operation": "explain_selected_formula_and_table_definition_neighborhoods",
        "paper_model": _paper_model_summary(paper_model),
        "neighborhoods": neighborhoods,
        "rules": [
            "Explain each object only from its object content and supplied same-section neighborhood.",
            "For formulas, define only symbols explicitly defined or unambiguously used in the neighborhood; list unknown symbols instead of guessing.",
            "Explain the formula's role in the paper's mechanism before algebraic details.",
            "For tables, preserve metric-to-model-to-value relationships and explain the experimental question rather than reading every cell.",
            "Every definition and interpretation must cite one or more supplied evidence handles.",
            "Write role, plain_explanation, symbol meanings, and unknowns in Chinese for direct use in the Chinese note.",
        ],
        "return": {"briefs": [{"handle": "bN", "kind": "formula|table", "role": "role", "plain_explanation": "explanation", "definitions": [{"symbol_or_metric": "token", "meaning": "meaning", "evidence_handles": ["bN"]}], "unknowns": ["unknown"], "evidence_handles": ["bN"]}]},
    })
    briefs = _validate_briefs(definition_raw, neighborhoods, handles)
    _write(config, "definition-briefs.json", {"briefs": briefs})

    visuals: dict[str, Any] = {}
    for item in decisions:
        block = handles[item["handle"]]
        if item["decision"] != "inline" or block.kind != "figure" or not item["requires_pixels"]:
            continue
        if not block.image_path or not Path(block.image_path).is_file():
            visuals[item["handle"]] = {"status": "failed", "reason": "safe local image unavailable"}
            continue
        raw = base._call_json(model, f"generic-visual-{len(visuals) + 1}", {
            "operation": "inspect_selected_figure_for_planned_explanation",
            "paper_type": paper_model["paper_type"],
            "active_section": item["section_id"],
            "explanatory_job": item["supports"],
            "caption": block.caption or block.text,
            "neighboring_context": _neighbor_text(block, blocks),
            "rules": [
                "Separate directly visible relations from caption-only claims.",
                "Describe only axes, topology, arrows, components, curves, or comparisons needed for the explanatory job.",
                "Do not infer illegible values; list uncertainty explicitly.",
            ],
            "return": {"visual_summary": "summary", "observed_relations": ["relations"], "caption_only_claims": ["claims"], "uncertainties": ["unknowns"]},
        }, image=block.image_path)
        visuals[item["handle"]] = {"status": "success" if not raw.get("_fallback") else "failed", **{key: value for key, value in raw.items() if not key.startswith("_")}}
    _write(config, "visual-results.json", visuals)

    writer_raw = base._call_json(model, "generic-structured-writer", {
        "operation": "write_paper_independent_pedagogical_chinese_note",
        "goal": "Write a standalone Chinese deep-reading note that a technical reader can follow without opening the paper.",
        "paper_model": paper_model,
        "definition_briefs": briefs,
        "visual_interpretations": visuals,
        "selected_assets": [item for item in decisions if item["decision"] == "inline"],
        "ordered_paper": paper_text,
        "rules": [
            "Return one section for every planned section_id in exactly the planned order, without headings inside markdown.",
            "Start from why the problem matters, add only necessary prerequisites, and use natural causal transitions instead of summary fragments.",
            "Explain the paper's own central mechanism and hierarchy; do not force a benchmark, RL, security, or Transformer template.",
            "Use every inline formula only where it resolves a concrete conceptual question. Explain its role, inputs, transformation, output, and explicitly supported symbol meanings; preserve unknown symbols as unknown.",
            "For each major experiment explain the question, setup, comparisons, exact result, interpretation, and what it cannot establish.",
            "Explain selected figures and tables at their semantic section but do not emit asset markers or reproduce their raw content; the renderer inserts them.",
            "Preserve must-preserve facts and exact reported numbers. Do not introduce a number, named mechanism, formula definition, or table interpretation outside the supplied paper and briefs.",
            "Keep limitations close to the claims they qualify and finish with a restrained statement of what the work proves and does not prove.",
            "Do not expose evidence handles, block IDs, parser/provider names, prompts, or local paths.",
        ],
        "length_guidance": "Use the length needed for comprehension, normally 5500-9000 Chinese characters; do not pad to a quota.",
        "return": {"sections": [{"section_id": "exact planned id", "markdown": "connected Chinese prose"}]},
    })
    sections = _validate_sections(writer_raw, paper_model)
    _write(config, "writer-output.json", {"sections": sections, "resolved_model": writer_raw.get("_resolved_model")})
    assets = _publish_assets(config, decisions, handles)
    note = _render(config, sections, paper_model, decisions, handles, briefs, assets)
    (config.output / "reading-note.md").write_text(note + "\n", encoding="utf-8")
    issues = _validate_note(note, paper_model, blocks, decisions, briefs, assets)
    html_check = _write_html(config, note, paper_model, decisions, briefs, visuals, issues, assets)

    blind_raw = base._call_json(model, "generic-blind-reader", {
        "operation": "blind_reader_review_of_pedagogical_paper_note",
        "reader_role": "You understand modern machine learning but have not read this paper. Use only the supplied Chinese note.",
        "note": note,
        "questions": [
            {"id": "background_problem", "question": "研究背景、具体问题以及这个问题为什么重要是什么？"},
            {"id": "prior_gap", "question": "已有方法为什么不足？"},
            {"id": "core_solution", "question": "论文的核心思想和主要机制是什么，各部分如何连接？"},
            {"id": "formalism", "question": "论文最关键的公式、结构化定义或评估流程是什么？若论文没有关键公式，应说明真正承担解释作用的结构，而不是臆造公式。"},
            {"id": "experiments", "question": "作者做了哪些关键实验，设置、比较、精确结果和含义是什么？"},
            {"id": "visual_evidence", "question": "笔记中的关键图表分别帮助理解什么？"},
            {"id": "boundaries", "question": "结果不能说明什么，主要局限和适用边界是什么？"},
        ],
        "rules": [
            "Do not use outside knowledge. Mark information absent from the note as missing.",
            "Rate every answer clear, partial, or missing; distinguish critical gaps from optional depth.",
            "Do not pass merely because the note is long or polished.",
        ],
        "return": {"answers": [{"id": "question id", "status": "clear|partial|missing", "answer": "from note", "missing_information": ["items"]}], "narrative": {"status": "clear|partial|fragmented", "reason": "reason"}, "critical_missing_information": ["items"], "optional_details": ["items"], "overall": "pass|needs_targeted_revision|fail", "overall_reason": "reason"},
    })
    blind = _validate_blind(blind_raw)
    _write(config, "blind-reader-review.json", blind)

    status = "generalization_passed" if not issues and html_check["passed"] and blind["overall"] == "pass" and not blind["critical_missing_information"] else "needs_review"
    receipt = {
        "source_id": config.source_id,
        "title": config.title,
        "route": "generic_full_ordered_paper_model_to_semantic_asset_plan_to_definition_neighborhoods_to_structured_writer",
        "status": status,
        "paper_type": paper_model["paper_type"],
        "material": {"blocks": len(blocks), "characters": len(paper_text), **{f"{kind}_blocks": sum(block.kind == kind for block in blocks) for kind in ("paragraph", "figure", "table", "formula")}},
        "model_calls": {"text": model.text_call_count, "vision": model.vision_call_count},
        "models": {"text": model.text_model, "vision": model.vision_model},
        "asset_counts": {value: sum(item["decision"] == value for item in decisions) for value in ("inline", "reference", "omit")},
        "inline_kind_counts": {kind: sum(item["decision"] == "inline" and handles[item["handle"]].kind == kind for item in decisions) for kind in ("formula", "figure", "table")},
        "fuse_events": fuse_events,
        "validation_issues": issues,
        "must_preserve_degradations": _must_preserve_degradations(paper_model, blocks),
        "blind_reader": {"overall": blind["overall"], "answers": {item["id"]: item["status"] for item in blind["answers"]}, "critical_missing_information": blind["critical_missing_information"]},
        "html_check": html_check,
        "started_at": started,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    _write(config, "reading-receipt.json", receipt)
    _write(config, "final-validation.json", {"status": status, "issues": issues, "blind_reader": receipt["blind_reader"], "html_check": html_check})
    _inject_verdict(config, receipt, blind)
    print(json.dumps({"output": str(config.output), "status": status, "paper_type": paper_model["paper_type"], "issues": issues, "blind_reader": receipt["blind_reader"], "calls": receipt["model_calls"]}, ensure_ascii=False, indent=2))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--material-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--resume-after-writer", action="store_true")
    parser.add_argument("--repair-existing", action="store_true")
    parser.add_argument("--repair-facts-existing", action="store_true")
    parser.add_argument("--deepen-by-groups", action="store_true")
    parser.add_argument("--refresh-local-artifacts", action="store_true")
    return parser


def _refresh_local_artifacts(config: ExperimentConfig) -> int:
    """Re-render and revalidate existing model state without another model call."""
    paper = v1._load_paper(PaperCandidate(config.source_id, config.title, config.source_url, "generalization"))
    blocks = _main_paper_blocks(paper.ordered_blocks)
    handles = {f"b{block.order}": block for block in blocks}
    paper_model = _read(config.output / "paper-model.json")
    decisions = _read(config.output / "asset-plan.json")["decisions"]
    briefs = _read(config.output / "definition-briefs.json")["briefs"]
    visuals = _read(config.output / "visual-results.json")
    sections = _read(config.output / "writer-output.json")["sections"]
    assets = _publish_assets(config, decisions, handles)
    note = _render(config, sections, paper_model, decisions, handles, briefs, assets)
    (config.output / "reading-note.md").write_text(note + "\n", encoding="utf-8")
    issues = _validate_note(note, paper_model, blocks, decisions, briefs, assets)
    html_check = _write_html(config, note, paper_model, decisions, briefs, visuals, issues, assets)
    receipt = _read(config.output / "reading-receipt.json")
    blind = _read(config.output / "blind-reader-review.json")
    status = "generalization_passed" if not issues and html_check["passed"] and blind.get("overall") == "pass" and not blind.get("critical_missing_information") else "needs_review"
    receipt["status"] = status
    receipt["validation_issues"] = issues
    receipt["html_check"] = html_check
    receipt["model_calls"] = _model_call_receipt(config, str(_read(config.output / "writer-output.json").get("revision", "initial")))
    receipt["local_artifacts_refreshed_at"] = datetime.now(timezone.utc).isoformat()
    _write(config, "reading-receipt.json", receipt)
    _write(config, "final-validation.json", {"status": status, "issues": issues, "blind_reader": receipt.get("blind_reader", {}), "html_check": html_check})
    _inject_verdict(config, receipt, blind)
    print(json.dumps({"status": status, "issues": issues, "html_check": html_check, "model_calls_added": 0}, ensure_ascii=False, indent=2))
    return 0


def _deepen_by_groups(config: ExperimentConfig) -> int:
    """Deepen two coherent section groups with only their evidence neighborhoods."""
    paper = v1._load_paper(PaperCandidate(config.source_id, config.title, config.source_url, "generalization"))
    blocks = _main_paper_blocks(paper.ordered_blocks)
    handles = {f"b{block.order}": block for block in blocks}
    model_state = _read(config.output / "paper-model.json")
    current_payload = _read(config.output / "writer-output.json")
    current = current_payload["sections"]
    current_by_id = {str(item["section_id"]): item for item in current}
    section_plans = model_state["sections"]
    planned_ids = [str(item["section_id"]) for item in section_plans]
    split = (len(planned_ids) + 1) // 2
    groups = [planned_ids[:split], planned_ids[split:]]
    revised_by_id: dict[str, dict[str, str]] = {}
    model = base.DeepSeekPaperReadingModel.from_environment()
    for index, section_ids in enumerate(groups, start=1):
        evidence_handles: list[str] = []
        for plan in section_plans:
            if str(plan["section_id"]) in section_ids:
                evidence_handles.extend(str(value) for value in plan.get("evidence_handles", []))
        linked_experiments = []
        for experiment in model_state.get("experiments", []):
            experiment_handles = [str(value) for value in experiment.get("evidence_handles", [])]
            if set(experiment_handles) & set(evidence_handles):
                linked_experiments.append(experiment)
                evidence_handles.extend(experiment_handles)
        expanded_handles = _expand_handles(evidence_handles, handles, blocks)
        provider = config.output / f"generic-deepen-group-{index}-provider.json"
        raw = v1._provider_response(provider) if provider.is_file() else base._call_json(model, f"generic-deepen-group-{index}", {
            "operation": "deepen_one_coherent_group_of_a_paper_independent_chinese_note",
            "paper_type": model_state["paper_type"],
            "global_argument": _paper_model_summary(model_state),
            "group_section_plans": [item for item in section_plans if str(item["section_id"]) in section_ids],
            "current_group_sections": [current_by_id[section_id] for section_id in section_ids],
            "linked_experiments": linked_experiments,
            "evidence_neighborhood": [_candidate(handle, handles[handle]) for handle in expanded_handles],
            "rules": [
                "Return exactly the requested section_ids in order, each as complete Chinese markdown without headings.",
                "Aim for roughly 550-950 Chinese characters per section only when evidence supports that depth; never pad with generic history or repeated conclusions.",
                "Answer the section's reader_question, introduce needed prerequisites, explain causal links, and transition naturally to the next section.",
                "For experiments cover purpose, setup, comparison, exact result, interpretation, and boundary. Keep judge-validation metrics distinct from evaluated-system performance.",
                "Preserve exact supported quantities and stable names. Do not expose evidence handles or add facts beyond the supplied neighborhood.",
            ],
            "return": {"sections": [{"section_id": "requested id", "markdown": "complete deepened Chinese section"}]},
        })
        body = base._response_body(raw, "sections")
        values = body.get("sections")
        actual = [str(item.get("section_id", "")) for item in values] if isinstance(values, list) else []
        if actual != section_ids:
            raise RuntimeError(f"Deepen group {index} mismatch: expected={section_ids}, actual={actual}")
        for item in values:
            value = str(item.get("markdown", "")).strip()
            if len(value) < 320:
                raise RuntimeError(f"Deepen group {index} remains too compressed: {item.get('section_id')}={len(value)}")
            revised_by_id[str(item["section_id"])] = {"section_id": str(item["section_id"]), "markdown": value}
    revised = [revised_by_id[section_id] for section_id in planned_ids]
    _write(config, "writer-output.json", {"sections": revised, "resolved_model": model.text_model, "revision": "two_group_evidence_neighborhood_deepening"})
    return _resume_after_writer(config)


def _expand_handles(values: Sequence[str], handles: Mapping[str, Any], blocks: Sequence[Any]) -> list[str]:
    position = {block.block_id: index for index, block in enumerate(blocks)}
    result: list[str] = []
    for handle in dict.fromkeys(values):
        if handle not in handles:
            continue
        index = position[handles[handle].block_id]
        for block in blocks[max(0, index - 1): min(len(blocks), index + 2)]:
            result.append(f"b{block.order}")
    return list(dict.fromkeys(result))


def _repair_facts_existing(config: ExperimentConfig) -> int:
    """Patch only sections implicated by failed must-preserve fact checks."""
    paper = v1._load_paper(PaperCandidate(config.source_id, config.title, config.source_url, "generalization"))
    blocks = _main_paper_blocks(paper.ordered_blocks)
    handles = {f"b{block.order}": block for block in blocks}
    paper_model = _read(config.output / "paper-model.json")
    sections_payload = _read(config.output / "writer-output.json")
    sections = sections_payload["sections"]
    decisions = _read(config.output / "asset-plan.json")["decisions"]
    briefs = _read(config.output / "definition-briefs.json")["briefs"]
    assets = _publish_assets(config, decisions, handles)
    current_note = _render(config, sections, paper_model, decisions, handles, briefs, assets)
    failed_statements = {
        issue["detail"] for issue in _validate_note(current_note, paper_model, blocks, decisions, briefs, assets)
        if issue["type"] == "missing_must_preserve_fact"
    }
    failed_facts = [item for item in paper_model.get("must_preserve_facts", []) if str(item.get("statement", "")) in failed_statements]
    if not failed_facts:
        return _resume_after_writer(config)
    evidence = [{"fact": fact, "source_blocks": [_candidate(handle, handles[handle]) for handle in fact.get("evidence_handles", [])]} for fact in failed_facts]
    model = base.DeepSeekPaperReadingModel.from_environment()
    provider = config.output / "generic-fact-repair-provider.json"
    raw = v1._provider_response(provider) if provider.is_file() else base._call_json(model, "generic-fact-repair", {
        "operation": "patch_only_sections_missing_evidence_backed_must_preserve_facts",
        "paper_type": paper_model["paper_type"],
        "section_plan": paper_model["sections"],
        "current_sections": sections,
        "failed_fact_evidence": evidence,
        "rules": [
            "Return only section patches needed to express every failed fact; preserve all other content in each patched section.",
            "Use natural Chinese but retain stable model/mechanism names from the fact and exact quantities so the claim is auditable.",
            "The fact's entities and quantities must occur together in one paragraph; do not attach them to an unrelated comparison.",
            "Use only the supplied source blocks. Do not add experiments, numbers, causal claims, headings, or evidence handles.",
        ],
        "return": {"section_patches": [{"section_id": "existing section id", "markdown": "complete revised section markdown"}]},
    })
    if raw.get("_fallback"):
        raise RuntimeError(f"Fact repair failed: {raw}")
    body = base._response_body(raw, "section_patches")
    patches = body.get("section_patches")
    by_id = {str(item["section_id"]): str(item.get("markdown", "")).strip() for item in patches} if isinstance(patches, list) else {}
    valid_ids = {str(item["section_id"]) for item in paper_model["sections"]}
    if not by_id or not set(by_id).issubset(valid_ids) or any(not value for value in by_id.values()):
        raise RuntimeError(f"Invalid fact repair patches: {by_id.keys()}")
    revised = [{"section_id": item["section_id"], "markdown": by_id.get(str(item["section_id"]), str(item["markdown"]))} for item in sections]
    revised_note = _render(config, revised, paper_model, decisions, handles, briefs, assets)
    remaining = [item for item in _validate_note(revised_note, paper_model, blocks, decisions, briefs, assets) if item["type"] == "missing_must_preserve_fact"]
    if remaining:
        remaining_statements = {item["detail"] for item in remaining}
        remaining_facts = [item for item in failed_facts if str(item.get("statement", "")) in remaining_statements]
        exact_provider = config.output / "generic-exact-fact-repair-provider.json"
        exact_raw = v1._provider_response(exact_provider) if exact_provider.is_file() else base._call_json(model, "generic-exact-fact-repair", {
            "operation": "one_retry_to_preserve_exact_named_mechanism_and_quantity",
            "current_sections": revised,
            "failed_facts": remaining_facts,
            "source_evidence": [{"fact": fact, "source_blocks": [_candidate(handle, handles[handle]) for handle in fact.get("evidence_handles", [])]} for fact in remaining_facts],
            "rules": [
                "Return only complete revised sections needed for the failed facts.",
                "Include every supplied named_entity exactly once, optionally followed by a Chinese explanation, and express every numeric_token exactly or with an explicit equivalent such as x=倍.",
                "Named entities and quantities for one fact must occur together in one paragraph.",
                "Preserve all existing supported content; do not add any other fact or number.",
            ],
            "return": {"section_patches": [{"section_id": "existing section id", "markdown": "complete revised section markdown"}]},
        })
        exact_body = base._response_body(exact_raw, "section_patches")
        exact_values = exact_body.get("section_patches")
        exact_by_id = {str(item["section_id"]): str(item.get("markdown", "")).strip() for item in exact_values} if isinstance(exact_values, list) else {}
        if not exact_by_id or not set(exact_by_id).issubset(valid_ids) or any(not value for value in exact_by_id.values()):
            raise RuntimeError(f"Invalid exact fact repair patches: {exact_by_id.keys()}")
        revised = [{"section_id": item["section_id"], "markdown": exact_by_id.get(str(item["section_id"]), str(item["markdown"]))} for item in revised]
        revised_note = _render(config, revised, paper_model, decisions, handles, briefs, assets)
        remaining = [item for item in _validate_note(revised_note, paper_model, blocks, decisions, briefs, assets) if item["type"] == "missing_must_preserve_fact"]
        if remaining:
            raise RuntimeError(f"Exact fact repair did not satisfy must-preserve facts: {remaining}")
    _write(config, "writer-output.json", {"sections": revised, "resolved_model": raw.get("_resolved_model"), "revision": "targeted_must_preserve_fact_repair"})
    return _resume_after_writer(config)


def _repair_existing(config: ExperimentConfig) -> int:
    """Run one bounded evidence-preserving repair, then repeat blind acceptance."""
    paper = v1._load_paper(PaperCandidate(config.source_id, config.title, config.source_url, "generalization"))
    blocks = _main_paper_blocks(paper.ordered_blocks)
    handles = {f"b{block.order}": block for block in blocks}
    paper_model = _read(config.output / "paper-model.json")
    decisions_payload = _read(config.output / "asset-plan.json")
    decisions = decisions_payload["decisions"]
    briefs_payload = _read(config.output / "definition-briefs.json")
    briefs = briefs_payload["briefs"]
    sections = _read(config.output / "writer-output.json")["sections"]
    fact_evidence = []
    for fact in paper_model.get("must_preserve_facts", []):
        fact_evidence.append({
            "fact": fact,
            "evidence": [_candidate(handle, handles[handle]) for handle in fact.get("evidence_handles", [])],
        })
    model = base.DeepSeekPaperReadingModel.from_environment()
    repair_provider = config.output / "generic-grounded-repair-provider.json"
    raw = v1._provider_response(repair_provider) if repair_provider.is_file() else base._call_json(model, "generic-grounded-repair", {
        "operation": "repair_grounded_note_and_localize_semantic_asset_explanations",
        "paper_type": paper_model["paper_type"],
        "planned_sections": paper_model["sections"],
        "current_sections": sections,
        "current_note_characters": sum(len(str(item.get("markdown", ""))) for item in sections),
        "must_preserve_fact_evidence": fact_evidence,
        "inline_assets": [item for item in decisions if item["decision"] == "inline"],
        "definition_briefs": briefs,
        "rules": [
            "Return exactly the same sections in the same order; revise only where needed to preserve omitted facts or improve transitions.",
            "When the current note is compressed, deepen underdeveloped sections from their reader_question, teaching_moves, and supplied evidence. Add prerequisite explanation and causal transitions, not generic padding.",
            "For every planned experiment retain why it was run, setup and comparison, exact result, interpretation, and boundary. Do not collapse experimental design into a result list.",
            "A section may remain short when its source evidence is genuinely limited; length is not a quota, but the whole note must be independently understandable.",
            "Express every must-preserve fact in natural Chinese with exact quantities. Keep stable mechanism/model names such as the supplied names once in English alongside Chinese when needed for auditability.",
            "Do not add facts, numbers, experiments, or definitions beyond the supplied evidence and briefs.",
            "Return one concise Chinese title for every inline asset; do not include block handles or unsupported numbers.",
            "Return a Chinese explanatory paragraph for every selected formula brief. Preserve its supported meaning and explicit unknowns; do not guess symbols.",
            "Do not emit headings inside section markdown, asset markers, evidence handles, parser/provider names, or local paths.",
        ],
        "return": {
            "sections": [{"section_id": "exact planned id", "markdown": "revised Chinese prose"}],
            "asset_titles": [{"handle": "every inline handle", "title": "Chinese explanatory title"}],
            "formula_explanations": [{"handle": "every inline formula handle", "explanation": "Chinese grounded explanation"}],
        },
    })
    revised_sections = _validate_sections(raw, paper_model)
    body = base._response_body(raw, "sections", "asset_titles", "formula_explanations")
    inline_handles = {str(item["handle"]) for item in decisions if item["decision"] == "inline"}
    titles = {str(item.get("handle", "")): str(item.get("title", "")).strip() for item in body.get("asset_titles", []) if isinstance(item, Mapping)}
    if set(titles) != inline_handles or any(not value for value in titles.values()):
        raise RuntimeError(f"Repair asset title mismatch: expected={inline_handles}, actual={set(titles)}")
    formula_handles = {str(item["handle"]) for item in decisions if item["decision"] == "inline" and handles[str(item["handle"])].kind == "formula"}
    explanations = {str(item.get("handle", "")): str(item.get("explanation", "")).strip() for item in body.get("formula_explanations", []) if isinstance(item, Mapping)}
    if not formula_handles.issubset(explanations) or any(not explanations[handle] for handle in formula_handles):
        raise RuntimeError(f"Repair formula explanation mismatch: expected at least={formula_handles}, actual={set(explanations)}")
    for item in decisions:
        handle = str(item["handle"])
        if handle in titles:
            item["supports"] = titles[handle]
    for brief in briefs:
        handle = str(brief["handle"])
        if handle in explanations:
            brief["plain_explanation"] = explanations[handle]
    _write(config, "asset-plan.json", {**decisions_payload, "decisions": decisions})
    _write(config, "definition-briefs.json", {**briefs_payload, "briefs": briefs})
    _write(config, "writer-output.json", {"sections": revised_sections, "resolved_model": raw.get("_resolved_model"), "revision": "bounded_grounded_repair"})
    return _resume_after_writer(config)


def _resume_after_writer(config: ExperimentConfig) -> int:
    """Resume a run that completed model writing but failed in local rendering."""
    paper = v1._load_paper(PaperCandidate(config.source_id, config.title, config.source_url, "generalization"))
    blocks = _main_paper_blocks(paper.ordered_blocks)
    handles = {f"b{block.order}": block for block in blocks}
    paper_model = _read(config.output / "paper-model.json")
    decisions = _read(config.output / "asset-plan.json")["decisions"]
    briefs = _read(config.output / "definition-briefs.json")["briefs"]
    visuals = _read(config.output / "visual-results.json")
    sections = _read(config.output / "writer-output.json")["sections"]
    assets = _publish_assets(config, decisions, handles)
    note = _render(config, sections, paper_model, decisions, handles, briefs, assets)
    (config.output / "reading-note.md").write_text(note + "\n", encoding="utf-8")
    issues = _validate_note(note, paper_model, blocks, decisions, briefs, assets)
    html_check = _write_html(config, note, paper_model, decisions, briefs, visuals, issues, assets)
    model = base.DeepSeekPaperReadingModel.from_environment()
    blind_raw = base._call_json(model, "generic-blind-reader", {
        "operation": "blind_reader_review_of_pedagogical_paper_note",
        "reader_role": "You understand modern machine learning but have not read this paper. Use only the supplied Chinese note.",
        "note": note,
        "questions": [
            {"id": "background_problem", "question": "研究背景、具体问题以及这个问题为什么重要是什么？"},
            {"id": "prior_gap", "question": "已有方法为什么不足？"},
            {"id": "core_solution", "question": "论文的核心思想和主要机制是什么，各部分如何连接？"},
            {"id": "formalism", "question": "论文最关键的公式、结构化定义或评估流程是什么？若论文没有关键公式，应说明真正承担解释作用的结构，而不是臆造公式。"},
            {"id": "experiments", "question": "作者做了哪些关键实验，设置、比较、精确结果和含义是什么？"},
            {"id": "visual_evidence", "question": "笔记中的关键图表分别帮助理解什么？"},
            {"id": "boundaries", "question": "结果不能说明什么，主要局限和适用边界是什么？"},
        ],
        "rules": [
            "Do not use outside knowledge. Mark information absent from the note as missing.",
            "Rate every answer clear, partial, or missing; distinguish critical gaps from optional depth.",
            "Do not pass merely because the note is long or polished.",
        ],
        "return": {"answers": [{"id": "question id", "status": "clear|partial|missing", "answer": "from note", "missing_information": ["items"]}], "narrative": {"status": "clear|partial|fragmented", "reason": "reason"}, "critical_missing_information": ["items"], "optional_details": ["items"], "overall": "pass|needs_targeted_revision|fail", "overall_reason": "reason"},
    })
    blind = _validate_blind(blind_raw)
    _write(config, "blind-reader-review.json", blind)
    status = "generalization_passed" if not issues and html_check["passed"] and blind["overall"] == "pass" and not blind["critical_missing_information"] else "needs_review"
    receipt = {
        "source_id": config.source_id,
        "title": config.title,
        "route": "generic_full_ordered_paper_model_to_semantic_asset_plan_to_definition_neighborhoods_to_structured_writer",
        "status": status,
        "paper_type": paper_model["paper_type"],
        "material": {"blocks": len(blocks), "characters": len(_paper_text(blocks)), **{f"{kind}_blocks": sum(block.kind == kind for block in blocks) for kind in ("paragraph", "figure", "table", "formula")}},
        "model_calls": _model_call_receipt(config, str(_read(config.output / "writer-output.json").get("revision", "initial"))),
        "models": {"text": model.text_model, "vision": model.vision_model},
        "asset_counts": {value: sum(item["decision"] == value for item in decisions) for value in ("inline", "reference", "omit")},
        "inline_kind_counts": {kind: sum(item["decision"] == "inline" and handles[item["handle"]].kind == kind for item in decisions) for kind in ("formula", "figure", "table")},
        "fuse_events": _read(config.output / "asset-plan.json").get("fuse_events", []),
        "validation_issues": issues,
        "must_preserve_degradations": _must_preserve_degradations(paper_model, blocks),
        "blind_reader": {"overall": blind["overall"], "answers": {item["id"]: item["status"] for item in blind["answers"]}, "critical_missing_information": blind["critical_missing_information"]},
        "html_check": html_check,
        "resumed_after_local_render_failure": True,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    _write(config, "reading-receipt.json", receipt)
    _write(config, "final-validation.json", {"status": status, "issues": issues, "blind_reader": receipt["blind_reader"], "html_check": html_check})
    _inject_verdict(config, receipt, blind)
    print(json.dumps({"output": str(config.output), "status": status, "issues": issues, "blind_reader": receipt["blind_reader"], "calls": receipt["model_calls"]}, ensure_ascii=False, indent=2))
    return 0


def _bind(config: ExperimentConfig) -> None:
    v1.SOURCE_ID = config.source_id
    v1.TITLE = config.title
    v1.SOURCE_URL = config.source_url
    v1.MATERIAL_ROOT = config.material_root
    v1.NORMALIZED = config.material_root / "normalized" / "blocks.jsonl"
    v1.OUTPUT = config.output
    v1.base.OUTPUT = config.output


def _model_call_receipt(config: ExperimentConfig, writer_revision: str) -> dict[str, int]:
    grounded = int((config.output / "generic-grounded-repair-provider.json").is_file())
    fact = int((config.output / "generic-fact-repair-provider.json").is_file())
    exact = int((config.output / "generic-exact-fact-repair-provider.json").is_file())
    group_attempts = len(list(config.output.glob("generic-deepen-group-*-provider.json")))
    group_applied = group_attempts if writer_revision == "two_group_evidence_neighborhood_deepening" else 0
    failed_groups = group_attempts - group_applied
    blind = 3 if group_applied or fact else 2 if grounded else 1
    return {
        "paper_model": 1,
        "asset_plan": 1,
        "definition_briefs": 1,
        "writer": 1,
        "grounded_repair": grounded,
        "fact_repair": fact,
        "exact_fact_retry": exact,
        "group_deepening_applied": group_applied,
        "failed_group_deepening": failed_groups,
        "blind_reader": blind,
        "text_total_including_failed_and_superseded": 4 + grounded + fact + exact + group_attempts + blind,
        "vision": len(list(config.output.glob("generic-visual-*-provider.json"))),
    }


def _main_paper_blocks(blocks: Sequence[Any]) -> list[Any]:
    result = []
    seen: set[tuple[str, str]] = set()
    for block in blocks:
        section = str(block.section or "")
        if v1._is_appendix(section) or section.casefold() in {"references", "acknowledgements", "acknowledgments"}:
            continue
        value = re.sub(r"\s+", " ", block.latex or block.table_html or block.caption or block.text).strip()
        key = (block.kind, value.casefold())
        if not value or key in seen:
            continue
        seen.add(key)
        result.append(block)
    return result


def _paper_text(blocks: Sequence[Any]) -> str:
    parts = []
    for block in blocks:
        value = re.sub(r"\s+", " ", block.latex or block.table_html or block.caption or block.text).strip()
        limit = 8000 if block.kind in {"formula", "table"} else 4500
        parts.append(f"[b{block.order}|{block.kind}|{block.section}]\n{value[:limit]}")
    return "\n\n".join(parts)


def _handles(value: Any, allowed: Mapping[str, Any]) -> list[str]:
    values = value if isinstance(value, list) else []
    result = [str(item) for item in values]
    invalid = [item for item in result if item not in allowed]
    if invalid:
        raise RuntimeError(f"Unknown evidence handles: {invalid[:10]}")
    return list(dict.fromkeys(result))


def _validate_paper_model(raw: Mapping[str, Any], allowed: Mapping[str, Any]) -> dict[str, Any]:
    if raw.get("_fallback"):
        raise RuntimeError(f"PaperModel failed: {raw}")
    body = base._response_body(raw, "sections", "argument_chain")
    sections = body.get("sections")
    if not isinstance(sections, list) or not 5 <= len(sections) <= 9:
        raise RuntimeError("PaperModel must plan 5-9 sections.")
    ids = [str(item.get("section_id", "")) for item in sections if isinstance(item, Mapping)]
    if len(ids) != len(sections) or len(set(ids)) != len(ids) or any(not item for item in ids):
        raise RuntimeError(f"Invalid section IDs: {ids}")
    cleaned = dict(body)
    for group in ("argument_chain", "sections", "experiments", "must_preserve_facts", "limitations"):
        values = cleaned.get(group, [])
        if not isinstance(values, list):
            raise RuntimeError(f"PaperModel field must be list: {group}")
        for item in values:
            if isinstance(item, dict):
                item["evidence_handles"] = _handles(item.get("evidence_handles"), allowed)
    formulas = cleaned.get("key_formulas", [])
    if not isinstance(formulas, list):
        raise RuntimeError("key_formulas must be a list")
    valid_formulas = []
    plan_degradations = []
    for item in formulas:
        handle = str(item.get("handle", ""))
        if handle not in allowed:
            raise RuntimeError(f"Invalid formula handle: {handle}")
        if allowed[handle].kind != "formula":
            plan_degradations.append({"type": "non_formula_planned_as_formula", "handle": handle, "actual_kind": allowed[handle].kind, "action": "removed_from_key_formulas"})
            continue
        if str(item.get("section_id", "")) not in ids:
            raise RuntimeError(f"Formula has invalid section: {item}")
        valid_formulas.append(item)
    cleaned["key_formulas"] = valid_formulas
    cleaned["plan_degradations"] = plan_degradations
    cleaned["resolved_model"] = raw.get("_resolved_model")
    return cleaned


def _candidate(handle: str, block: Any) -> dict[str, Any]:
    value = block.latex or block.table_html or block.caption or block.text
    return {"handle": handle, "kind": block.kind, "section": block.section, "content": re.sub(r"\s+", " ", value).strip()[:3500], "has_real_image": bool(block.kind == "figure" and block.image_path and Path(block.image_path).is_file())}


def _validate_asset_plan(raw: Mapping[str, Any], expected: list[str], paper_model: Mapping[str, Any], handles: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if raw.get("_fallback"):
        raise RuntimeError(f"Asset plan failed: {raw}")
    body = base._response_body(raw, "decisions")
    values = body.get("decisions")
    by_handle = {str(item.get("handle", "")): dict(item) for item in values} if isinstance(values, list) else {}
    if set(by_handle) != set(expected):
        raise RuntimeError(f"Asset plan mismatch missing={set(expected)-set(by_handle)} extra={set(by_handle)-set(expected)}")
    section_ids = {str(item["section_id"]) for item in paper_model["sections"]}
    caps = {"formula": 4, "figure": 3, "table": 3}
    counts = {key: 0 for key in caps}
    events: list[dict[str, str]] = []
    decisions = []
    for handle in expected:
        item = by_handle[handle]
        decision = str(item.get("decision", ""))
        section_id = str(item.get("section_id", ""))
        if decision not in {"inline", "reference", "omit"}:
            raise RuntimeError(f"Invalid decision: {handle}={decision}")
        if decision == "inline" and section_id not in section_ids:
            raise RuntimeError(f"Invalid inline section: {handle}={section_id}")
        kind = handles[handle].kind
        if decision == "inline":
            counts[kind] += 1
            if counts[kind] > caps[kind]:
                decision = "reference"
                section_id = ""
                events.append({"handle": handle, "event": f"inline_to_reference_{kind}_fuse"})
        requires_pixels = bool(item.get("requires_pixels", False)) if kind == "figure" else False
        decisions.append({"handle": handle, "decision": decision, "section_id": section_id if decision == "inline" else "", "supports": str(item.get("supports", "")), "reason": str(item.get("reason", "")), "requires_pixels": requires_pixels})
    return decisions, events


def _neighborhood(decision: Mapping[str, Any], handles: Mapping[str, Any], blocks: Sequence[Any]) -> dict[str, Any]:
    block = handles[str(decision["handle"])]
    index = next(i for i, item in enumerate(blocks) if item.block_id == block.block_id)
    neighbors = []
    for item in blocks[max(0, index - 3): min(len(blocks), index + 4)]:
        if item.block_id == block.block_id or item.section != block.section or item.kind not in {"paragraph", "text", "formula", "table"}:
            continue
        value = item.latex or item.table_html or item.text
        neighbors.append({"handle": f"b{item.order}", "kind": item.kind, "content": re.sub(r"\s+", " ", value).strip()[:5000]})
    value = block.latex or block.table_html or block.text
    return {"handle": str(decision["handle"]), "kind": block.kind, "section": block.section, "object": re.sub(r"\s+", " ", value).strip()[:10000], "neighbors": neighbors}


def _validate_briefs(raw: Mapping[str, Any], neighborhoods: list[dict[str, Any]], allowed: Mapping[str, Any]) -> list[dict[str, Any]]:
    if raw.get("_fallback"):
        raise RuntimeError(f"Definition briefs failed: {raw}")
    body = base._response_body(raw, "briefs")
    values = body.get("briefs")
    by_handle = {str(item.get("handle", "")): dict(item) for item in values} if isinstance(values, list) else {}
    expected = {str(item["handle"]) for item in neighborhoods}
    if set(by_handle) != expected:
        raise RuntimeError(f"Definition briefs mismatch: expected={expected}, actual={set(by_handle)}")
    neighborhood_handles = {str(item["handle"]): {str(item["handle"]), *(str(value["handle"]) for value in item["neighbors"])} for item in neighborhoods}
    result = []
    for handle in (str(item["handle"]) for item in neighborhoods):
        item = by_handle[handle]
        allowed_local = neighborhood_handles[handle]
        item["evidence_handles"] = _handles(item.get("evidence_handles"), {key: allowed[key] for key in allowed_local})
        definitions = item.get("definitions", [])
        if not isinstance(definitions, list):
            raise RuntimeError(f"Brief definitions must be list: {handle}")
        for definition in definitions:
            definition["evidence_handles"] = _handles(definition.get("evidence_handles"), {key: allowed[key] for key in allowed_local})
        result.append(item)
    return result


def _paper_model_summary(model: Mapping[str, Any]) -> dict[str, Any]:
    return {key: model.get(key) for key in ("paper_type", "central_problem", "prior_gap", "central_idea", "argument_chain", "sections", "experiments")}


def _neighbor_text(block: Any, blocks: Sequence[Any]) -> str:
    index = next(i for i, item in enumerate(blocks) if item.block_id == block.block_id)
    return " ".join(item.text for item in blocks[max(0, index - 2): min(len(blocks), index + 3)] if item.kind in {"text", "paragraph"})[:7000]


def _validate_sections(raw: Mapping[str, Any], model: Mapping[str, Any]) -> list[dict[str, str]]:
    if raw.get("_fallback"):
        raise RuntimeError(f"Writer failed: {raw}")
    body = base._response_body(raw, "sections")
    values = body.get("sections")
    expected = [str(item["section_id"]) for item in model["sections"]]
    actual = [str(item.get("section_id", "")) for item in values] if isinstance(values, list) else []
    if actual != expected:
        raise RuntimeError(f"Writer sections mismatch: expected={expected}, actual={actual}")
    result = [{"section_id": str(item["section_id"]), "markdown": str(item.get("markdown", "")).strip()} for item in values]
    if any(not item["markdown"] for item in result):
        raise RuntimeError("Writer emitted empty section")
    return result


def _publish_assets(config: ExperimentConfig, decisions: Sequence[Mapping[str, Any]], handles: Mapping[str, Any]) -> list[dict[str, Any]]:
    asset_dir = config.output / "assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    result = []
    figure_index = 0
    for item in decisions:
        if item["decision"] != "inline":
            continue
        block = handles[str(item["handle"])]
        path = ""
        size = 0
        if block.kind == "figure" and block.image_path and Path(block.image_path).is_file():
            figure_index += 1
            source = Path(block.image_path)
            target = asset_dir / f"figure-{figure_index}{source.suffix.casefold() or '.jpg'}"
            shutil.copy2(source, target)
            path = f"assets/{target.name}"
            size = target.stat().st_size
        result.append({"handle": item["handle"], "kind": block.kind, "section_id": item["section_id"], "relative_path": path, "bytes": size})
    return result


def _render(config: ExperimentConfig, sections: Sequence[Mapping[str, str]], model: Mapping[str, Any], decisions: Sequence[Mapping[str, Any]], handles: Mapping[str, Any], briefs: Sequence[Mapping[str, Any]], assets: Sequence[Mapping[str, Any]]) -> str:
    headings = {str(item["section_id"]): str(item["heading"]) for item in model["sections"]}
    by_section: dict[str, list[Mapping[str, Any]]] = {}
    for item in decisions:
        if item["decision"] == "inline":
            by_section.setdefault(str(item["section_id"]), []).append(item)
    asset_paths = {str(item["handle"]): str(item["relative_path"]) for item in assets}
    brief_by_handle = {str(item["handle"]): item for item in briefs}
    parts = [f"# {config.title}：教学式精读"]
    for section in sections:
        section_id = str(section["section_id"])
        parts.extend([f"## {headings[section_id]}", str(section["markdown"])])
        for decision in by_section.get(section_id, []):
            handle = str(decision["handle"])
            block = handles[handle]
            title = str(decision["supports"] or block.caption or "关键材料")
            if block.kind == "figure":
                path = asset_paths.get(handle, "")
                if path:
                    parts.append(f"![{title}]({path})\n\n*{title}*")
            elif block.kind == "table":
                table_markdown = _table_projection(block)
                parts.append(f"**{title}**\n\n{table_markdown}")
            else:
                brief = brief_by_handle.get(handle, {})
                latex = block.latex or block.text
                explanation = str(brief.get("plain_explanation", ""))
                parts.append(f"**{title}**\n\n$$\n{latex}\n$$\n\n{explanation}")
    return "\n\n".join(parts).strip()


def _table_projection(block: Any) -> str:
    if block.table_html:
        return v1._markdown_table(block.table_html)
    lines = [line.strip() for line in str(block.text or "").splitlines() if line.strip().startswith("|")]
    if not lines:
        return str(block.text or "").strip()
    width = max(1, len([cell for cell in lines[0].strip("|").split("|")]))
    if len(lines) == 1 or not re.fullmatch(r"\|(?:\s*:?-+:?\s*\|)+", lines[1]):
        lines.insert(1, "| " + " | ".join("---" for _ in range(width)) + " |")
    return "\n".join(lines)


def _validate_note(note: str, model: Mapping[str, Any], blocks: Sequence[Any], decisions: Sequence[Mapping[str, Any]], briefs: Sequence[Mapping[str, Any]], assets: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if len(note) < 4500:
        issues.append({"type": "too_compressed", "detail": str(len(note))})
    if re.search(r"\bb\d+\b|normalized:|reading-experiment-root", note):
        issues.append({"type": "internal_state_leak", "detail": "evidence handle, block id, or path"})
    for item in model.get("must_preserve_facts", []):
        if not _fact_present_in_one_paragraph(item, note, blocks):
            issues.append({"type": "missing_must_preserve_fact", "detail": str(item.get("statement", ""))})
    source = " ".join((block.text or "") + " " + (block.table_html or "") + " " + (block.latex or "") for block in blocks)
    source = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", source)
    source = re.sub(r"(?<=\d)\s+(?=\d)", "", source)
    allowed_numbers = {base._normalize_number(value).rstrip("%") for value in base._numeric_tokens(base._replace_number_words(source))}
    prose = re.sub(r"\$\$.*?\$\$", "", note, flags=re.S)
    for value in dict.fromkeys(base._numeric_tokens(prose)):
        if base._normalize_number(value).rstrip("%") not in allowed_numbers:
            issues.append({"type": "unsupported_number", "detail": value})
    inline = [item for item in decisions if item["decision"] == "inline"]
    if len(assets) != len(inline):
        issues.append({"type": "asset_publish_mismatch", "detail": f"inline={len(inline)} assets={len(assets)}"})
    selected_formula_handles = {str(item["handle"]) for item in inline if ":formula:" in handles_id(item, blocks)}
    brief_handles = {str(item["handle"]) for item in briefs}
    if not selected_formula_handles.issubset(brief_handles):
        issues.append({"type": "formula_brief_missing", "detail": str(sorted(selected_formula_handles - brief_handles))})
    return _dedupe_issues(issues)


def _fact_present_in_one_paragraph(fact: Mapping[str, Any], note: str, blocks: Sequence[Any]) -> bool:
    paragraphs = [value for value in re.split(r"\n\s*\n", note) if value.strip() and not value.lstrip().startswith(("|", "$$"))]
    by_handle = {f"b{block.order}": block for block in blocks}
    evidence_blocks = [by_handle[str(handle)] for handle in fact.get("evidence_handles", []) if str(handle) in by_handle]
    evidence_text = " ".join((block.text or "") + " " + (block.caption or "") + " " + (block.table_html or "") + " " + (block.latex or "") for block in evidence_blocks)
    entities = [str(value) for value in fact.get("named_entities", []) if str(value).strip() and _entity_present(str(value), evidence_text)]
    quantities = [str(value) for value in fact.get("numeric_tokens", []) if str(value).strip() and _quantity_present(str(value), evidence_text)]
    if not entities and not quantities:
        return True
    return any(
        all(_entity_present(entity, paragraph) for entity in entities)
        and all(_quantity_present(quantity, paragraph) for quantity in quantities)
        for paragraph in paragraphs
    )


def _must_preserve_degradations(model: Mapping[str, Any], blocks: Sequence[Any]) -> list[dict[str, Any]]:
    by_handle = {f"b{block.order}": block for block in blocks}
    result = []
    for fact in model.get("must_preserve_facts", []):
        evidence_blocks = [by_handle[str(handle)] for handle in fact.get("evidence_handles", []) if str(handle) in by_handle]
        evidence_text = " ".join((block.text or "") + " " + (block.caption or "") + " " + (block.table_html or "") + " " + (block.latex or "") for block in evidence_blocks)
        unsupported_entities = [str(value) for value in fact.get("named_entities", []) if not _entity_present(str(value), evidence_text)]
        unsupported_quantities = [str(value) for value in fact.get("numeric_tokens", []) if not _quantity_present(str(value), evidence_text)]
        if unsupported_entities or unsupported_quantities:
            result.append({"statement": fact.get("statement"), "unsupported_entities": unsupported_entities, "unsupported_numeric_tokens": unsupported_quantities, "action": "excluded_from_exact_writer_gate"})
    return result


def _entity_present(entity: str, paragraph: str) -> bool:
    def compact(value: str) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.casefold())
    target = compact(entity)
    haystack = compact(paragraph)
    if target in haystack:
        return True
    return target.endswith("s") and target[:-1] in haystack


def _quantity_present(quantity: str, paragraph: str) -> bool:
    token = quantity.casefold().strip()
    compact = re.sub(r"\s+", "", paragraph.casefold()).replace("×", "x").replace("倍", "x").replace(",", "")
    if token == "linearly":
        return "线性" in paragraph or "linearly" in compact
    if token == "million":
        return "100万" in compact or "百万" in compact or "1000000" in compact or "1m" in compact
    if token == "twice":
        return "2x" in compact or "两倍" in paragraph or "twice" in compact
    normalized = re.sub(r"\s+", "", token).replace("×", "x").replace("倍", "x").replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([kmb%]?)x?", normalized, flags=re.I)
    if match and normalized.endswith("x"):
        target = float(match.group(1))
        for left, right in re.findall(r"(\d+(?:\.\d+)?)?(?:-)?(\d+(?:\.\d+)?)x", compact):
            low = float(left) if left else float(right)
            high = float(right)
            if low <= target <= high:
                return True
    pattern = rf"(?<![0-9.]){re.escape(normalized)}(?![0-9.])"
    return bool(re.search(pattern, compact, flags=re.I))


def handles_id(item: Mapping[str, Any], blocks: Sequence[Any]) -> str:
    order = int(str(item["handle"])[1:])
    block = next((value for value in blocks if value.order == order), None)
    return block.block_id if block else ""


def _dedupe_issues(values: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    seen = set()
    result = []
    for item in values:
        key = (item["type"], item["detail"])
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _validate_blind(raw: Mapping[str, Any]) -> dict[str, Any]:
    if raw.get("_fallback"):
        raise RuntimeError(f"Blind reader failed: {raw}")
    body = base._response_body(raw, "answers", "overall")
    expected = ["background_problem", "prior_gap", "core_solution", "formalism", "experiments", "visual_evidence", "boundaries"]
    values = body.get("answers")
    actual = [str(item.get("id", "")) for item in values] if isinstance(values, list) else []
    if actual != expected:
        raise RuntimeError(f"Blind reader answers mismatch: {actual}")
    if str(body.get("overall", "")) not in {"pass", "needs_targeted_revision", "fail"}:
        raise RuntimeError(f"Invalid blind reader overall: {body.get('overall')}")
    body["resolved_model"] = raw.get("_resolved_model")
    body["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    return body


def _write_html(config: ExperimentConfig, note: str, model: Mapping[str, Any], decisions: Sequence[Mapping[str, Any]], briefs: Sequence[Mapping[str, Any]], visuals: Mapping[str, Any], issues: Sequence[Mapping[str, str]], assets: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    note_html = markdown.markdown(note, extensions=["tables", "fenced_code"])
    note_html = re.sub(r"<p>\$\$\s*(.*?)\s*\$\$</p>", lambda match: f'<pre class="formula">{escape(match.group(1))}</pre>', note_html, flags=re.S)
    embedded = 0
    for asset in assets:
        path = str(asset.get("relative_path", ""))
        if asset["kind"] != "figure" or not path:
            continue
        local = config.output / path
        mime = "image/png" if local.suffix.casefold() == ".png" else "image/jpeg"
        uri = f"data:{mime};base64," + base64.b64encode(local.read_bytes()).decode("ascii")
        note_html = note_html.replace(f'src="{path}"', f'src="{uri}"')
        embedded += 1
    panels = {
        "最终笔记": note_html,
        "PaperModel": f"<pre>{escape(json.dumps(model, ensure_ascii=False, indent=2))}</pre>",
        "素材规划": f"<pre>{escape(json.dumps(decisions, ensure_ascii=False, indent=2))}</pre>",
        "公式与表格解释": f"<pre>{escape(json.dumps(briefs, ensure_ascii=False, indent=2))}</pre>",
        "视觉读取": f"<pre>{escape(json.dumps(visuals, ensure_ascii=False, indent=2))}</pre>",
        "自动检查": f"<pre>{escape(json.dumps(issues, ensure_ascii=False, indent=2))}</pre>",
    }
    depth_plan_path = config.output / "section-depth-plan.json"
    if depth_plan_path.is_file():
        panels = {
            "最终笔记": panels.pop("最终笔记"),
            "章节理解契约": f"<pre>{escape(json.dumps(_read(depth_plan_path), ensure_ascii=False, indent=2))}</pre>",
            **panels,
        }
    buttons = "".join(f'<button data-tab="p{i}" class="{"active" if i == 0 else ""}">{escape(name)}</button>' for i, name in enumerate(panels))
    sections = "".join(f'<section id="p{i}" class="panel {"active" if i == 0 else ""}">{body}</section>' for i, body in enumerate(panels.values()))
    html_value = f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>通用教学式精读泛化实验</title><style>
body{{font:16px/1.82 system-ui;margin:0;background:#f3f5f8;color:#172033}}main{{max-width:1100px;margin:28px auto;padding:0 22px}}header{{background:#172033;color:white;padding:24px 30px;border-radius:16px}}nav{{display:flex;gap:8px;margin:18px 0;flex-wrap:wrap}}button{{border:1px solid #ccd3df;background:white;padding:9px 15px;border-radius:999px;cursor:pointer}}button.active{{background:#3559d8;color:white;border-color:#3559d8}}.panel{{display:none;background:white;padding:28px 38px;border-radius:16px;box-shadow:0 6px 24px #18243b12}}.panel.active{{display:block}}img{{display:block;max-width:100%;margin:24px auto;border-radius:10px}}table{{border-collapse:collapse;width:100%;margin:20px 0;font-size:14px}}th,td{{border:1px solid #d8deea;padding:8px 10px;text-align:left}}pre{{white-space:pre-wrap;background:#f6f8fb;padding:18px;border-radius:10px;overflow:auto}}pre.formula{{font-family:Cambria Math,serif;font-size:17px}}
</style></head><body><main><header><h1>{escape(config.title)}</h1><p>同一套论文无关流程：全文 PaperModel → 选择性公式/图表 → 教学式中文笔记 → 盲读验收。</p></header><nav>{buttons}</nav>{sections}</main><script>document.querySelectorAll('button[data-tab]').forEach(b=>b.onclick=()=>{{document.querySelectorAll('button,.panel').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.tab).classList.add('active')}})</script></body></html>"""
    path = config.output / "trace-review.html"
    path.write_text(html_value, encoding="utf-8")
    expected_images = sum(1 for item in assets if item["kind"] == "figure" and item.get("relative_path"))
    expected_tables = sum(1 for item in assets if item["kind"] == "table")
    expected_formulas = sum(1 for item in assets if item["kind"] == "formula")
    check = {"html_exists": path.is_file(), "expected_images": expected_images, "embedded_images": embedded, "expected_tables": expected_tables, "rendered_tables": note_html.count("<table>"), "expected_formulas": expected_formulas, "formula_panels": note_html.count('class="formula"'), "no_tmp_path": "reading-experiment-root" not in note_html}
    check["passed"] = check["html_exists"] and embedded == expected_images and check["rendered_tables"] == expected_tables and check["formula_panels"] == expected_formulas and check["no_tmp_path"]
    return check


def _inject_verdict(config: ExperimentConfig, receipt: Mapping[str, Any], blind: Mapping[str, Any]) -> None:
    path = config.output / "trace-review.html"
    html_value = path.read_text(encoding="utf-8")
    panel = f'<section style="background:#eef8f0;padding:20px 28px;border:1px solid #b8d8bf;border-radius:16px;margin:18px 0"><h2>泛化结论：{escape(str(receipt["status"]))}</h2><pre>{escape(json.dumps(blind, ensure_ascii=False, indent=2))}</pre></section>'
    path.write_text(html_value.replace("</header>", "</header>" + panel, 1), encoding="utf-8")


def _write(config: ExperimentConfig, name: str, value: Any) -> None:
    (config.output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
