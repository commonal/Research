from __future__ import annotations

"""PROTOTYPE: full-paper B + bounded explanatory Targets + direct grounded Writer."""

from dataclasses import replace
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import base64
import json
import mimetypes
import os
import re
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from research_pulse.production.normalized import load_normalized_jsonl
from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import (
    CanonicalPaperIR,
    DeepSeekPaperReadingModel,
    PaperIRBlock,
    _guard_unsupported_symbol_definitions,
    _guard_unsupported_table_claims,
    _numeric_claim_supported,
    _validated_paper_model,
)


SOURCE_ID = "2608.18351v1"
TITLE = "Task-Conditioned Least-Privilege Learning for Executable Terminal and MCP Agents"
MATERIAL_ROOT = ROOT / "tmp" / "reading-experiment-root" / SOURCE_ID
NORMALIZED = MATERIAL_ROOT / "normalized" / "blocks.jsonl"
V5 = ROOT / "evals" / "real-e2e" / "full-paper-b-production-v5"
REUSE_STATE = ROOT / "evals" / "real-e2e" / "integrated-b-deepening-v1"
OUTPUT = ROOT / "evals" / "real-e2e" / "integrated-b-deepening-v4"


def main() -> int:
    _load_dotenv()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    candidate = PaperCandidate(SOURCE_ID, TITLE, "https://arxiv.org/abs/2608.18351v1", "security")
    paper = _load_paper(candidate)
    stored_model = json.loads((V5 / "paper-model.json").read_text(encoding="utf-8"))
    raw_model = json.loads((V5 / "raw-paper-model-response.json").read_text(encoding="utf-8"))
    validated_model = _validated_paper_model(raw_model, paper)
    model = DeepSeekPaperReadingModel.from_environment()

    reused_state = all((REUSE_STATE / name).is_file() for name in ("deepening-targets.json", "deepening-records.json", "visual-results.json"))
    if reused_state:
        target_payload = json.loads((REUSE_STATE / "deepening-targets.json").read_text(encoding="utf-8"))
        targets = list(target_payload.get("targets", ()))
        selector_fallback = target_payload.get("fallback")
        records = json.loads((REUSE_STATE / "deepening-records.json").read_text(encoding="utf-8"))
        visual_results = json.loads((REUSE_STATE / "visual-results.json").read_text(encoding="utf-8"))
        selector_calls = 0
        target_calls = 0
    else:
        catalog = [_catalog_item(paper.block_by_id[item]) for item in _candidate_ids(stored_model, paper)]
        selector_raw = _call_json(model, "deepening-selector", {
            "operation": "select_explanatory_deepening_targets",
            "question": "Which at most two joint reads would most improve human understanding beyond this already complete full-paper model?",
            "rules": [
                "Select explanatory deepening, not missing local trivia.",
                "Prefer one problem-to-mechanism target and one mechanism-to-evidence target when both add value.",
                "A target must sharpen a causal relationship, interpretation, or conclusion boundary; coverage complete does not forbid deepening.",
                "Use exact catalog block_ids, at most 10 per target, and at most one image requiring pixels across the whole plan.",
                "Do not generate follow-up targets or an open-ended agenda. Return zero to two targets only.",
            ],
            "paper_model": stored_model,
            "catalog": catalog,
            "return": {"targets": [{
                "target_id": "string",
                "kind": "problem_to_mechanism|mechanism_to_evidence",
                "question": "plain explanatory question",
                "success_condition": "what a blind reader should understand",
                "selected_block_ids": ["exact catalog id"],
                "inspect_visual_ids": ["at most one exact figure id"],
                "reason": "why this adds depth beyond the full model",
            }]},
        })
        targets, selector_fallback = _validate_targets(selector_raw, catalog, stored_model, paper)
        visual_results: dict[str, dict[str, Any]] = {}
        records: list[dict[str, Any]] = []
        for target in targets:
            blocks = [paper.block_by_id[item] for item in target["selected_block_ids"]]
            for block_id in target["inspect_visual_ids"]:
                if block_id not in visual_results:
                    visual_results[block_id] = _read_visual(model, target, paper.block_by_id[block_id], paper)
            raw = _call_json(model, f"target-{target['target_id']}", {
                "operation": "joint_explanatory_deep_read",
                "target": target,
                "rules": [
                    "Explain the relationship, not the paper section order.",
                    "Every source fact must cite exact supplied block_ids and preserve exact numbers.",
                    "Keep source facts, cross-block synthesis, formula explanation, experimental interpretation, and unknowns separate.",
                    "Formula meanings must come literally from supplied definition context. Do not infer symbol names from convention.",
                    "A visual result marked degraded may contribute caption/context facts only, never pixel observations.",
                ],
                "blocks": [_full_block(block) for block in blocks],
                "visual_results": [visual_results[item] for item in target["inspect_visual_ids"] if item in visual_results],
                "return": {
                    "plain_explanation": "self-contained explanation of the target",
                    "source_facts": [{"fact_id": "string", "facet": "problem|method|experiment|limitation", "statement": "atomic fact", "source_block_ids": ["exact id"]}],
                    "mechanism_relations": [{"from": "string", "relation": "string", "to": "string", "source_block_ids": ["exact id"]}],
                    "formula_explanations": [{"formula_block_id": "exact id", "role": "why it matters", "definitions": [{"symbol": "string", "meaning": "literal supplied meaning"}], "source_block_ids": ["exact id"]}],
                    "experimental_interpretations": [{"statement": "what the comparison establishes", "source_block_ids": ["exact id"]}],
                    "unknowns": ["material unresolved point"],
                },
            })
            records.append(_validate_record(raw, target, blocks))
        selector_calls = 1
        target_calls = len(records)
    _write_json("deepening-targets.json", {"targets": targets, "fallback": selector_fallback, "reused_from": str(REUSE_STATE) if reused_state else None})
    _write_json("deepening-records.json", records)

    formula_briefs = _formula_briefs(stored_model, paper)
    table_briefs = _table_briefs(stored_model)
    visual_receipt = _visual_receipt(visual_results)
    _write_json("formula-briefs.json", formula_briefs)
    _write_json("table-briefs.json", table_briefs)
    _write_json("visual-results.json", visual_results)
    _write_json("visual-receipt.json", visual_receipt)

    writer_raw = _call_json(model, "direct-long-writer", _writer_payload(stored_model, records, formula_briefs, table_briefs, visual_results))
    draft = _response_body(writer_raw, "markdown")
    markdown = str(draft.get("markdown", "")).strip()
    if not markdown:
        raise RuntimeError("Direct long Writer returned no markdown.")
    markdown, issues = _validate_draft(markdown, stored_model, validated_model, records, formula_briefs, table_briefs, visual_results)
    repair_calls = 0
    if issues:
        repair_calls = 1
        repair_raw = _call_json(model, "local-draft-repair", {
            "operation": "repair_grounding_issues_without_rewriting_the_note_structure",
            "draft": markdown,
            "issues": issues,
            "formula_briefs": formula_briefs,
            "table_briefs": table_briefs,
            "visual_results": list(visual_results.values()),
            "required_conclusion_boundary": stored_model.get("conclusion_scope", ""),
            "rules": [
                "Repair only the listed unsupported, missing, or malformed local content.",
                "Keep the narrative order, supported exact numbers, and {{asset:figure-1}} marker.",
                "Keep or insert {{asset:main-results-table}} in the internal-results paragraph.",
                "When visual explanation is missing, explicitly walk through Fig.1 from policy to broker, terminal/MCP, telemetry and verifiers, deterministic reward, and training update.",
                "When the sandbox boundary is missing, state in the limitations section that learned restraint is an additional control layer and cannot replace permission gates or sandboxing.",
                "Do not introduce new facts. Return JSON with markdown only.",
            ],
        })
        repaired = str(_response_body(repair_raw, "markdown").get("markdown", "")).strip()
        if repaired:
            markdown, issues = _validate_draft(repaired, stored_model, validated_model, records, formula_briefs, table_briefs, visual_results)
    rendered = _render_asset_markers(markdown, visual_results, table_briefs, paper)
    (OUTPUT / "reading-note.md").write_text(rendered + "\n", encoding="utf-8")
    _write_json("writer-output.json", draft)
    _write_json("final-validation.json", {"issues": issues, "repair_calls": repair_calls})

    comparison = _compare(rendered)
    receipt = {
        "route": "full_paper_plus_bounded_deepening_direct_writer",
        "status": "completed" if not issues else "needs_review",
        "reused_full_paper_model": str(V5 / "paper-model.json"),
        "reused_deepening_state": str(REUSE_STATE) if reused_state else None,
        "selector_calls": selector_calls,
        "target_count": len(targets),
        "target_calls": target_calls,
        "vision": visual_receipt,
        "writer_calls": 1,
        "repair_calls": repair_calls,
        "text_calls_this_experiment": model.text_call_count,
        "vision_calls_this_experiment": model.vision_call_count,
        "fallbacks": list(model.fallbacks),
        "validation_issues": issues,
        "started_at": started,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json("reading-receipt.json", receipt)
    _write_json("comparison.json", comparison)
    (OUTPUT / "comparison.md").write_text(_comparison_markdown(comparison, receipt), encoding="utf-8")
    (OUTPUT / "trace-review.html").write_text(_trace_html(rendered, stored_model, targets, records, formula_briefs, table_briefs, visual_results, receipt, comparison), encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "receipt": receipt, "comparison": comparison}, ensure_ascii=False, indent=2))
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


