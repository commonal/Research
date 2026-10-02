from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest import TestCase

from research_pulse.knowledge.models import (
    DurableEvidenceAnchor,
    KnowledgeAsset,
    KnowledgeAssetError,
    KnowledgeBundle,
    KnowledgeClaim,
    ReadingSectionEvidence,
    ReadingVisualEvidence,
    bundle_from_provenance_payload,
    render_provenance,
    split_front_matter,
)
from research_pulse.rag.contracts import EvidenceHit, SearchRequest


ROOT = Path(__file__).resolve().parents[1]


class KnowledgeContractTests(TestCase):
    def test_validated_asset_has_a_deterministic_body_hash(self) -> None:
        asset = KnowledgeAsset.from_markdown(
            ROOT / "knowledge" / "fixtures" / "2026-08-22-validated-agent-memory.md"
        )

        self.assertEqual(asset.knowledge_id, "kp:arxiv:2606.10677")
        self.assertEqual(asset.publication_status, "published")
        self.assertEqual(asset.content_sha256, "188cccab82ec54277d2d554da37d6eb7769e1b51c9037b01b1cd713295a01531")

    def test_wrong_body_hash_cannot_be_published(self) -> None:
        path = ROOT / "knowledge" / "fixtures" / "2026-08-22-validated-agent-memory.md"
        changed = path.read_text(encoding="utf-8").replace("长期运行", "长期持续运行")

        path = _write_fixture(changed)
        try:
            with self.assertRaisesRegex(KnowledgeAssetError, "content_sha256"):
                KnowledgeAsset.from_markdown(path)
        finally:
            path.unlink(missing_ok=True)

    def test_front_matter_rejects_non_json_values(self) -> None:
        with self.assertRaisesRegex(KnowledgeAssetError, "JSON"):
            split_front_matter("---\ntitle: unquoted\n---\nbody")

    def test_content_address_source_is_accepted(self) -> None:
        digest = "a" * 64
        markdown = (
            "---\n"
            'knowledge_id: "kp:test"\n'
            'knowledge_version: "2026-09-02T00:00:00+00:00"\n'
            'publication_status: "published"\n'
            'evidence_level: "source_linked_unverified"\n'
            f'source_urls: ["urn:sha256:{digest}"]\n'
            'domain: "test"\n'
            'title: "Test"\n'
            "---\n"
            "# hello\n"
        )
        path = _write_fixture(markdown)
        try:
            asset = KnowledgeAsset.from_markdown(path)
            self.assertEqual(asset.source_urls, (f"urn:sha256:{digest}",))
        finally:
            path.unlink(missing_ok=True)

    def test_evidence_hit_keeps_asset_version_and_anchor(self) -> None:
        request = SearchRequest(query="长期记忆", domain="llm_agent_memory")
        hit = EvidenceHit(
            chunk_id="chunk:1",
            knowledge_id="kp:arxiv:2606.10677",
            knowledge_version="2026-08-22T00:00:00Z",
            title="Infini Memory",
            text="主题记忆可演化。",
            source_url="https://arxiv.org/abs/2606.10677v1",
            anchor_id="anchor:section:problem",
            dense_score=0.8,
            keyword_score=0.4,
            fused_score=0.9,
        )

        self.assertEqual(request.limit, 8)
        self.assertEqual(hit.anchor_id, "anchor:section:problem")

    def test_bundle_rejects_duplicate_claim_and_anchor_ids(self) -> None:
        asset = _published_asset()
        anchor = _durable_anchor(asset)
        claim = KnowledgeClaim("claim:1", "source_fact", "The method reaches 90% accuracy.", (anchor.anchor_id,))

        with self.assertRaisesRegex(KnowledgeAssetError, "Claim IDs"):
            KnowledgeBundle(asset, (claim, claim), (anchor,))
        with self.assertRaisesRegex(KnowledgeAssetError, "Anchor IDs"):
            KnowledgeBundle(asset, (claim,), (anchor, anchor))

    def test_source_fact_requires_a_resolvable_durable_anchor(self) -> None:
        asset = _published_asset()
        claim = KnowledgeClaim("claim:1", "source_fact", "A fact.", ("source:missing",))

        with self.assertRaisesRegex(KnowledgeAssetError, "missing durable anchor"):
            KnowledgeBundle(asset, (claim,), ())

    def test_sidecar_must_match_markdown_identity_and_hash(self) -> None:
        asset = _published_asset()
        anchor = _durable_anchor(asset)
        bundle = KnowledgeBundle(
            asset,
            (KnowledgeClaim("claim:1", "source_fact", anchor.evidence_excerpt, (anchor.anchor_id,)),),
            (anchor,),
        )
        payload = {
            "schema_version": 1,
            "knowledge_id": "kp:wrong",
            "knowledge_version": asset.knowledge_version,
            "claims": [],
            "anchors": [],
        }
        with self.assertRaisesRegex(KnowledgeAssetError, "ID/version"):
            bundle_from_provenance_payload(asset, payload)

        root = ROOT / "data"
        root.mkdir(parents=True, exist_ok=True)
        sidecar = root / "test-sample.provenance.json"
        markdown = root / "test-sample.md"
        try:
            sidecar.write_text(render_provenance(bundle), encoding="utf-8")
            persisted = replace(
                asset,
                schema_version=2,
                provenance_file=sidecar.name,
                provenance_sha256="0" * 64,
            )
            markdown.write_text(_render_test_asset(persisted), encoding="utf-8")
            with self.assertRaisesRegex(KnowledgeAssetError, "provenance_sha256"):
                KnowledgeBundle.from_markdown(markdown)
        finally:
            markdown.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)

    def test_provenance_serialization_is_deterministic_and_round_trips(self) -> None:
        asset = _published_asset()
        anchor = _durable_anchor(asset)
        bundle = KnowledgeBundle(
            asset,
            (KnowledgeClaim("claim:1", "source_fact", anchor.evidence_excerpt, (anchor.anchor_id,)),),
            (anchor,),
        )

        first = render_provenance(bundle)
        second = render_provenance(bundle)

        self.assertEqual(first.encode("utf-8"), second.encode("utf-8"))
        self.assertEqual(bundle_from_provenance_payload(asset, __import__("json").loads(first)), bundle)

    def test_extended_anchor_fields_round_trip_without_backfilling_v2_fixture(self) -> None:
        asset = _published_asset()
        excerpt = "The method reaches 90% accuracy."
        anchor = DurableEvidenceAnchor(
            anchor_id="source:test:table:1",
            source_url=asset.source_urls[0],
            evidence_excerpt=excerpt,
            excerpt_sha256=sha256(excerpt.encode("utf-8")).hexdigest(),
            section="Results",
            page_start=4,
            figure_or_table="Table 1: Accuracy comparison",
            block_kind="table",
            parse_status="available",
            locator_completeness="exact",
            bbox=(1.0, 2.0, 3.0, 4.0),
        )
        bundle = KnowledgeBundle(
            asset,
            (KnowledgeClaim("claim:1", "source_fact", excerpt, (anchor.anchor_id,), "experiment"),),
            (anchor,),
        )
        reparsed = bundle_from_provenance_payload(asset, __import__("json").loads(render_provenance(bundle)))
        legacy_v2 = KnowledgeBundle.from_markdown(ROOT / "knowledge" / "fixtures" / "2026-08-22-provenance-sample.md")

        self.assertEqual(reparsed.anchors[0].block_kind, "table")
        self.assertEqual(reparsed.anchors[0].bbox, (1.0, 2.0, 3.0, 4.0))
        self.assertEqual(reparsed.claims[0].source_facet, "experiment")
        self.assertIsNone(legacy_v2.anchors[0].block_kind)
        self.assertIsNone(legacy_v2.claims[0].source_facet)

    def test_v3_reading_section_anchor_mapping_round_trips_without_copying_section_text(self) -> None:
        asset = replace(_published_asset(), schema_version=3)
        anchor = _durable_anchor(asset)
        mapping = ReadingSectionEvidence("method", (anchor.anchor_id,))
        bundle = KnowledgeBundle(
            asset,
            (KnowledgeClaim("claim:1", "source_fact", anchor.evidence_excerpt, (anchor.anchor_id,), "method"),),
            (anchor,),
            reading_sections=(mapping,),
        )

        rendered = render_provenance(bundle)
        reparsed = bundle_from_provenance_payload(asset, __import__("json").loads(rendered))

        self.assertEqual(reparsed.reading_sections, (mapping,))
        self.assertIn('"schema_version":2', rendered)
        self.assertNotIn("The method reaches 90% accuracy.", __import__("json").loads(rendered)["reading_sections"][0].values())

    def test_selected_visual_metadata_round_trips_without_absolute_asset_path(self) -> None:
        asset = replace(_published_asset(), schema_version=3)
        anchor = _durable_anchor(asset)
        visual = ReadingVisualEvidence(
            block_id="source:formula:1",
            kind="formula",
            role="mechanism",
            explanation="说明动作如何进入验证器。",
        )
        mapping = ReadingSectionEvidence("method", (anchor.anchor_id,), (visual,))
        bundle = KnowledgeBundle(
            asset,
            (KnowledgeClaim("claim:1", "source_fact", anchor.evidence_excerpt, (anchor.anchor_id,), "method"),),
            (anchor,),
            reading_sections=(mapping,),
        )
        rendered = render_provenance(bundle)
        reparsed = bundle_from_provenance_payload(asset, __import__("json").loads(rendered))
        self.assertEqual(reparsed.reading_sections[0].visuals, (visual,))
        self.assertNotIn("C:\\", rendered)

    def test_visual_asset_path_cannot_escape_version_directory(self) -> None:
        asset = replace(_published_asset(), schema_version=3)
        with self.assertRaisesRegex(KnowledgeAssetError, "safe relative"):
            KnowledgeBundle(asset, (), (), visual_assets={"assets/../outside.png": Path("C:/cache/fig.png")})

    def test_reading_section_mapping_cannot_reference_non_fact_or_missing_anchor(self) -> None:
        asset = _published_asset()
        anchor = _durable_anchor(asset)

        with self.assertRaisesRegex(KnowledgeAssetError, "approved source_fact"):
            KnowledgeBundle(
                asset,
                (KnowledgeClaim("claim:inference", "agent_inference", "Inference.", ()),),
                (anchor,),
                reading_sections=(ReadingSectionEvidence("method", (anchor.anchor_id,)),),
            )

    def test_legacy_markdown_is_readable_but_not_answer_eligible(self) -> None:
        bundle = KnowledgeBundle.from_markdown(
            ROOT / "knowledge" / "fixtures" / "2026-08-22-validated-agent-memory.md"
        )

        self.assertEqual(bundle.provenance_status, "legacy_missing_provenance")
        self.assertFalse(bundle.answer_eligible)
        self.assertEqual(bundle.claims, ())

    def test_v2_fixture_reparses_with_matching_body_and_provenance_hashes(self) -> None:
        path = ROOT / "knowledge" / "fixtures" / "2026-08-22-provenance-sample.md"

        bundle = KnowledgeBundle.from_markdown(path)

        self.assertTrue(bundle.answer_eligible)
        self.assertEqual(bundle.provenance_status, "complete")
        self.assertEqual(len(bundle.claims), 3)
        self.assertEqual(bundle.reading_sections, ())
        self.assertEqual(sha256(bundle.asset.body.encode("utf-8")).hexdigest(), bundle.asset.content_sha256)
        assert bundle.asset.provenance_file and bundle.asset.provenance_sha256
        self.assertEqual(
            sha256((path.parent / bundle.asset.provenance_file).read_bytes()).hexdigest(),
            bundle.asset.provenance_sha256,
        )


