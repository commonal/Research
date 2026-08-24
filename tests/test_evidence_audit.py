from __future__ import annotations

import json
from pathlib import Path
from unittest import TestCase

from research_pulse.production.evidence import EvidenceCandidate, classify_candidate
from research_pulse.production.evidence_audit import audit_material, write_audit
from research_pulse.production.pipeline import SourceMaterial


def _candidate(block_id: str, *, kind: str = "text", text: str, section: str = "Introduction", status: str = "available") -> EvidenceCandidate:
    return EvidenceCandidate(
        block_id=block_id,
        kind=kind,  # type: ignore[arg-type]
        text=text,
        source_url="https://arxiv.org/abs/2608.04746v1",
        parser="fixture",
        parse_status=status,  # type: ignore[arg-type]
        locator_completeness="section_only",
        section_path=(section,),
    )


class EvidenceAuditTests(TestCase):
    def test_audit_exposes_only_safe_coverage_and_rejection_metadata(self) -> None:
        blocks = {
            "problem": classify_candidate(_candidate("problem", text="This paper addresses stale evidence.")),
            "method": classify_candidate(_candidate("method", text="The method uses verified blocks.", section="Method")),
            "table": classify_candidate(_candidate("table", kind="table", text="| Model | Accuracy | | Parent | 67.75% | | Ours | 90.50% |", section="Results")),
            "formula": classify_candidate(_candidate("formula", kind="formula", text="formula-not-decoded", status="unparsed")),
        }
        material = SourceMaterial("full_text_text", {}, {}, evidence_blocks=blocks)

        audit = audit_material(material)

        self.assertEqual(audit.total_blocks, 4)
        self.assertEqual(audit.eligible_by_facet["experiment"], ("table",))
        self.assertEqual(audit.missing_facets, ("limitation",))
        self.assertFalse(audit.automatic_publication_possible)
        self.assertEqual(audit.rejection_counts, {"parse_unparsed": 1})
        self.assertNotIn("formula-not-decoded", repr(audit))

    def test_audit_writer_persists_metadata_not_parser_text(self) -> None:
        material = SourceMaterial(
            "full_text_text",
            {},
            {},
            evidence_blocks={"block": classify_candidate(_candidate("block", text="private parser body"))},
        )
        target = Path(__file__).resolve().parents[1] / "data" / "test-evidence-audit.json"
        try:
            write_audit(audit_material(material), target)
            text = target.read_text(encoding="utf-8")
            payload = json.loads(text)
        finally:
            target.unlink(missing_ok=True)

        self.assertEqual(payload["eligible_block_ids"], ["block"])
        self.assertNotIn("private parser body", text)
        self.assertEqual(payload["persistence"], "metadata_only; no PDF, parser text, Markdown, image, or model response")