def _candidate_ids(paper_model: Mapping[str, Any], paper: CanonicalPaperIR) -> list[str]:
    ordered = list(paper.ordered_blocks)
    position = {block.block_id: index for index, block in enumerate(ordered)}
    ids: list[str] = []
    for group in ("source_facts", "source_limitations", "agent_syntheses"):
        for item in paper_model.get(group, ()):
            if isinstance(item, Mapping):
                ids.extend(str(value) for value in item.get("source_block_ids", ()) if str(value) in position)
    for item in paper_model.get("definition_neighborhoods", ()):
        if isinstance(item, Mapping):
            ids.extend(str(value) for value in item.get("source_block_ids", ()) if str(value) in position)
    for item in paper_model.get("visual_decisions", ()):
        if isinstance(item, Mapping) and str(item.get("block_id", "")) in position:
            ids.append(str(item["block_id"]))
    expanded: list[str] = []
    for block_id in ids:
        index = position[block_id]
        expanded.extend(block.block_id for block in ordered[max(0, index - 1):min(len(ordered), index + 2)])
    return list(dict.fromkeys(expanded))[:72]


def _catalog_item(block: PaperIRBlock) -> dict[str, Any]:
    value = block.caption or block.latex or block.table_html or block.text
    return {
        "block_id": block.block_id,
        "order": block.order,
        "section": block.section,
        "kind": block.kind,
        "excerpt": re.sub(r"\s+", " ", value).strip()[:650],
        "has_real_image": bool(block.image_path and Path(block.image_path).is_file()),
    }


