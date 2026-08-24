from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from unittest import TestCase

from research_pulse.knowledge.models import EvidenceAnchor, KnowledgeAsset, KnowledgeClaim
from research_pulse.production.adapters import _select_extraction_fragments
from research_pulse.production.evidence import EvidenceBlock, EvidenceCandidate, classify_candidate
from research_pulse.production.pipeline import (
    DeepReadingAnalysis,
    GroundedReadingSection,
    SourceMaterial,
)
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


def _reading_analysis(*, method: str | None = None, experiments: str | None = None) -> DeepReadingAnalysis:
    return DeepReadingAnalysis(
        summary=GroundedReadingSection("Bounded summary.", ("audit:problem",)),
        problem=GroundedReadingSection("The paper studies unnecessary execution risk.", ("audit:problem",)),
        method=GroundedReadingSection(
            method or "The policy learns a task-conditioned permission boundary.",
            ("audit:method",),
        ),
        experiments=GroundedReadingSection(
            experiments or "Proposed reaches 90.50% success.",
            ("audit:experiment",),
        ),
        limitations=GroundedReadingSection(
            "The study does not evaluate long-running tasks.",
            ("audit:limitation",),
        ),
        reproduction=GroundedReadingSection("Reproduce the task-conditioned policy.", ("audit:method",)),
        reading_boundary="Only bounded text evidence is used.",
    )


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

    def test_complete_result_outranks_and_excludes_short_table_title(self) -> None:
        problem = _candidate("introduction_problem")
        short_title = _candidate("short_table_title", kind="caption")
        complete_result = _candidate("complete_result")
        candidates = (problem, short_title, complete_result)
        blocks = {candidate.block_id: classify_candidate(candidate) for candidate in candidates}
        fragments = {candidate.block_id: candidate.text for candidate in candidates}
        material = SourceMaterial("full_text_text", {}, fragments, evidence_blocks=blocks)

        selected = _select_extraction_fragments(material, 60_000)

        self.assertIn(complete_result.block_id, selected)
        self.assertNotIn(short_title.block_id, selected)

    def test_reading_number_must_exist_in_same_section_durable_excerpt(self) -> None:
        asset, claims, anchors, fragments, blocks = _complete_gate_inputs(FIXTURE["reading_number"])

        result = validate_draft(
            asset=asset,
            claims=claims,
            anchors=anchors,
            source_fragments=fragments,
            evidence_blocks=blocks,
            reading_analysis=_reading_analysis(experiments=FIXTURE["reading_number"]),
            entailment_judge=_SupportedJudge(),
        )

        self.assertFalse(result.approved)
        self.assertIn("reading_number_not_in_durable_excerpt", {issue.code for issue in result.issues})

    def test_reading_number_in_same_section_durable_excerpt_can_pass(self) -> None:
        asset, claims, anchors, fragments, blocks = _complete_gate_inputs("A bounded reading note.")

        result = validate_draft(
            asset=asset,
            claims=claims,
            anchors=anchors,
            source_fragments=fragments,
            evidence_blocks=blocks,
            reading_analysis=_reading_analysis(),
            entailment_judge=_SupportedJudge(),
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.issues, ())
        self.assertIsNotNone(result.bundle)
        assert result.bundle is not None
        mapping = {section.section_name: section.anchor_ids for section in result.bundle.reading_sections}
        self.assertEqual(mapping["experiments"], ("audit:experiment",))

    def test_non_empty_reading_section_without_valid_anchor_is_blocked(self) -> None:
        asset, claims, anchors, fragments, blocks = _complete_gate_inputs("A bounded reading note.")
        analysis = replace(
            _reading_analysis(),
            method=GroundedReadingSection("The policy learns a task-conditioned permission boundary.", ()),
        )

        result = validate_draft(
            asset=asset,
            claims=claims,
            anchors=anchors,
            source_fragments=fragments,
            evidence_blocks=blocks,
            reading_analysis=analysis,
            entailment_judge=_SupportedJudge(),
        )

        self.assertFalse(result.approved)
        self.assertIn("reading_section_missing_evidence", {issue.code for issue in result.issues})

    def test_reading_number_requires_an_exact_numeric_token_match(self) -> None:
        asset, claims, anchors, fragments, blocks = _complete_gate_inputs("A bounded reading note.")
        source_text = "The policy is evaluated across 312 tasks."
        fragments["audit:method"] = source_text
        blocks["audit:method"] = classify_candidate(
            EvidenceCandidate(
                block_id="audit:method",
                kind="text",
                text=source_text,
                source_url=SOURCE_URL,
                parser="audit-fixture",
                parse_status="available",
                locator_completeness="section_only",
                section_path=("Method",),
            )
        )
        claims = (
            claims[0],
            replace(claims[1], text=source_text),
            claims[2],
            claims[3],
        )
        analysis = replace(
            _reading_analysis(),
            method=GroundedReadingSection("The policy is evaluated across 12 tasks.", ("audit:method",)),
        )

        result = validate_draft(
            asset=asset,
            claims=claims,
            anchors=anchors,
            source_fragments=fragments,
            evidence_blocks=blocks,
            reading_analysis=analysis,
            entailment_judge=_SupportedJudge(),
        )

        self.assertFalse(result.approved)
        self.assertIn("reading_number_not_in_durable_excerpt", {issue.code for issue in result.issues})

    def test_reading_list_numbers_are_not_treated_as_paper_metrics(self) -> None:
        asset, claims, anchors, fragments, blocks = _complete_gate_inputs("A bounded reading note.")
        analysis = replace(
            _reading_analysis(),
            method=GroundedReadingSection(
                "1) 先选择任务 schema；2) 再执行 broker 检查。",
                ("audit:method",),
            ),
        )

        result = validate_draft(
            asset=asset,
            claims=claims,
            anchors=anchors,
            source_fragments=fragments,
            evidence_blocks=blocks,
            reading_analysis=analysis,
            entailment_judge=_SupportedJudge(),
        )

        self.assertTrue(result.approved)

    def test_table_comparison_requires_metric_objects_and_values_in_one_excerpt(self) -> None:
        asset, claims, anchors, fragments, blocks = _complete_gate_inputs("A bounded table reading note.")
        table_text = "| Accuracy | Baseline | 67.75% |"
        table_candidate = EvidenceCandidate(
            block_id="audit:experiment",
            kind="table",
            text=table_text,
            source_url=SOURCE_URL,
            parser="audit-fixture",
            parse_status="available",
            locator_completeness="section_only",
            section_path=("Results",),
        )
        blocks[table_candidate.block_id] = EvidenceBlock(table_candidate, True, ("experiment",))
        fragments[table_candidate.block_id] = table_text
        claims = (*claims[:2], KnowledgeClaim("audit:claim:table", "source_fact", table_text, (table_candidate.block_id,), "experiment"), claims[3])
        analysis = _reading_analysis(experiments="Baseline reports 67.75% accuracy.")

        result = validate_draft(
            asset=asset,
            claims=claims,
            anchors=anchors,
            source_fragments=fragments,
            evidence_blocks=blocks,
            reading_analysis=analysis,
            entailment_judge=_SupportedJudge(),
        )

        self.assertFalse(result.approved)
        self.assertIn("reading_table_comparison_incomplete", {issue.code for issue in result.issues})

    def test_unparsed_formula_cannot_be_expanded_in_reading_body(self) -> None:
        asset, claims, anchors, fragments, blocks = _complete_gate_inputs(FIXTURE["formula_expansion"])
        formula = EvidenceCandidate(
            block_id="audit:formula",
            kind="formula",
            text="formula-not-decoded",
            source_url=SOURCE_URL,
            parser="audit-fixture",
            parse_status="unparsed",
            locator_completeness="section_only",
            section_path=("Method",),
        )
        blocks[formula.block_id] = classify_candidate(formula)

        result = validate_draft(
            asset=asset,
            claims=claims,
            anchors=anchors,
            source_fragments=fragments,
            evidence_blocks=blocks,
            reading_analysis=_reading_analysis(method=FIXTURE["formula_expansion"]),
            entailment_judge=_SupportedJudge(),
        )

        self.assertFalse(result.approved)
        self.assertIn("reading_formula_not_durable", {issue.code for issue in result.issues})
