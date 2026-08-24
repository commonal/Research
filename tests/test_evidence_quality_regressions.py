from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from unittest import TestCase

from research_pulse.knowledge.models import EvidenceAnchor, KnowledgeAsset, KnowledgeClaim
from research_pulse.production.evidence import EvidenceBlock, EvidenceCandidate, classify_candidate
from research_pulse.production.quality import validate_draft


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "evidence_quality_regressions.json").read_text(encoding="utf-8")
)
SOURCE_URL = "https://arxiv.org/abs/2608.18351v1"


class _SupportedJudge:
    def assess(self, *, claim: str, evidence: str) -> str:
        return "supported"


def _candidate(name: str, *, kind: str = "text", status: str = "available") -> EvidenceCandidate:
    item = FIXTURE[name]
    return EvidenceCandidate(
        block_id=item["block_id"],
        kind=kind,  # type: ignore[arg-type]
        text=item["text"],
        source_url=SOURCE_URL,
        parser="audit-fixture",
        parse_status=status,  # type: ignore[arg-type]
        locator_completeness="section_only",
        section_path=(item["section"],),
    )


def _asset(body: str) -> KnowledgeAsset:
    return KnowledgeAsset(
        knowledge_id="kp:arxiv:2608.18351v1",
        knowledge_version="audit-fixture-v1",
        publication_status="needs_review",
        evidence_level="full_text_text",
        source_urls=(SOURCE_URL,),
        domain="audit-fixture",
        title="Bounded evidence quality regression",
        body=body,
        content_sha256=sha256(body.encode("utf-8")).hexdigest(),
    )


def _complete_gate_inputs(body: str):
    specs = (
        ("problem", "Introduction", "The paper studies unnecessary execution risk."),
        ("method", "Method", "The policy learns a task-conditioned permission boundary."),
        ("experiment", "Results", "Proposed reaches 90.50% success."),
        ("limitation", "Limitations", "The study does not evaluate long-running tasks."),
    )
    anchors: dict[str, EvidenceAnchor] = {}
    fragments: dict[str, str] = {}
    blocks: dict[str, EvidenceBlock] = {}
    claims: list[KnowledgeClaim] = []
    for ordinal, (facet, section, text) in enumerate(specs, start=1):
        anchor_id = f"audit:{facet}"
        candidate = EvidenceCandidate(
            block_id=anchor_id,
            kind="text",
            text=text,
            source_url=SOURCE_URL,
            parser="audit-fixture",
            parse_status="available",
            locator_completeness="section_only",
            section_path=(section,),
        )
        anchors[anchor_id] = EvidenceAnchor(anchor_id, SOURCE_URL, section=section)
        fragments[anchor_id] = text
        blocks[anchor_id] = classify_candidate(candidate)
        claims.append(KnowledgeClaim(f"audit:claim:{ordinal}", "source_fact", text, (anchor_id,), facet))
    return _asset(body), tuple(claims), anchors, fragments, blocks


class EvidenceQualityRegressionTests(TestCase):
    def test_introduction_current_methods_remains_problem_evidence(self) -> None:
        block = classify_candidate(_candidate("introduction_problem"))

        self.assertEqual(block.supported_facets, ("problem",))

    def test_same_anchor_cannot_fill_problem_and_method(self) -> None:
        asset, claims, anchors, fragments, blocks = _complete_gate_inputs("A bounded reading note.")
        shared = _candidate("shared_problem_method")
        shared_block = EvidenceBlock(shared, True, ("problem", "method"))
        anchors[shared.block_id] = EvidenceAnchor(shared.block_id, SOURCE_URL, section="Introduction")
        fragments[shared.block_id] = shared.text
        blocks[shared.block_id] = shared_block
        duplicate_claims = (
            KnowledgeClaim("audit:claim:problem", "source_fact", shared.text, (shared.block_id,), "problem"),
            KnowledgeClaim("audit:claim:method", "source_fact", shared.text, (shared.block_id,), "method"),
            claims[2],
            claims[3],
        )

        result = validate_draft(
            asset=asset,
            claims=duplicate_claims,
            anchors=anchors,
            source_fragments=fragments,
            evidence_blocks=blocks,
            entailment_judge=_SupportedJudge(),
        )

        self.assertFalse(result.approved)
        self.assertIn("duplicate_required_facet_anchor", {issue.code for issue in result.issues})
        self.assertIn("missing_evidence_facet", {issue.code for issue in result.issues})