def _validate_targets(raw: Mapping[str, Any], catalog: list[dict[str, Any]], paper_model: Mapping[str, Any], paper: CanonicalPaperIR) -> tuple[list[dict[str, Any]], str | None]:
    body = _response_body(raw, "targets")
    allowed = {str(item["block_id"]) for item in catalog}
    targets: list[dict[str, Any]] = []
    for index, item in enumerate(body.get("targets", ()) if isinstance(body.get("targets"), list) else ()):
        if not isinstance(item, Mapping) or len(targets) >= 2:
            continue
        ids = list(dict.fromkeys(str(value) for value in item.get("selected_block_ids", ()) if str(value) in allowed))[:10]
        if not ids:
            continue
        visuals = [value for value in (str(raw_id) for raw_id in item.get("inspect_visual_ids", ())) if value in ids and paper.block_by_id[value].kind == "figure" and paper.block_by_id[value].image_path]
        targets.append({
            "target_id": str(item.get("target_id", f"deep-{index + 1}")),
            "kind": str(item.get("kind", "problem_to_mechanism")),
            "question": str(item.get("question", "")),
            "success_condition": str(item.get("success_condition", "")),
            "selected_block_ids": ids,
            "inspect_visual_ids": visuals[:1],
            "reason": str(item.get("reason", "")),
        })
    fallback: str | None = None
    if not targets:
        fallback = "selector_returned_no_valid_target"
        targets = _fallback_targets(paper_model, paper)
    figure_one = next((block for block in paper.ordered_blocks if block.kind == "figure" and "Fig. 1." in (block.caption or block.text) and block.image_path), None)
    if figure_one and targets and not any(target["inspect_visual_ids"] for target in targets):
        first = targets[0]
        first["selected_block_ids"] = list(dict.fromkeys([*first["selected_block_ids"], figure_one.block_id]))[:10]
        first["inspect_visual_ids"] = [figure_one.block_id]
    return targets[:2], fallback


