from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest import TestCase

from research_pulse.knowledge.models import (
    DurableEvidenceAnchor,
    EvidenceAnchor,
    KnowledgeAsset,
    KnowledgeAssetError,
    KnowledgeClaim,
)
from research_pulse.production.quality import (
    _numeric_tokens,
    make_durable_anchor,
    validate_draft,
)
from research_pulse.production.evidence import EvidenceCandidate, classify_candidate


ROOT = Path(__file__).resolve().parents[1]


class _Judge:
    def __init__(self, verdict: str) -> None:
        self.verdict = verdict

    def assess(self, *, claim: str, evidence: str) -> str:
        return self.verdict


def _asset() -> KnowledgeAsset:
    asset = KnowledgeAsset.from_markdown(ROOT / "knowledge/fixtures/2026-08-22-validated-agent-memory.md")
    return replace(asset, publication_status="needs_review")


def _anchor() -> EvidenceAnchor:
    return EvidenceAnchor(
        anchor_id="anchor:long-term-memory",
        source_url="https://arxiv.org/abs/2606.10677v1",
        section="Method",
    )


class QualityGateTests(TestCase):
    def test_numeric_tokens_normalize_parser_typography(self) -> None:
        self.assertEqual(
            _numeric_tokens("1,020 episodes; p = 4 . 55 × 10 − 4; 83.6%"),
            ("1020", "4.55e-4", "83.6%"),
        )

    def test_durable_anchor_requires_locator_continuous_bounded_excerpt_and_valid_hash(self) -> None:
        anchor = replace(_anchor(), section=None)
        with self.assertRaisesRegex(KnowledgeAssetError, "section, page"):
            make_durable_anchor(anchor=anchor, source_fragment="Supported text.")
        with self.assertRaisesRegex(KnowledgeAssetError, "continuous"):
            make_durable_anchor(
                anchor=_anchor(), source_fragment="First sentence. Second sentence.", evidence_excerpt="First Second"
            )
        with self.assertRaisesRegex(KnowledgeAssetError, "1-1000"):
            DurableEvidenceAnchor(
                anchor_id="source:too-long",
                source_url=_anchor().source_url,
                evidence_excerpt="x" * 1_001,
                excerpt_sha256=sha256(("x" * 1_001).encode("utf-8")).hexdigest(),
                section="Method",
            )
        with self.assertRaisesRegex(KnowledgeAssetError, "excerpt_sha256"):
            DurableEvidenceAnchor(
                anchor_id="source:bad-hash",
                source_url=_anchor().source_url,
                evidence_excerpt="Supported text.",
                excerpt_sha256="0" * 64,
                section="Method",
            )

    def test_long_source_fragment_is_deterministically_bounded(self) -> None:
        durable = make_durable_anchor(anchor=_anchor(), source_fragment="x" * 1_500)

        self.assertEqual(len(durable.evidence_excerpt), 1_000)
        self.assertEqual(durable.excerpt_sha256, sha256(("x" * 1_000).encode("utf-8")).hexdigest())

    def test_anchor_source_outside_asset_blocks_publication(self) -> None:
        anchor = replace(_anchor(), source_url="https://example.com/other-paper")
        result = validate_draft(
            asset=_asset(),
            claims=(KnowledgeClaim("claim:1", "source_fact", "A supported statement.", (anchor.anchor_id,)),),
            anchors={anchor.anchor_id: anchor},
            source_fragments={anchor.anchor_id: "A supported statement."},
            entailment_judge=_Judge("supported"),
        )

        self.assertFalse(result.approved)
        self.assertEqual(result.issues[0].code, "anchor_source_outside_asset")

    def test_source_fact_without_anchor_blocks_publication(self) -> None:
        result = validate_draft(
            asset=_asset(),
            claims=[KnowledgeClaim("claim:1", "source_fact", "The method improves retrieval.", ())],
            anchors={},
            source_fragments={},
        )

        self.assertFalse(result.approved)
        self.assertEqual(result.issues[0].code, "source_fact_without_anchor")

    def test_source_fact_must_be_a_continuous_anchored_excerpt(self) -> None:
        anchor = _anchor()
        result = validate_draft(
            asset=_asset(),
            claims=[KnowledgeClaim("claim:1", "source_fact", "该方法提升了准确率。", (anchor.anchor_id,))],
            anchors={anchor.anchor_id: anchor},
            source_fragments={anchor.anchor_id: "The method reaches 90% accuracy."},
            entailment_judge=_Judge("supported"),
        )

        self.assertFalse(result.approved)
        self.assertEqual(result.issues[-1].code, "source_fact_not_verbatim")

    def test_number_not_in_anchor_blocks_publication(self) -> None:
        anchor = _anchor()
        result = validate_draft(
            asset=_asset(),
            claims=[KnowledgeClaim("claim:1", "source_fact", "The method reaches 95% accuracy.", (anchor.anchor_id,))],
            anchors={anchor.anchor_id: anchor},
            source_fragments={anchor.anchor_id: "The method reaches 90% accuracy."},
            entailment_judge=_Judge("supported"),
        )

        self.assertFalse(result.approved)
        self.assertEqual(result.issues[0].code, "number_not_in_anchor")

    def test_independent_unsupported_verdict_blocks_publication(self) -> None:
        anchor = _anchor()
        result = validate_draft(
            asset=_asset(),
            claims=[KnowledgeClaim("claim:1", "source_fact", "The method reaches 90% accuracy.", (anchor.anchor_id,))],
            anchors={anchor.anchor_id: anchor},
            source_fragments={anchor.anchor_id: "The method reaches 90% accuracy."},
            entailment_judge=_Judge("unsupported"),
        )

        self.assertFalse(result.approved)
        self.assertEqual(result.issues[0].code, "entailment_unsupported")

    def test_anchored_fact_can_pass_with_independent_support(self) -> None:
        anchor = _anchor()
        result = validate_draft(
            asset=_asset(),
            claims=[KnowledgeClaim("claim:1", "source_fact", "The method reaches 90% accuracy.", (anchor.anchor_id,))],
            anchors={anchor.anchor_id: anchor},
            source_fragments={anchor.anchor_id: "The method reaches 90% accuracy."},
            entailment_judge=_Judge("supported"),
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.issues, ())
        self.assertIsNotNone(result.bundle)
        assert result.bundle is not None
        self.assertEqual(result.bundle.claims[0].claim_type, "source_fact")
        self.assertEqual(result.bundle.anchors[0].evidence_excerpt, "The method reaches 90% accuracy.")

    def test_enhanced_gate_rejects_unusable_blocks_and_facet_mismatches(self) -> None:
        anchor = _anchor()
        table_block = classify_candidate(
            EvidenceCandidate(
                block_id=anchor.anchor_id,
                kind="table",
                text="| Model | Accuracy | | Parent | 67.75% | | Continuation | 90.50% |",
                source_url=anchor.source_url,
                parser="fixture",
                parse_status="available",
                locator_completeness="section_only",
                section_path=("Results",),
            )
        )
        result = validate_draft(
            asset=_asset(),
            claims=(KnowledgeClaim("claim:1", "source_fact", table_block.candidate.text, (anchor.anchor_id,), "method"),),
            anchors={anchor.anchor_id: anchor},
            source_fragments={anchor.anchor_id: table_block.candidate.text},
            evidence_blocks={anchor.anchor_id: table_block},
            entailment_judge=_Judge("supported"),
        )

        codes = {issue.code for issue in result.issues}
        self.assertIn("evidence_block_facet_mismatch", codes)
        self.assertIn("missing_evidence_facet", codes)
        self.assertFalse(result.approved)

    def test_enhanced_gate_requires_all_facets_from_eligible_blocks(self) -> None:
        source_url = _anchor().source_url
        specs = (
            ("problem", "Introduction", "This paper addresses stale memory retrieval."),
            ("method", "Method", "The method uses a version-aware memory index."),
            ("experiment", "Results", "The experiment reaches 90% accuracy."),
            ("limitation", "Limitations", "A limitation is that long-term effects are not evaluated."),
        )
        anchors: dict[str, EvidenceAnchor] = {}
        fragments: dict[str, str] = {}
        blocks = {}
        claims = []
        for ordinal, (facet, section, text) in enumerate(specs, start=1):
            anchor_id = f"anchor:{facet}"
            anchors[anchor_id] = EvidenceAnchor(anchor_id, source_url, section=section)
            fragments[anchor_id] = text
            blocks[anchor_id] = classify_candidate(
                EvidenceCandidate(
                    block_id=anchor_id,
                    kind="text",
                    text=text,
                    source_url=source_url,
                    parser="fixture",
                    parse_status="available",
                    locator_completeness="section_only",
                    section_path=(section,),
                )
            )
            claims.append(KnowledgeClaim(f"claim:{ordinal}", "source_fact", text, (anchor_id,), facet))

        result = validate_draft(
            asset=_asset(),
            claims=claims,
            anchors=anchors,
            source_fragments=fragments,
            evidence_blocks=blocks,
            entailment_judge=_Judge("supported"),
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.issues, ())
