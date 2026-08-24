from __future__ import annotations

"""Throwaway B/D experiment for full-context scientific-paper reading.

B: full ordered PaperIR -> English PaperModel -> Chinese note.
D: the same PaperModel -> high-value targeted reads -> same Chinese writer ->
   one full-document audit.

Nothing in this file is a production implementation.
"""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
import base64
import html
import json
import os
import re
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen


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
OUTPUT = ROOT / "evals" / "real-e2e" / "human-like-bd-v3"
REUSABLE_PAPER_MODEL = ROOT / "evals" / "real-e2e" / "human-like-bd-v2" / "paper-model.json"
V2_NOTE = ROOT / "evals" / "real-e2e" / "human-like-emergent-questions-v2" / "reading-note.md"
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
    full_document = [full_block(block) for block in paper.ordered_blocks]

    if REUSABLE_PAPER_MODEL.is_file():
        raw_paper_model = {"_reused_from": str(REUSABLE_PAPER_MODEL)}
        paper_model = validate_paper_model(json.loads(REUSABLE_PAPER_MODEL.read_text(encoding="utf-8")), paper)
    else:
        raw_paper_model = call_json(
            model,
            "bd_global_full_read",
            {
            "operation": "global_full_paper_read",
            "task": "Read the complete ordered paper and build a compact English PaperModel. Do not write the final note.",
            "rules": [
                "Reconstruct the paper's causal argument, not a section-by-section summary.",
                "Explain why the work exists, the concrete problem, why prior approaches are insufficient, the central mechanism, how experiments test it, and the conclusion boundary.",
                "Every source_fact and source_limitation must cite exact block_ids from the supplied paper. Keep exact numbers and model/mechanism names unchanged.",
                "Do not treat missing detail as an author limitation. Use material_unknown for information you cannot establish.",
                "Distinguish author facts, cross-block synthesis, and visual candidates. Return at most 24 high-value source facts.",
                "A visual candidate is important only if pixels may add explanatory value beyond caption, formula text, or table HTML.",
            ],
            "title": TITLE,
            "full_ordered_paper": full_document,
            "return": {
                "thesis": "one compact provisional thesis",
                "argument_chain": {
                    "background": "why this area matters",
                    "concrete_problem": "specific failure being addressed",
                    "prior_gap": "why existing approaches are insufficient",
                    "research_question": "what the paper asks",
                    "core_idea": "central abstraction",
                    "mechanisms": ["ordered mechanism relationships"],
                    "experiment_logic": "how experiments test the mechanism",
                    "conclusion_scope": "what is and is not established",
                },
                "coverage": {
                    "background": {"status": "complete|partial|uncertain", "reason": "why", "missing": []},
                    "problem": {"status": "complete|partial|uncertain", "reason": "why", "missing": []},
                    "prior_gap": {"status": "complete|partial|uncertain", "reason": "why", "missing": []},
                    "mechanism": {"status": "complete|partial|uncertain", "reason": "why", "missing": []},
                    "experiment": {"status": "complete|partial|uncertain", "reason": "why", "missing": []},
                    "boundary": {"status": "complete|partial|uncertain", "reason": "why", "missing": []},
                },
                "source_facts": [{"fact_id": "f1", "facet": "problem|method|experiment|limitation", "statement": "atomic fact", "source_block_ids": ["exact id"]}],
                "source_limitations": [{"statement": "author-stated limitation", "source_block_ids": ["exact id"]}],
                "agent_syntheses": [{"statement": "cross-block interpretation", "source_block_ids": ["exact id"]}],
                "visual_candidates": [{"block_id": "exact visual id", "value": "what pixels may clarify", "decision": "inspect|reference|skip"}],
                "material_unknowns": ["unresolved information"],
            },
            },
        )
        paper_model = validate_paper_model(response_body(raw_paper_model, "argument_chain", "source_facts"), paper)
    write_json("raw-paper-model.json", raw_paper_model)
    if not paper_model["source_facts"] or not paper_model["argument_chain"]:
        raise RuntimeError("Global full read did not produce a usable PaperModel; refusing to run writers on empty state.")
    write_json("paper-model.json", paper_model)

    b_start = call_snapshot(model)
    b_note = write_chinese_note(model, paper_model, (), "B")
    b_calls = call_diff(b_start, call_snapshot(model))
    (OUTPUT / "b-reading-note.md").write_text(b_note, encoding="utf-8")
    write_json("b-receipt.json", {"route": "B", "calls_after_shared_global_read": b_calls, "uses_targeted_reading": False, "uses_global_audit": False})

    d_start = call_snapshot(model)
    raw_gap_plan = call_json(
        model,
        "bd_high_value_gap_plan",
        {
            "operation": "high_value_gap_planning",
            "task": "Decide whether at most two targeted reads can materially improve blind-reader understanding beyond the full PaperModel.",
            "rules": [
                "Prioritize a missing link in the main causal story, decisive evidence, author-stated boundary, or an indispensable visual.",
                "Do not pursue local implementation detail unless it would change the paper's main conclusion.",
                "Do not choose a target already adequately covered by validated source facts.",
                "Select at most 8 exact block_ids per target from the catalog. Targets may combine non-contiguous sections.",
                "Use inspect_visual_ids only when real pixels are necessary and the visual id is also selected.",
            ],
            "paper_model": paper_model,
            "paper_catalog": [catalog_item(paper.block_by_id[block_id]) for block_id in gap_candidate_ids(paper_model, paper)],
            "output_contract": "Return a NEW top-level JSON object with keys targets and deferred_local_details. Do not echo the input. Each target must contain target_id, coverage_slot, question, success_condition, selected_block_ids, inspect_visual_ids, and reason.",
        },
    )
    write_json("d-raw-gap-plan.json", raw_gap_plan)
    gap_plan = validate_gap_plan(response_body(raw_gap_plan, "targets"), paper)
    if not gap_plan["targets"]:
        gap_plan = visual_fallback_gap(paper_model, paper)
    write_json("d-gap-plan.json", gap_plan)
    targeted_records: list[dict[str, Any]] = []
    visual_records: list[dict[str, Any]] = []

    for target in gap_plan["targets"][:2]:
        selected = [paper.block_by_id[item] for item in target["selected_block_ids"]]
        target_visuals: list[dict[str, Any]] = []
        for block_id in target["inspect_visual_ids"][:1]:
            block = paper.block_by_id[block_id]
            if not block.image_path or not Path(block.image_path).is_file():
                continue
            raw_visual = call_json(
                model,
                f"bd_visual_{target['target_id']}",
                {
                    "operation": "targeted_visual_read",
                    "target": target,
                    "block_id": block.block_id,
                    "caption": block.caption or block.text,
                    "neighboring_context": neighboring_context(block, paper),
                    "task": "Explain only what the pixels add to this target beyond the supplied caption and text. Mark uncertainty explicitly.",
                    "output_contract": "Return a NEW top-level JSON object with interpretation, adds_beyond_caption, and uncertainty. Do not echo the input.",
                },
                image=block.image_path,
            )
            visual = response_body(raw_visual, "interpretation")
            target_visuals.append({"block_id": block_id, **visual})
            visual_records.append({"target_id": target["target_id"], "raw": raw_visual, "validated": target_visuals[-1]})

        raw_record = call_json(
            model,
            f"bd_target_read_{target['target_id']}",
            {
                "operation": "targeted_gap_read",
                "task": "Jointly read the selected evidence to close this high-value gap. Do not summarize blocks independently.",
                "rules": [
                    "Every source fact must cite exact selected block ids and preserve exact numbers.",
                    "If the selected material is insufficient, return material_unknown; do not fill it from memory.",
                    "Only author-explicit limitations belong in source_limitations.",
                    "Explain how the new evidence changes or strengthens the paper's main argument.",
                ],
                "paper_thesis": paper_model.get("thesis", ""),
                "target": target,
                "selected_blocks": [full_block(block) for block in selected],
                "visual_interpretations": target_visuals,
                "output_contract": "Return a NEW top-level JSON object with answer_status, plain_answer, source_facts, relationships, source_limitations, material_unknowns, and argument_update. Do not echo the input. Facts and relationships must use exact selected block ids.",
            },
        )
        write_json(f"d-raw-target-{target['target_id']}.json", raw_record)
        targeted_records.append(validate_target_record(response_body(raw_record, "answer_status", "source_facts"), target, selected))

    write_json("d-targeted-records.json", targeted_records)
    write_json("d-visual-records.json", visual_records)
    d_pre_audit_note = write_chinese_note(model, paper_model, targeted_records, "D")
    (OUTPUT / "d-pre-audit-note.md").write_text(d_pre_audit_note, encoding="utf-8")

    raw_audit = call_json(
        model,
        "bd_global_note_audit",
        {
            "operation": "full_document_note_audit",
            "task": "Audit the Chinese note against the complete ordered paper and return a corrected final note only where needed.",
            "rules": [
                "Check the full causal story, exact numbers, model and mechanism names, formulas, experiment design, and author-stated boundaries.",
                "Remove unsupported symbol definitions and claims. Do not convert missing material into a paper limitation.",
                "Check whether a critical figure, formula, or table is needed for understanding; explain it naturally, do not dump raw tables.",
                "Preserve fluent Chinese narrative and do not expose block ids or internal reading-state terms.",
                "The corrected note should be self-contained and approximately 1800-3500 Chinese characters.",
            ],
            "draft_note": d_pre_audit_note,
            "full_ordered_paper": full_document,
            "output_contract": "Return a NEW top-level JSON object with verdict, issues, and corrected_markdown. Do not echo the input. corrected_markdown must contain the complete final Chinese note, not a placeholder.",
        },
    )
    write_json("d-raw-global-audit.json", raw_audit)
    audit = response_body(raw_audit, "corrected_markdown", "verdict")
    write_json("d-global-audit.json", audit)
    d_note = str(audit.get("corrected_markdown") or d_pre_audit_note).strip() + "\n"
    (OUTPUT / "d-reading-note.md").write_text(d_note, encoding="utf-8")
    d_calls = call_diff(d_start, call_snapshot(model))
    write_json("d-receipt.json", {"route": "D", "calls_after_shared_global_read": d_calls, "targets": len(targeted_records), "visuals": len(visual_records), "global_audit_verdict": audit.get("verdict", "unknown")})

    comparison = compare_notes(b_note, d_pre_audit_note, d_note, paper_model, targeted_records)
    write_json("comparison.json", comparison)
    (OUTPUT / "comparison.md").write_text(comparison_markdown(comparison), encoding="utf-8")
    (OUTPUT / "trace-review.html").write_text(trace_html(comparison, b_note, d_pre_audit_note, d_note, paper_model, gap_plan, targeted_records, audit), encoding="utf-8")
    write_json(
        "run-meta.json",
        {
            "started_at": started,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "source_id": SOURCE_ID,
            "blocks": len(paper.blocks),
            "full_payload_chars": len(json.dumps(full_document, ensure_ascii=False)),
            "resolved_text_model": model.resolved_text_model,
            "resolved_vision_model": model.resolved_vision_model,
            "total_text_calls": model.text_call_count,
            "total_vision_calls": model.vision_call_count,
            "fallbacks": model.fallbacks,
            "b_calls_after_shared_read": b_calls,
            "d_calls_after_shared_read": d_calls,
        },
    )
    print(json.dumps({"output": str(OUTPUT), "comparison": comparison, "total_calls": call_snapshot(model)}, ensure_ascii=False, indent=2))
    return 0