def _fallback_targets(paper_model: Mapping[str, Any], paper: CanonicalPaperIR) -> list[dict[str, Any]]:
    facets = {name: [] for name in ("problem", "method", "experiment", "limitation")}
    for fact in paper_model.get("source_facts", ()):
        if isinstance(fact, Mapping) and str(fact.get("facet")) in facets:
            facets[str(fact["facet"])].extend(str(value) for value in fact.get("source_block_ids", ()) if str(value) in paper.block_by_id)
    method_ids = list(dict.fromkeys([*facets["problem"][-2:], *facets["method"]]))[:10]
    experiment_ids = list(dict.fromkeys([*facets["experiment"], *facets["limitation"][:2]]))[:10]
    return [
        {"target_id": "deep-mechanism", "kind": "problem_to_mechanism", "question": "Why are permission gates insufficient, and how do the risk representation, sufficient-authority envelope, reward, and broker loop address that gap?", "success_condition": "A blind reader can explain the complete problem-to-mechanism chain.", "selected_block_ids": method_ids, "inspect_visual_ids": [], "reason": "Deepen the central causal mechanism."},
        {"target_id": "deep-evidence", "kind": "mechanism_to_evidence", "question": "Which comparisons show learned restraint rather than refusal or prompt dependence, and where does generalization remain limited?", "success_condition": "A blind reader can connect the mechanism to decisive evidence without overclaiming.", "selected_block_ids": experiment_ids, "inspect_visual_ids": [], "reason": "Deepen method-to-evidence interpretation."},
    ]


def _full_block(block: PaperIRBlock) -> dict[str, Any]:
    return {"block_id": block.block_id, "order": block.order, "section": block.section, "kind": block.kind, "text": block.text, "caption": block.caption, "latex": block.latex, "table_html": block.table_html}


def _validate_record(raw: Mapping[str, Any], target: Mapping[str, Any], blocks: list[PaperIRBlock]) -> dict[str, Any]:
    body = _response_body(raw, "plain_explanation", "source_facts")
    block_map = {block.block_id: block for block in blocks}
    facts = []
    for index, item in enumerate(body.get("source_facts", ()) if isinstance(body.get("source_facts"), list) else ()):
        if not isinstance(item, Mapping):
            continue
        ids = tuple(dict.fromkeys(str(value) for value in item.get("source_block_ids", ()) if str(value) in block_map))
        statement = str(item.get("statement", "")).strip()
        facet = str(item.get("facet", ""))
        if statement and ids and facet in {"problem", "method", "experiment", "limitation"} and _numeric_claim_supported(statement, ids, block_map):
            facts.append({"fact_id": str(item.get("fact_id", f"{target['target_id']}-f{index + 1}")), "facet": facet, "statement": statement, "source_block_ids": ids})
    def cited(group: str) -> list[dict[str, Any]]:
        result = []
        for item in body.get(group, ()) if isinstance(body.get(group), list) else ():
            if isinstance(item, Mapping):
                ids = tuple(dict.fromkeys(str(value) for value in item.get("source_block_ids", ()) if str(value) in block_map))
                if ids:
                    result.append({**dict(item), "source_block_ids": ids})
        return result
    return {
        "target": dict(target),
        "plain_explanation": str(body.get("plain_explanation", "")),
        "source_facts": facts,
        "mechanism_relations": cited("mechanism_relations"),
        "formula_explanations": cited("formula_explanations"),
        "experimental_interpretations": cited("experimental_interpretations"),
        "unknowns": [str(value) for value in body.get("unknowns", ()) if str(value).strip()],
    }


def _read_visual(model: DeepSeekPaperReadingModel, target: Mapping[str, Any], block: PaperIRBlock, paper: CanonicalPaperIR) -> dict[str, Any]:
    if not block.image_path or not Path(block.image_path).is_file():
        return {"block_id": block.block_id, "status": "failed", "reason": "local_asset_missing", "interpretation": "", "observed_relations": [], "caption": block.caption}
    ordered = list(paper.ordered_blocks)
    index = next(i for i, item in enumerate(ordered) if item.block_id == block.block_id)
    response = _call_json(model, "visual-figure-1", {
        "operation": "read_important_paper_figure",
        "target_question": target["question"],
        "caption": block.caption,
        "neighboring_context": "\n".join(item.text for item in ordered[max(0, index - 2):min(len(ordered), index + 3)]),
        "rules": ["Report only relationships visible in the pixels or explicitly stated in the supplied caption/context.", "Separate pixel observations from caption-supported interpretation.", "Return a non-empty interpretation and observed_relations when successful."],
        "return": {"status": "success|degraded|failed", "interpretation": "string", "observed_relations": ["string"], "caption_supported_relations": ["string"], "uncertainties": ["string"]},
    }, image=block.image_path)
    body = _response_body(response, "status", "interpretation")
    interpretation = str(body.get("interpretation", "")).strip()
    relations = [str(value) for value in body.get("observed_relations", ()) if str(value).strip()] if isinstance(body.get("observed_relations"), list) else []
    requested = str(body.get("status", "failed"))
    status = "success" if requested == "success" and interpretation and relations else ("degraded" if block.caption else "failed")
    reason = "" if status == "success" else (str(response.get("_fallback", "")) or "empty_or_invalid_visual_interpretation")
    return {"block_id": block.block_id, "status": status, "reason": reason, "interpretation": interpretation if status == "success" else "", "observed_relations": relations if status == "success" else [], "caption_supported_relations": [str(value) for value in body.get("caption_supported_relations", ()) if str(value).strip()] if isinstance(body.get("caption_supported_relations"), list) else [], "uncertainties": [str(value) for value in body.get("uncertainties", ()) if str(value).strip()] if isinstance(body.get("uncertainties"), list) else [], "caption": block.caption, "image_path": block.image_path}