def _write_fixture(markdown: str) -> Path:
    """Use a deterministic, local test file and remove it in tearDown-free form."""

    path = ROOT / "data" / "test-invalid-asset.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return path


def _published_asset() -> KnowledgeAsset:
    asset = KnowledgeAsset.from_markdown(
        ROOT / "knowledge" / "fixtures" / "2026-08-22-validated-agent-memory.md"
    )
    return replace(asset, publication_status="published")


def _durable_anchor(asset: KnowledgeAsset) -> DurableEvidenceAnchor:
    excerpt = "The method reaches 90% accuracy."
    return DurableEvidenceAnchor(
        anchor_id="source:test:method:1",
        source_url=asset.source_urls[0],
        evidence_excerpt=excerpt,
        excerpt_sha256=sha256(excerpt.encode("utf-8")).hexdigest(),
        section="Method",
    )


def _render_test_asset(asset: KnowledgeAsset) -> str:
    import json

    fields = {
        "knowledge_id": asset.knowledge_id,
        "knowledge_version": asset.knowledge_version,
        "publication_status": asset.publication_status,
        "evidence_level": asset.evidence_level,
        "source_urls": list(asset.source_urls),
        "domain": asset.domain,
        "title": asset.title,
        "content_sha256": asset.content_sha256,
        "schema_version": asset.schema_version,
        "provenance_file": asset.provenance_file,
        "provenance_sha256": asset.provenance_sha256,
    }
    header = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items())
    return f"---\n{header}\n---\n{asset.body}"