def call_json(model: DeepSeekPaperReadingModel, operation: str, payload: Mapping[str, Any], image: str | None = None) -> dict[str, Any]:
    selected_model = model.vision_model if image else model.text_model
    if image and not selected_model:
        return {"_fallback": "vision_model_unavailable"}
    if image is None:
        model.text_call_count += 1
    else:
        model.vision_call_count += 1
    prompt = "Return json only.\n" + json.dumps(payload, ensure_ascii=False)
    content: Any = prompt
    if image:
        path = Path(image)
        if not path.is_file():
            raise FileNotFoundError(path)
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")}},
        ]
    max_tokens = 12_000 if operation in {"bd_global_full_read", "bd_global_note_audit"} else 6_000
    request = Request(
        model._base_url,
        data=json.dumps({
            "model": selected_model,
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "stream": False,
        }).encode(),
        headers={"Authorization": f"Bearer {model._api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            outer = json.loads(response.read().decode())
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"DeepSeek request failed with HTTP {error.code}: {detail}") from error
    actual = outer.get("model") or outer.get("choices", [{}])[0].get("model") or selected_model
    if image:
        model.resolved_vision_model = str(actual)
    else:
        model.resolved_text_model = str(actual)
    raw_content = str(outer.get("choices", [{}])[0].get("message", {}).get("content", ""))
    cleaned = strip_json_fence(raw_content)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        safe_operation = re.sub(r"[^a-zA-Z0-9_-]+", "-", operation)
        (OUTPUT / f"{safe_operation}-invalid-response.txt").write_text(raw_content, encoding="utf-8")
        model.fallbacks.append(f"{operation}:invalid_json")
        return {"_fallback": "invalid_json", "_resolved_model": str(actual)}
    if not isinstance(parsed, dict):
        raise ValueError(f"DeepSeek {operation} response must be an object")
    return {**parsed, "_resolved_model": str(actual)}


def response_body(response: Mapping[str, Any], *expected_keys: str) -> dict[str, Any]:
    if any(key in response for key in expected_keys):
        return dict(response)
    nested = response.get("return")
    if isinstance(nested, Mapping) and any(key in nested for key in expected_keys):
        return dict(nested)
    return dict(response)


def strip_json_fence(value: str) -> str:
    value = value.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def full_block(block: PaperIRBlock) -> dict[str, Any]:
    return {
        "block_id": block.block_id,
        "order": block.order,
        "section": block.section,
        "kind": block.kind,
        "text": block.text,
        "caption": block.caption,
        "latex": block.latex,
        "table_html": block.table_html,
        "has_real_image": bool(block.image_path and Path(block.image_path).is_file()),
    }


def catalog_item(block: PaperIRBlock) -> dict[str, Any]:
    value = block.caption or block.latex or block.table_html or block.text
    return {
        "block_id": block.block_id,
        "order": block.order,
        "section": block.section,
        "kind": block.kind,
        "excerpt": re.sub(r"\s+", " ", value).strip()[:500],
        "has_real_image": bool(block.image_path and Path(block.image_path).is_file()),
    }


def gap_candidate_ids(paper_model: Mapping[str, Any], paper: CanonicalPaperIR) -> list[str]:
    ordered = list(paper.ordered_blocks)
    index_by_id = {block.block_id: index for index, block in enumerate(ordered)}
    ids: list[str] = []
    for visual in paper_model.get("visual_candidates", ()):
        if not isinstance(visual, Mapping):
            continue
        block_id = str(visual.get("block_id", ""))
        if block_id not in index_by_id:
            continue
        index = index_by_id[block_id]
        ids.extend(block.block_id for block in ordered[max(0, index - 2): min(len(ordered), index + 3)])
    for group in ("source_facts", "source_limitations", "agent_syntheses"):
        for item in paper_model.get(group, ()):
            if isinstance(item, Mapping):
                ids.extend(str(block_id) for block_id in item.get("source_block_ids", ()) if str(block_id) in paper.block_by_id)
    return list(dict.fromkeys(ids))[:48]


def visual_fallback_gap(paper_model: Mapping[str, Any], paper: CanonicalPaperIR) -> dict[str, Any]:
    ordered = list(paper.ordered_blocks)
    for visual in paper_model.get("visual_candidates", ()):
        if not isinstance(visual, Mapping) or str(visual.get("decision", "")) != "inspect":
            continue
        block_id = str(visual.get("block_id", ""))
        if block_id not in paper.block_by_id or not paper.block_by_id[block_id].image_path:
            continue
        index = next(i for i, block in enumerate(ordered) if block.block_id == block_id)
        ids = [block.block_id for block in ordered[max(0, index - 2): min(len(ordered), index + 3)]]
        return {"targets": [{
            "target_id": "d-visual-1",
            "coverage_slot": "mechanism",
            "question": "What does the most important visual add to the central causal mechanism beyond its caption?",
            "success_condition": "The pixels clarify a mainline relationship that the full-text PaperModel could not establish alone.",
            "selected_block_ids": list(dict.fromkeys([block_id, *ids]))[:8],
            "inspect_visual_ids": [block_id],
            "reason": str(visual.get("value", "The full read marked this visual as worth pixel inspection.")),
        }], "deferred_local_details": ["No additional text-only gap justified another targeted call."]}
    return {"targets": [], "deferred_local_details": ["The full PaperModel reported complete mainline coverage and no indispensable visual was available."]}


def validate_paper_model(value: Mapping[str, Any], paper: CanonicalPaperIR) -> dict[str, Any]:
    candidate_facts = [
        *list(value.get("source_facts", ()) if isinstance(value.get("source_facts"), (list, tuple)) else ()),
        *list(value.get("rejected_numeric_facts", ()) if isinstance(value.get("rejected_numeric_facts"), (list, tuple)) else ()),
    ]
    facts = validate_facts(candidate_facts, paper.block_by_id)
    limitations = validate_limitations(value.get("source_limitations", ()), paper.block_by_id)
    syntheses = []
    for item in value.get("agent_syntheses", ()):
        if not isinstance(item, Mapping) or not str(item.get("statement", "")).strip():
            continue
        ids = valid_ids(item.get("source_block_ids", ()), paper.block_by_id)
        if ids:
            syntheses.append({"statement": str(item["statement"]).strip(), "source_block_ids": ids})
    visuals = []
    for item in value.get("visual_candidates", ()):
        if not isinstance(item, Mapping):
            continue
        block_id = str(item.get("block_id", ""))
        if block_id in paper.block_by_id and paper.block_by_id[block_id].is_visual:
            visuals.append({"block_id": block_id, "value": str(item.get("value", "")), "decision": str(item.get("decision", "reference"))})
    return {
        "thesis": str(value.get("thesis", "")),
        "argument_chain": dict(value.get("argument_chain", {})) if isinstance(value.get("argument_chain"), Mapping) else {},
        "coverage": dict(value.get("coverage", {})) if isinstance(value.get("coverage"), Mapping) else {},
        "source_facts": facts,
        "source_limitations": limitations,
        "agent_syntheses": syntheses,
        "visual_candidates": visuals,
        "material_unknowns": [str(item) for item in value.get("material_unknowns", ()) if str(item).strip()],
        "rejected_numeric_facts": [
            dict(item)
            for item in candidate_facts
            if isinstance(item, Mapping)
            and item.get("statement")
            and not numeric_claim_supported(
                str(item["statement"]),
                valid_ids(item.get("source_block_ids", ()), paper.block_by_id),
                paper.block_by_id,
            )
        ],
    }


def validate_gap_plan(value: Mapping[str, Any], paper: CanonicalPaperIR) -> dict[str, Any]:
    targets = []
    for index, item in enumerate(value.get("targets", ())[:2]):
        if not isinstance(item, Mapping):
            continue
        all_ids = valid_ids(item.get("selected_block_ids", ()), paper.block_by_id)
        requested_visuals = [str(block_id) for block_id in item.get("inspect_visual_ids", ()) if str(block_id) in paper.block_by_id and paper.block_by_id[str(block_id)].is_visual and paper.block_by_id[str(block_id)].image_path]
        ids = list(dict.fromkeys([*requested_visuals[:1], *all_ids]))[:8]
        if not ids:
            continue
        visual_ids = [block_id for block_id in requested_visuals if block_id in ids]
        targets.append({
            "target_id": str(item.get("target_id", f"d{index + 1}")),
            "coverage_slot": str(item.get("coverage_slot", "mechanism")),
            "question": str(item.get("question", "")),
            "success_condition": str(item.get("success_condition", "")),
            "selected_block_ids": ids,
            "inspect_visual_ids": visual_ids[:1],
            "reason": str(item.get("reason", "")),
        })
    return {"targets": targets, "deferred_local_details": [str(item) for item in value.get("deferred_local_details", ()) if str(item).strip()]}


def validate_target_record(value: Mapping[str, Any], target: Mapping[str, Any], selected: list[PaperIRBlock]) -> dict[str, Any]:
    block_map = {block.block_id: block for block in selected}
    normalized_facts = []
    for index, item in enumerate(value.get("source_facts", ()) if isinstance(value.get("source_facts"), (list, tuple)) else ()):
        if not isinstance(item, Mapping):
            continue
        if item.get("statement"):
            normalized_facts.append(dict(item))
            continue
        block_id = str(item.get("block_id", ""))
        statement = str(item.get("fact", "")).strip()
        if block_id and statement:
            normalized_facts.append({
                "fact_id": f"{target.get('target_id', 'target')}-f{index + 1}",
                "facet": str(target.get("coverage_slot", "method")),
                "statement": statement,
                "source_block_ids": [block_id],
            })
    facts = validate_facts(normalized_facts, block_map)
    relationships = []
    for item in value.get("relationships", ()):
        if not isinstance(item, Mapping):
            continue
        ids = valid_ids(item.get("source_block_ids", ()), block_map)
        if ids and item.get("from") and item.get("relation") and item.get("to"):
            relationships.append({"from": str(item["from"]), "relation": str(item["relation"]), "to": str(item["to"]), "source_block_ids": ids})
    return {
        "target": dict(target),
        "answer_status": str(value.get("answer_status", "unresolved")),
        "plain_answer": str(value.get("plain_answer", "")),
        "source_facts": facts,
        "relationships": relationships,
        "source_limitations": validate_limitations(value.get("source_limitations", ()), block_map),
        "material_unknowns": [str(item) for item in value.get("material_unknowns", ()) if str(item).strip()],
        "argument_update": str(value.get("argument_update", "")),
    }


def validate_facts(items: Any, block_map: Mapping[str, PaperIRBlock]) -> list[dict[str, Any]]:
    result = []
    for index, item in enumerate(items if isinstance(items, (list, tuple)) else ()):
        if not isinstance(item, Mapping) or not str(item.get("statement", "")).strip():
            continue
        ids = valid_ids(item.get("source_block_ids", ()), block_map)
        statement = str(item["statement"]).strip()
        facet = str(item.get("facet", "problem"))
        if facet not in {"problem", "method", "experiment", "limitation"} or not ids or not numeric_claim_supported(statement, ids, block_map):
            continue
        result.append({"fact_id": str(item.get("fact_id", f"fact-{index + 1}")), "facet": facet, "statement": statement, "source_block_ids": ids})
    return result


def validate_limitations(items: Any, block_map: Mapping[str, PaperIRBlock]) -> list[dict[str, Any]]:
    result = []
    for item in items if isinstance(items, (list, tuple)) else ():
        if not isinstance(item, Mapping) or not str(item.get("statement", "")).strip():
            continue
        ids = valid_ids(item.get("source_block_ids", ()), block_map)
        if ids and numeric_claim_supported(str(item["statement"]), ids, block_map):
            result.append({"statement": str(item["statement"]).strip(), "source_block_ids": ids})
    return result


def valid_ids(values: Any, block_map: Mapping[str, PaperIRBlock]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in values if str(item) in block_map)) if isinstance(values, (list, tuple)) else []