def _formula_briefs(paper_model: Mapping[str, Any], paper: CanonicalPaperIR) -> list[dict[str, Any]]:
    actions = {str(item.get("block_id")): str(item.get("action")) for item in paper_model.get("visual_decisions", ()) if isinstance(item, Mapping)}
    result = []
    for item in paper_model.get("definition_neighborhoods", ()):
        if isinstance(item, Mapping) and item.get("kind") == "formula" and actions.get(str(item.get("object_block_id"))) in {"inline", "reference"}:
            formula_symbols = [str(value) for value in item.get("formula_symbols", ())]
            defined = [str(value) for value in item.get("defined_symbols", ())]
            symbol_contexts = _symbol_definition_contexts(formula_symbols, paper)
            source_ids = list(dict.fromkeys([
                *(str(value) for value in item.get("source_block_ids", ())),
                *(str(context["block_id"]) for context in symbol_contexts),
            ]))
            result.append({
                "asset_key": "authority-vector" if not formula_symbols else "reward-formula",
                "block_id": str(item.get("object_block_id")),
                "decision": actions.get(str(item.get("object_block_id"))),
                "completeness": "complete" if not formula_symbols or set(formula_symbols).issubset(defined) else "partial",
                "formula_symbols": formula_symbols,
                "defined_symbols": defined,
                "definition_context": str(item.get("context", "")),
                "symbol_definition_contexts": symbol_contexts,
                "source_block_ids": source_ids,
            })
    return result


def _symbol_definition_contexts(symbols: list[str], paper: CanonicalPaperIR) -> list[dict[str, str]]:
    """Hydrate formula briefs with paper-wide, definition-like source passages."""
    if not symbols:
        return []
    definition_words = re.compile(r"\b(denote|denotes|represent|represents|measure|measures|mark|marks|reward|rewards|penalize|penalizes|correspond)\b", re.I)
    result: list[tuple[int, int, dict[str, str]]] = []
    for block in paper.ordered_blocks:
        text_value = re.sub(r"\s+", " ", block.text or block.caption or "").strip()
        if not text_value or "REFERENCE" in block.section.upper() or not definition_words.search(text_value):
            continue
        hits = sum(bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])", text_value)) for symbol in symbols)
        if hits:
            result.append((-hits, block.order, {"block_id": block.block_id, "text": text_value}))
    return [value for _, _, value in sorted(result)[:8]]


def _table_briefs(paper_model: Mapping[str, Any]) -> list[dict[str, Any]]:
    actions = {str(item.get("block_id")): str(item.get("action")) for item in paper_model.get("visual_decisions", ()) if isinstance(item, Mapping)}
    return [{"asset_key": "main-results-table" if actions.get(str(item.get("object_block_id"))) == "inline" else "continuation-table", "block_id": str(item.get("object_block_id")), "decision": actions.get(str(item.get("object_block_id"))), "rows": item.get("table_rows", ()), "context": str(item.get("context", "")), "source_block_ids": item.get("source_block_ids", ())} for item in paper_model.get("definition_neighborhoods", ()) if isinstance(item, Mapping) and item.get("kind") == "table" and actions.get(str(item.get("object_block_id"))) in {"inline", "reference"}]


