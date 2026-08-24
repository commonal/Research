from __future__ import annotations

"""Finish the B/D prototype by replaying v3 state through a bounded global audit."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import json

import run_full_context_bd_v1 as bd


V3 = bd.ROOT / "evals" / "real-e2e" / "human-like-bd-v3"
OUTPUT = bd.ROOT / "evals" / "real-e2e" / "human-like-bd-v4"


def main() -> int:
    bd.OUTPUT = OUTPUT
    bd.load_dotenv_without_printing()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()

    normalized = bd.load_normalized_jsonl(bd.NORMALIZED)
    paper = bd.CanonicalPaperIR.from_normalized(
        source_id=bd.SOURCE_ID,
        title=bd.TITLE,
        source_url=bd.SOURCE_URL,
        blocks=normalized,
    )
    paper = replace(
        paper,
        blocks=tuple(
            replace(block, image_path=str(resolved))
            if block.image_path and (resolved := bd.resolve_image(block.image_path))
            else block
            for block in paper.blocks
        ),
    )
    model = bd.DeepSeekPaperReadingModel.from_environment()
    bd.write_json("provider-probe.json", model.probe())

    paper_model = bd.validate_paper_model(
        json.loads((V3 / "paper-model.json").read_text(encoding="utf-8")),
        paper,
    )
    gap_plan = json.loads((V3 / "d-gap-plan.json").read_text(encoding="utf-8"))
    records = []
    for target in gap_plan.get("targets", ()):
        raw = json.loads((V3 / f"d-raw-target-{target['target_id']}.json").read_text(encoding="utf-8"))
        selected = [paper.block_by_id[block_id] for block_id in target["selected_block_ids"]]
        records.append(bd.validate_target_record(raw, target, selected))

    b_note = (V3 / "b-reading-note.md").read_text(encoding="utf-8")
    (OUTPUT / "b-reading-note.md").write_text(b_note, encoding="utf-8")
    bd.write_json("paper-model.json", paper_model)
    bd.write_json("d-gap-plan.json", gap_plan)
    bd.write_json("d-targeted-records.json", records)

    d_pre_note = bd.write_chinese_note(model, paper_model, records, "D_pre_audit_v4")
    (OUTPUT / "d-pre-audit-note.md").write_text(d_pre_note, encoding="utf-8")

    full_document = [bd.full_block(block) for block in paper.ordered_blocks]
    raw_audit = bd.call_json(
        model,
        "bd_compact_global_audit",
        {
            "operation": "bounded_full_document_audit",
            "task": "Compare the draft with the complete ordered paper. Return only a short, evidence-linked correction plan; do not rewrite the note and do not comment on claims that are already correct.",
            "rules": [
                "Report at most 12 material issues that affect the causal story, mechanism, formula-symbol meanings, decisive numbers, or conclusion boundary.",
                "Every supported correction must be an atomic source fact with facet, statement, and exact source_block_ids.",
                "For an unsupported draft claim, quote only the shortest identifying phrase and state remove or rephrase; never infer the author's limitation from absent material.",
                "Do not echo the draft or paper. Do not produce a corrected note. If there is no material issue, return empty lists.",
            ],
            "draft_note": d_pre_note,
            "complete_ordered_paper": full_document,
            "output_contract": "Return a new JSON object with verdict, supported_corrections, unsupported_claims, and missing_critical_points. supported_corrections items use fact_id, facet, statement, source_block_ids. unsupported_claims items use draft_phrase, action, reason.",
        },
    )
    bd.write_json("d-raw-global-audit.json", raw_audit)
    if raw_audit.get("_fallback"):
        raise RuntimeError("The bounded full-document audit did not return valid JSON.")
    audit_facts = bd.validate_facts(raw_audit.get("supported_corrections", ()), paper.block_by_id)
    audit = {
        "record_type": "global_audit",
        "verdict": str(raw_audit.get("verdict", "unknown")),
        "source_facts": audit_facts,
        "claims_to_remove_or_correct": [
            dict(item) for item in raw_audit.get("unsupported_claims", ()) if isinstance(item, dict)
        ][:12],
        "missing_critical_points": [str(item) for item in raw_audit.get("missing_critical_points", ())][:12],
    }
    bd.write_json("d-global-audit.json", audit)

    d_note = bd.write_chinese_note(model, paper_model, [*records, audit], "D_v4")
    (OUTPUT / "d-reading-note.md").write_text(d_note, encoding="utf-8")
    comparison = bd.compare_notes(b_note, d_pre_note, d_note, paper_model, records)
    bd.write_json("comparison.json", comparison)
    (OUTPUT / "comparison.md").write_text(bd.comparison_markdown(comparison), encoding="utf-8")
    (OUTPUT / "trace-review.html").write_text(
        bd.trace_html(comparison, b_note, d_pre_note, d_note, paper_model, gap_plan, records, audit),
        encoding="utf-8",
    )
    bd.write_json(
        "run-meta.json",
        {
            "started_at": started,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "reused_from": str(V3),
            "reused_global_paper_model": True,
            "reused_b_note": True,
            "reused_gap_plan_and_target_model_outputs": True,
            "new_text_calls": model.text_call_count,
            "new_vision_calls": model.vision_call_count,
            "fallbacks": model.fallbacks,
            "validated_global_facts": len(paper_model["source_facts"]),
            "validated_target_facts": sum(len(record["source_facts"]) for record in records),
            "validated_audit_facts": len(audit_facts),
        },
    )
    print(json.dumps({"output": str(OUTPUT), "comparison": comparison, "audit": audit, "new_calls": bd.call_snapshot(model)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