def numeric_claim_supported(statement: str, ids: Iterable[str], block_map: Mapping[str, PaperIRBlock]) -> bool:
    numbers = re.findall(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?%?", statement)
    if not numbers:
        return True
    source = " ".join(
        " ".join(filter(None, (
            block_map[item].text,
            block_map[item].caption or "",
            block_map[item].latex or "",
            block_map[item].table_html or "",
        )))
        for item in ids
        if item in block_map
    )
    normalized_source = re.sub(r"[\s,]+", "", source)
    return all(re.sub(r"[\s,]+", "", token) in normalized_source for token in numbers)


def write_chinese_note(model: DeepSeekPaperReadingModel, paper_model: Mapping[str, Any], supplemental_records: Iterable[Mapping[str, Any]], route: str) -> str:
    raw = call_json(
        model,
        f"bd_fixed_writer_{route.casefold()}",
        {
            "operation": "fixed_chinese_note_writer",
            "task": "Write a fluent, self-contained Chinese deep-reading note for a reader who has not opened the paper.",
            "rules": [
                "Use this order: one-minute thesis; background and concrete problem; why prior work is insufficient; central mechanism and useful formulas/visuals; how experiments test it; conclusion boundary and open questions.",
                "Do not write in reading-round order and do not expose block ids, evidence excerpts, or internal state names.",
                "Concrete facts, model names, mechanisms, exact numbers, formula meanings, and paper limitations must come from validated source_facts or source_limitations.",
                "Agent synthesis may explain relationships but cannot invent symbol definitions or turn material_unknown into a paper limitation.",
                "If supplemental_records contain an audit, apply its validated corrections and remove or rephrase the listed unsupported draft claims.",
                "Prefer 1800-3500 Chinese characters, avoid repeated headings, and retain exact supported numbers without rounding.",
            ],
            "paper_model": paper_model,
            "supplemental_records": list(supplemental_records),
            "output_contract": "Return a NEW top-level JSON object with exactly one key named markdown. Its value must be the complete Chinese Markdown note, not a placeholder. Do not echo the input.",
        },
    )
    body = response_body(raw, "markdown")
    write_json(f"{route.casefold()}-raw-writer.json", raw)
    markdown = str(body.get("markdown", "")).strip()
    if not markdown or markdown.casefold() == "complete chinese markdown note":
        raise RuntimeError(f"Route {route} writer did not return a usable note.")
    return markdown + "\n"


def neighboring_context(block: PaperIRBlock, paper: CanonicalPaperIR) -> str:
    ordered = list(paper.ordered_blocks)
    index = next(i for i, item in enumerate(ordered) if item.block_id == block.block_id)
    return "\n".join(item.text for item in ordered[max(0, index - 2): min(len(ordered), index + 3)])[:5000]


def resolve_image(image_path: str) -> Path | None:
    candidates = (IMAGE_ROOT / image_path, IMAGE_ROOT / "mineru" / "source" / "auto" / image_path)
    return next((path.resolve() for path in candidates if path.is_file()), None)


def call_snapshot(model: DeepSeekPaperReadingModel) -> dict[str, int]:
    return {"text": model.text_call_count, "vision": model.vision_call_count}


def call_diff(before: Mapping[str, int], after: Mapping[str, int]) -> dict[str, int]:
    return {key: after[key] - before[key] for key in before}


def compare_notes(b_note: str, d_pre: str, d_note: str, paper_model: Mapping[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    v2 = V2_NOTE.read_text(encoding="utf-8") if V2_NOTE.is_file() else ""
    golden = GOLDEN_NOTE.read_text(encoding="utf-8") if GOLDEN_NOTE.is_file() else ""
    concepts = ["Qwen3.5-4B", "broker", "执行前", "执行后", "六维", "2,000", "1,500", "300", "200", "500", "2,896", "64.36%", "98.48%", "4.56%", "0.79%", "权限门", "沙箱"]
    notes = {"B": b_note, "D_pre_audit": d_pre, "D": d_note, "C_v2": v2, "golden": golden}
    return {
        "characters": {name: len(note) for name, note in notes.items()},
        "concept_hits": {concept: {name: concept.casefold() in note.casefold() for name, note in notes.items()} for concept in concepts},
        "raw_block_ids": {name: "normalized:" in note for name, note in notes.items() if name != "golden"},
        "placeholder_numbers": {name: bool(re.search(r"\b(?:XX|YY)%", note, re.I)) for name, note in notes.items() if name != "golden"},
        "global_fact_count": len(paper_model.get("source_facts", ())),
        "global_fact_facets": sorted({item["facet"] for item in paper_model.get("source_facts", ())}),
        "supplemental_fact_count": sum(len(item.get("source_facts", ())) for item in records),
        "target_count": len(records),
    }


def comparison_markdown(value: Mapping[str, Any]) -> str:
    lines = ["# B/D 全文阅读实验对比", "", "## 长度", ""]
    lines.extend(f"- {name}: {count} 字符" for name, count in value["characters"].items())
    lines.extend(["", "## 关键内容命中", ""])
    for concept, hits in value["concept_hits"].items():
        lines.append(f"- {concept}: " + "；".join(f"{name}={hit}" for name, hit in hits.items()))
    lines.extend([
        "",
        "## 结构证据",
        "",
        f"- 全文 PaperModel facts: {value['global_fact_count']}，facets={value['global_fact_facets']}",
        f"- D 定向补读 targets: {value['target_count']}，新增 facts={value['supplemental_fact_count']}",
        f"- raw block ID: {value['raw_block_ids']}",
        f"- XX/YY 占位数字: {value['placeholder_numbers']}",
        "",
        "> 以上只是程序化初筛；路线判定必须继续人工检查主线、事实、边界与叙事。",
    ])
    return "\n".join(lines) + "\n"


def trace_html(comparison: Mapping[str, Any], b_note: str, d_pre: str, d_note: str, paper_model: Mapping[str, Any], gap_plan: Mapping[str, Any], records: list[dict[str, Any]], audit: Mapping[str, Any]) -> str:
    def esc(value: Any) -> str:
        return html.escape(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))

    return f"""<!doctype html><meta charset='utf-8'><title>B/D 全文阅读实验</title>
<style>body{{font:15px/1.65 system-ui;margin:32px;color:#17202a}}h1{{color:#145a73}}.intro{{max-width:1100px;background:#eaf5f7;padding:16px;border-left:4px solid #168aad}}.grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}}article,details{{border:1px solid #d8e1e5;border-radius:10px;padding:18px;background:white}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7f8;padding:14px}}@media(max-width:1000px){{.grid{{grid-template-columns:1fr}}}}</style>
<h1>B/D 全文阅读实验</h1><p class='intro'>验证问题：全文两阶段 B 是否已经足够好；混合路线 D 的定向补读和全文审校是否带来值得复杂度的真实提升。两组共用同一份全文 PaperModel 和同一 Writer。</p>
<h2>笔记并排比较</h2><div class='grid'><article><h3>B：全文两阶段</h3><pre>{esc(b_note)}</pre></article><article><h3>D：审校前</h3><pre>{esc(d_pre)}</pre></article><article><h3>D：最终</h3><pre>{esc(d_note)}</pre></article></div>
<h2>状态</h2><details open><summary>程序化对比</summary><pre>{esc(comparison)}</pre></details><details><summary>全文 PaperModel</summary><pre>{esc(paper_model)}</pre></details><details><summary>D 缺口计划</summary><pre>{esc(gap_plan)}</pre></details><details><summary>D 补读记录</summary><pre>{esc(records)}</pre></details><details><summary>D 全文审校</summary><pre>{esc(audit)}</pre></details>"""


def write_json(name: str, value: Any) -> None:
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