def _visual_receipt(results: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    values = list(results.values())
    return {"selected": len(values), "attempted": len(values), "succeeded": sum(item.get("status") == "success" for item in values), "degraded": sum(item.get("status") == "degraded" for item in values), "failed": sum(item.get("status") == "failed" for item in values), "items": values}


def _writer_payload(paper_model: Mapping[str, Any], records: list[dict[str, Any]], formulas: list[dict[str, Any]], tables: list[dict[str, Any]], visuals: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "operation": "direct_grounded_chinese_deep_reading_writer",
        "task": "Write one fluent, self-contained Chinese deep-reading note for a reader who has not opened the paper. Use the full PaperModel for global stability and the bounded records for explanatory depth.",
        "narrative_contract": ["一分钟说清楚", "背景与具体问题", "为什么已有方法不够", "核心想法与方法机制", "实验如何验证", "结果应该怎样理解", "局限与开放问题"],
        "rules": [
            "Write in the narrative contract order, but use natural headings and transitions rather than reading-round order.",
            "Prefer 2200-3500 Chinese characters; explanation quality matters more than hitting a length target.",
            "Explain problem-to-mechanism and mechanism-to-evidence relationships, not just facts.",
            "Use exact numbers, model/mechanism names, formula meanings, and limitations only from validated inputs.",
            "For every formula symbol, first consult symbol_definition_contexts, including remote definitions of S and E; never guess from conventional notation.",
            "For the reward formula, explain S, E, P, U, H, B, F_u and F_r exactly from the supplied source context.",
            "Use the main result table to explain that task success and safe success rise together, so safety was not obtained by refusing work.",
            "State the conclusion boundary explicitly: learned restraint is an additional control layer and does not replace permission gates or sandboxing.",
            "Insert the exact marker {{asset:figure-1}} in the method section. If visual status is success, explain observed pixel relations; otherwise use caption-supported relations only and do not claim pixel inspection succeeded.",
            "Immediately around {{asset:figure-1}}, explain in Chinese how Fig.1 flows from policy through broker, terminal/MCP, telemetry and verifiers, deterministic reward, and training update.",
            "Insert the exact marker {{asset:main-results-table}} in the internal-results paragraph. Do not rewrite its cells; the renderer will use validated table rows.",
            "Do not expose block ids, evidence excerpts, internal state names, or provider details.",
            "Return a new JSON object with markdown, used_fact_ids, used_formula_assets, used_table_assets, used_visual_assets, and asset_placements.",
        ],
        "paper_model": paper_model,
        "deepening_records": records,
        "formula_briefs": formulas,
        "table_briefs": tables,
        "visual_results": list(visuals.values()),
    }


def _validate_draft(markdown: str, stored_model: Mapping[str, Any], validated_model: Any, records: list[dict[str, Any]], formulas: list[dict[str, Any]], tables: list[dict[str, Any]], visuals: Mapping[str, Mapping[str, Any]]) -> tuple[str, list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    markdown, symbols = _guard_unsupported_symbol_definitions(markdown, validated_model)
    issues.extend({"type": "unsupported_formula_definition", "detail": value} for value in symbols)
    markdown, table_ids = _guard_unsupported_table_claims(markdown, validated_model)
    issues.extend({"type": "unsupported_table_relationship", "detail": value} for value in table_ids)
    wrong = ("U表示未使用工具", "U 表示未使用工具", "H表示帮助性", "H 表示帮助性", "B表示破坏性", "B 表示破坏性", "P表示持久性", "P 表示持久性")
    issues.extend({"type": "known_wrong_formula_meaning", "detail": value} for value in wrong if value in markdown)
    if re.search(r"E\s*(?:和|、|分别)?[^。；\n]{0,24}(?:效率|efficiency)", markdown, flags=re.I):
        issues.append({"type": "known_wrong_formula_meaning", "detail": "E must mean evidence existence and sufficiency, not efficiency"})
    source_text = " ".join([
        *(str(item.get("statement", "")) for item in stored_model.get("source_facts", ()) if isinstance(item, Mapping)),
        *(str(item.get("statement", "")) for item in stored_model.get("source_limitations", ()) if isinstance(item, Mapping)),
        *(str(item.get("definition_context", "")) for item in formulas),
        *(str(item.get("context", "")) for item in tables),
        *(str(fact.get("statement", "")) for record in records for fact in record.get("source_facts", ())),
    ])
    allowed = {_normalize_number(value) for value in _numeric_tokens(_replace_number_words(source_text))}
    numeric_markdown = re.sub(r"\{\{asset:[^}]+\}\}", "", markdown)
    numeric_markdown = re.sub(r"(?m)^\s*\d+\.(?=\s)", "", numeric_markdown)
    for value in dict.fromkeys(_numeric_tokens(numeric_markdown)):
        if _normalize_number(value) not in allowed:
            issues.append({"type": "unsupported_number", "detail": value})
    for required in ("为什么", "broker", "64.36%", "98.48%", "4.56%", "0.79%", "权限门", "沙箱", "{{asset:figure-1}}", "{{asset:main-results-table}}"):
        if required.casefold() not in markdown.casefold():
            issues.append({"type": "missing_required_explanation", "detail": required})
    if any(item.get("status") == "success" for item in visuals.values()):
        visual_terms = {
            "figure_reference": ("图1", "fig.1", "fig. 1"),
            "telemetry": ("遥测", "telemetry"),
            "verifiers": ("验证器", "verifier"),
            "training_update": ("训练更新", "training update"),
        }
        for name, alternatives in visual_terms.items():
            if not any(value.casefold() in markdown.casefold() for value in alternatives):
                issues.append({"type": "missing_visual_explanation", "detail": name})
    if "normalized:" in markdown:
        issues.append({"type": "raw_block_id", "detail": "normalized:"})
    return markdown, issues


def _numeric_tokens(value: str) -> list[str]:
    return re.findall(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?(?:[eE][+-]?\d+)?%?", value)


def _normalize_number(value: str) -> str:
    return re.sub(r"[\s,]+", "", value).casefold()


def _replace_number_words(value: str) -> str:
    words = {
        "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
        "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
        "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
        "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
        "eighteen": "18", "nineteen": "19", "twenty": "20",
    }
    return re.sub(r"\b(" + "|".join(words) + r")\b", lambda match: words[match.group(1).casefold()], value, flags=re.I)


def _render_asset_markers(markdown: str, visual_results: Mapping[str, Mapping[str, Any]], tables: list[dict[str, Any]], paper: CanonicalPaperIR) -> str:
    result = next(iter(visual_results.values()), None)
    if result and result.get("image_path"):
        image = str(result["image_path"]).replace("\\", "/")
        rendered = f"\n\n![图1：broker强化学习闭环]({image})\n\n*图1：策略选择动作；broker 负责解析和执行前审计，将动作发送至终端或 MCP 服务，并依据返回结果计算训练奖励。*\n\n"
    else:
        rendered = "\n\n*Fig.1 视觉材料不可用，方法关系仅依据正文和图注解释。*\n\n"
    markdown = markdown.replace("{{asset:figure-1}}", rendered)
    main_table = next((item for item in tables if item.get("asset_key") == "main-results-table" and item.get("decision") == "inline"), None)
    table_markdown = _markdown_table(main_table.get("rows", ())) if main_table else "*主结果表不可用。*"
    return markdown.replace("{{asset:main-results-table}}", f"\n\n**表 II：完整 500 任务保留集评估**\n\n{table_markdown}\n\n")


def _markdown_table(rows: Iterable[Iterable[Any]]) -> str:
    values = [[str(cell).replace("|", "\\|") for cell in row] for row in rows]
    if not values:
        return ""
    width = max(len(row) for row in values)
    values = [row + [""] * (width - len(row)) for row in values]
    lines = ["| " + " | ".join(values[0]) + " |", "| " + " | ".join("---" for _ in range(width)) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in values[1:])
    return "\n".join(lines)


def _compare(note: str) -> dict[str, Any]:
    paths = {
        "integrated": OUTPUT / "reading-note.md",
        "dynamic_v2": ROOT / "evals" / "real-e2e" / "human-like-emergent-questions-v2" / "reading-note.md",
        "B_v4": ROOT / "evals" / "real-e2e" / "human-like-bd-v4" / "b-reading-note.md",
        "D_v4": ROOT / "evals" / "real-e2e" / "human-like-bd-v4" / "d-reading-note.md",
        "v5": V5 / "reading-note.md",
        "golden": ROOT / "evals" / "real-e2e" / "2608.18351v1-golden-human-note.md",
    }
    notes = {name: (note if name == "integrated" else path.read_text(encoding="utf-8") if path.is_file() else "") for name, path in paths.items()}
    concepts = ("Qwen3.5-4B", "broker", "六维", "64.36%", "98.48%", "4.56%", "0.79%", "68.92%", "99.27%", "权限门", "沙箱", "F_u", "F_r")
    wrong = ("U表示未使用工具", "U 表示未使用工具", "H表示帮助性", "H 表示帮助性", "B表示破坏性", "B 表示破坏性", "E表示效率", "E 表示效率")
    return {"characters": {name: len(value) for name, value in notes.items()}, "concept_hits": {concept: {name: concept.casefold() in value.casefold() for name, value in notes.items()} for concept in concepts}, "wrong_formula_meanings": {name: [item for item in wrong if item in value] for name, value in notes.items()}, "integrated_sections": re.findall(r"^#{1,3}\s+(.+)$", note, flags=re.MULTILINE)}


def _comparison_markdown(comparison: Mapping[str, Any], receipt: Mapping[str, Any]) -> str:
    return "# 综合 B+ 实验对比\n\n" + "## 调用与状态\n\n```json\n" + json.dumps(receipt, ensure_ascii=False, indent=2) + "\n```\n\n## 程序化比较\n\n```json\n" + json.dumps(comparison, ensure_ascii=False, indent=2) + "\n```\n"


def _trace_html(note: str, paper_model: Mapping[str, Any], targets: Any, records: Any, formulas: Any, tables: Any, visuals: Any, receipt: Any, comparison: Any) -> str:
    def panel(title: str, value: Any) -> str:
        body = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        return f"<details><summary>{escape(title)}</summary><pre>{escape(body)}</pre></details>"
    return """<!doctype html><meta charset='utf-8'><title>综合 B+ 阅读实验</title><style>body{font:16px/1.65 system-ui;max-width:1100px;margin:40px auto;padding:0 24px;color:#172033}h1{font-size:30px}details{border:1px solid #dce2ea;border-radius:10px;padding:12px;margin:12px 0}summary{font-weight:700;cursor:pointer}pre{white-space:pre-wrap;background:#f6f8fb;padding:16px;border-radius:8px}.note{border-left:4px solid #4f6bed;padding:8px 24px;background:#fafbff}</style>""" + "<h1>综合 B+ 阅读实验</h1><p>问题：全文 B 的稳定主线、两个动态深化 Target、直接长文 Writer 与 v5 证据门禁能否同时成立？</p>" + panel("Receipt", receipt) + panel("Comparison", comparison) + panel("Targets", targets) + panel("Reading Records", records) + panel("Formula Briefs", formulas) + panel("Table Briefs", tables) + panel("Visual Results", visuals) + panel("PaperModel", paper_model) + f"<h2>最终笔记</h2><div class='note'><pre>{escape(note)}</pre></div>"


def _call_json(model: DeepSeekPaperReadingModel, operation: str, payload: Mapping[str, Any], image: str | None = None) -> dict[str, Any]:
    selected_model = model.vision_model if image else model.text_model
    if not selected_model:
        return {"_fallback": "model_not_configured"}
    if image:
        model.vision_call_count += 1
    else:
        model.text_call_count += 1
    prompt = "Return JSON only.\n" + json.dumps(payload, ensure_ascii=False)
    content: Any = prompt
    if image:
        path = Path(image)
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        content = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")}}]
    request = Request(model._base_url, data=json.dumps({"model": selected_model, "messages": [{"role": "user", "content": content}], "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"}, "temperature": 0.1, "max_tokens": 12000 if operation == "direct-long-writer" else 6000, "stream": False}).encode(), headers={"Authorization": f"Bearer {model._api_key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=120) as response:
            outer = json.loads(response.read().decode())
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"DeepSeek {operation} failed with HTTP {error.code}: {detail}") from error
    actual = outer.get("model") or outer.get("choices", [{}])[0].get("model") or selected_model
    raw = str(outer.get("choices", [{}])[0].get("message", {}).get("content", ""))
    _write_json(f"{operation}-provider.json", {"resolved_model": actual, "content": raw, "usage": outer.get("usage", {})})
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        model.fallbacks.append(f"{operation}:invalid_json")
        return {"_fallback": "invalid_json", "_resolved_model": str(actual)}
    return {**parsed, "_resolved_model": str(actual)} if isinstance(parsed, dict) else {"_fallback": "non_object", "_resolved_model": str(actual)}


def _response_body(response: Mapping[str, Any], *expected: str) -> dict[str, Any]:
    if any(key in response for key in expected):
        return dict(response)
    for key in ("return", "result", "output", "paper_model"):
        nested = response.get(key)
        if isinstance(nested, Mapping) and any(item in nested for item in expected):
            return dict(nested)
    return dict(response)


def _resolve_image(value: str) -> Path | None:
    candidates = (MATERIAL_ROOT / value, MATERIAL_ROOT / "mineru" / "source" / "auto" / value)
    return next((item.resolve() for item in candidates if item.is_file()), None)


def _load_dotenv() -> None:
    path = ROOT / ".env"
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                name, value = line.split("=", 1)
                os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def _write_json(name: str, value: Any) -> None:
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
