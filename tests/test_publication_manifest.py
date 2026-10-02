import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from research_pulse.pedagogical.contracts import (
    ArtifactQualityIssue,
    ArtifactQualityReport,
    AssetUsageLedger,
    BlindReaderResult,
    EvidenceGateResult,
    PedagogicalResult,
    RenderedNote,
)
from research_pulse.pedagogical.publication import PublicationAssetPublisher
from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import CanonicalPaperIR, DeepSeekPaperReadingModel, PaperIRBlock
from research_pulse.reader_production import ReaderConfig, ReaderProductionService


class PublicationManifestTests(unittest.TestCase):
    def test_fatal_pedagogical_fallback_persists_both_error_summaries(self) -> None:
        class _FatalPipeline:
            def run(self, paper):
                raise ValueError("renderer failed at wrapped asset")

        paper = CanonicalPaperIR(
            "fatal-pedagogical-attempt",
            "Fatal attempt",
            (PaperIRBlock("intro", "paragraph", "Introduction", "Readable body.", 0),),
        )
        with mock.patch.object(DeepSeekPaperReadingModel, "from_environment", return_value=object()):
            service = ReaderProductionService(
                ReaderConfig(normalized_root=Path("tmp/publication-manifest-tests/normalized"), mode="pedagogical"),
                Path("tmp/publication-manifest-tests/fatal-pedagogical-attempt"),
                pedagogical=_FatalPipeline(),
            )
        service.reader.resolver.resolve = lambda candidate: paper  # type: ignore[method-assign]
        service.reader.read = lambda candidate: SimpleNamespace(  # type: ignore[method-assign]
            draft=SimpleNamespace(markdown="# generic 兜底\n\n这是结构完整的兜底笔记。"),
            receipt=SimpleNamespace(status="completed", stop_reason="completed"),
        )

        result = service.process(PaperCandidate(
            paper.source_id, paper.title, "https://example.test", "ai"
        ))

        attempts = result["pedagogical_failure"]["attempts"]
        self.assertEqual(2, len(attempts))
        self.assertEqual("ValueError", attempts[0]["error_type"])
        self.assertEqual("renderer failed at wrapped asset", attempts[1]["summary"])

    def test_pedagogical_gate_fallback_persists_rejected_attempt_evidence(self) -> None:
        class _RejectedPedagogicalAttempt:
            def run(self, paper):
                return PedagogicalResult(
                    note=RenderedNote("# 教学化候选稿\n\n{{asset:figure-03}}"),
                    evidence=EvidenceGateResult(passed=True),
                    artifact=ArtifactQualityReport(
                        passed=False,
                        issues=(ArtifactQualityIssue(
                            code="duplicate_visible_caption",
                            message="图片后重复显示英文 caption",
                            line=3,
                        ),),
                    ),
                    blind=BlindReaderResult(overall="pass"),
                    repaired=True,
                    asset_usage=AssetUsageLedger(
                        selected_ids=("figure-03",),
                        anchored_ids=("figure-03",),
                        rendered_ids=("figure-03",),
                    ),
                )

        paper = CanonicalPaperIR(
            "rejected-pedagogical-attempt",
            "Rejected attempt",
            (PaperIRBlock("intro", "paragraph", "Introduction", "Readable body.", 0),),
        )
        vault = Path("tmp/publication-manifest-tests/rejected-pedagogical-attempt")
        with mock.patch.object(DeepSeekPaperReadingModel, "from_environment", return_value=object()):
            service = ReaderProductionService(
                ReaderConfig(normalized_root=Path("tmp/publication-manifest-tests/normalized"), mode="pedagogical"),
                vault,
                pedagogical=_RejectedPedagogicalAttempt(),
            )
        service.reader.resolver.resolve = lambda candidate: paper  # type: ignore[method-assign]
        service.reader.read = lambda candidate: SimpleNamespace(  # type: ignore[method-assign]
            draft=SimpleNamespace(markdown="# generic 兜底\n\n这是结构完整的兜底笔记。"),
            receipt=SimpleNamespace(status="completed", stop_reason="completed"),
        )

        result = service.process(PaperCandidate(
            paper.source_id, paper.title, "https://example.test", "ai"
        ))

        attempt = result["pedagogical_attempt"]
        self.assertFalse(attempt["gate_artifact_passed"])
        self.assertEqual("duplicate_visible_caption", attempt["gate_artifact_issue_details"][0]["code"])
        self.assertEqual(["figure-03"], attempt["asset_usage"]["selected"])
        self.assertTrue(attempt["repaired"])
        candidate_path = Path(attempt["candidate_path"])
        self.assertTrue(candidate_path.is_file())
        self.assertIn("教学化候选稿", candidate_path.read_text(encoding="utf-8"))

    def test_unreferenced_selected_asset_forces_pedagogical_fallback(self) -> None:
        class _IncompleteAssetCommitment:
            def run(self, paper):
                return PedagogicalResult(
                    note=RenderedNote("# 教学化笔记\n\n只使用了一部分 Planner 选中的素材。"),
                    evidence=EvidenceGateResult(passed=True),
                    blind=BlindReaderResult(overall="pass"),
                    asset_usage=AssetUsageLedger(
                        selected_ids=("figure-01", "table-01"),
                        anchored_ids=("figure-01",),
                        rendered_ids=("figure-01",),
                        unreferenced_selected_ids=("table-01",),
                    ),
                )

        paper = CanonicalPaperIR(
            "unreferenced-assets",
            "Assets",
            (PaperIRBlock("intro", "paragraph", "Introduction", "Readable body.", 0),),
        )
        with mock.patch.object(DeepSeekPaperReadingModel, "from_environment", return_value=object()):
            service = ReaderProductionService(
                ReaderConfig(normalized_root=Path("tmp/publication-manifest-tests/normalized"), mode="pedagogical"),
                Path("tmp/publication-manifest-tests/unreferenced-assets"),
                pedagogical=_IncompleteAssetCommitment(),
            )
        service.reader.resolver.resolve = lambda candidate: paper  # type: ignore[method-assign]
        service.reader.read = lambda candidate: SimpleNamespace(  # type: ignore[method-assign]
            draft=SimpleNamespace(markdown="# generic 兜底\n\n这是结构完整的兜底笔记。"),
            receipt=SimpleNamespace(status="completed", stop_reason="completed"),
        )

        result = service.process(
            PaperCandidate("unreferenced-assets", "Assets", "https://example.test", "ai")
        )

        self.assertEqual("generic", result["delivered_route"])
        self.assertEqual(["gates:unreferenced_assets"], result["pedagogical_fallbacks"])

    def test_clean_generic_fallback_passes_the_same_final_publication_gate(self) -> None:
        class _NeedsFallback:
            def run(self, paper):
                return PedagogicalResult(
                    note=RenderedNote("教学化草稿"),
                    evidence=EvidenceGateResult(passed=False, issues=("证据不足",)),
                    blind=BlindReaderResult(overall="pass"),
                )

        paper = CanonicalPaperIR(
            "clean-fallback",
            "Fallback",
            (PaperIRBlock("intro", "paragraph", "Introduction", "Readable body.", 0),),
        )
        with mock.patch.object(DeepSeekPaperReadingModel, "from_environment", return_value=object()):
            service = ReaderProductionService(
                ReaderConfig(normalized_root=Path("tmp/publication-manifest-tests/normalized"), mode="pedagogical"),
                Path("tmp/publication-manifest-tests/clean-fallback"),
                pedagogical=_NeedsFallback(),
            )
        service.reader.resolver.resolve = lambda candidate: paper  # type: ignore[method-assign]
        service.reader.read = lambda candidate: SimpleNamespace(  # type: ignore[method-assign]
            draft=SimpleNamespace(markdown="# generic 兜底\n\n这是结构完整且可以发布的阅读笔记。"),
            receipt=SimpleNamespace(status="completed", stop_reason="completed"),
        )

        result = service.process(
            PaperCandidate("clean-fallback", "Fallback", "https://example.test", "ai")
        )

        self.assertEqual("published", result["status"])
        self.assertEqual("pedagogical", result["requested_route"])
        self.assertEqual("generic", result["delivered_route"])
        self.assertTrue(result["fallback_used"])
        self.assertEqual(["gates:evidence"], result["pedagogical_fallbacks"])
        self.assertTrue(result["gate_artifact_passed"])
        self.assertEqual([], result["publication_manifest"]["missing"])
        self.assertTrue(Path(result["published_path"]).is_file())
        receipt_path = Path(result["receipt_path"])
        self.assertTrue(receipt_path.is_file())
        persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual("pedagogical", persisted["requested_route"])
        self.assertEqual("generic", persisted["delivered_route"])
        self.assertEqual(["gates:evidence"], persisted["pedagogical_fallbacks"])

    def test_bad_generic_fallback_is_rejected_by_final_publication_gate(self) -> None:
        class _BadPedagogicalPipeline:
            def run(self, paper):
                return PedagogicalResult(
                    note=RenderedNote("# 教学化草稿\n\nresolu- tion"),
                    evidence=EvidenceGateResult(passed=False, issues=("证据不足",)),
                    blind=BlindReaderResult(overall="pass"),
                )

        paper = CanonicalPaperIR(
            "generic-fallback",
            "Fallback",
            (PaperIRBlock("intro", "paragraph", "Introduction", "Readable body.", 0),),
        )
        vault = Path("tmp/publication-manifest-tests/unified-final-gate")
        with mock.patch.object(DeepSeekPaperReadingModel, "from_environment", return_value=object()):
            service = ReaderProductionService(
                ReaderConfig(normalized_root=Path("tmp/publication-manifest-tests/normalized"), mode="pedagogical"),
                vault,
                pedagogical=_BadPedagogicalPipeline(),
            )
        service.reader.resolver.resolve = lambda candidate: paper  # type: ignore[method-assign]
        service.reader.read = lambda candidate: SimpleNamespace(  # type: ignore[method-assign]
            draft=SimpleNamespace(
                markdown=(
                    "# generic 兜底笔记\n\n"
                    "这是一段足够长的重复段落，不应该被发布到正式知识库，因为它会明显损害最终阅读体验。\n\n"
                    "这是一段足够长的重复段落，不应该被发布到正式知识库，因为它会明显损害最终阅读体验。"
                )
            ),
            receipt=SimpleNamespace(status="completed", stop_reason="completed"),
        )

        result = service.process(
            PaperCandidate("generic-fallback", "Fallback", "https://example.test", "ai")
        )

        self.assertEqual(["gates:evidence"], result["pedagogical_fallbacks"])
        self.assertEqual("needs_review", result["status"])
        self.assertEqual("artifact_quality:duplicate_prose_paragraph", result["stop_reason"])
        self.assertIsNone(result["published_path"])
        persisted = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual("needs_review", persisted["status"])
        self.assertEqual("generic", persisted["delivered_route"])
        self.assertTrue(persisted["fallback_used"])
        self.assertEqual(
            "duplicate_prose_paragraph",
            persisted["gate_artifact_issue_details"][0]["code"],
        )
        self.assertIsInstance(persisted["gate_artifact_issue_details"][0]["line"], int)
        candidate_path = Path(persisted["audit_candidate_path"])
        self.assertTrue(candidate_path.is_file())
        self.assertIn("generic 兜底笔记", candidate_path.read_text(encoding="utf-8"))

    def test_missing_referenced_asset_fails_preflight_without_copying(self) -> None:
        root = Path("tmp/publication-manifest-tests/missing-source")
        paper = CanonicalPaperIR(
            "paper-1",
            "Missing image",
            (
                PaperIRBlock("intro", "paragraph", "Introduction", "Readable body.", 0),
                PaperIRBlock(
                    "figure-1", "figure", "Method", "Figure 1", 1,
                    image_path="images/missing.png", safe_image=True,
                ),
            ),
        )
        assets_dir = root / "vault" / "assets"

        manifest = PublicationAssetPublisher(root / "normalized").publish(
            paper,
            "# Note\n\n![图 1](assets/missing.png)",
            assets_dir,
        )

        self.assertFalse(manifest.passed)
        self.assertEqual(("missing.png",), manifest.referenced_files)
        self.assertEqual(("missing.png",), manifest.missing_files)
        self.assertEqual((), manifest.copied_files)
        self.assertFalse(assets_dir.exists())

    def test_same_basename_from_distinct_sources_is_a_collision(self) -> None:
        root = Path("tmp/publication-manifest-tests/collision")
        paper = CanonicalPaperIR(
            "paper-2",
            "Colliding images",
            (
                PaperIRBlock("intro", "paragraph", "Introduction", "Readable body.", 0),
                PaperIRBlock("figure-1", "figure", "Method", "Figure 1", 1,
                             image_path="images/first/figure.png", safe_image=True),
                PaperIRBlock("figure-2", "figure", "Method", "Figure 2", 2,
                             image_path="images/second/figure.png", safe_image=True),
            ),
        )
        assets_dir = root / "vault" / "assets"

        manifest = PublicationAssetPublisher(root / "normalized").publish(
            paper,
            "# Note\n\n![图](assets/figure.png)",
            assets_dir,
        )

        self.assertFalse(manifest.passed)
        self.assertEqual(("figure.png",), manifest.collision_files)
        self.assertEqual((), manifest.copied_files)
        self.assertFalse(assets_dir.exists())

    def test_missing_asset_blocks_note_publication(self) -> None:
        class _Pipeline:
            def run(self, paper):
                return PedagogicalResult(
                    note=RenderedNote("# Note\n\n![图 1](assets/missing.png)"),
                    evidence=EvidenceGateResult(passed=True),
                    blind=BlindReaderResult(overall="pass"),
                )

        paper = CanonicalPaperIR(
            "paper-3",
            "Missing image",
            (
                PaperIRBlock("intro", "paragraph", "Introduction", "Readable body.", 0),
                PaperIRBlock("figure", "figure", "Method", "Figure", 1,
                             image_path="images/missing.png", safe_image=True),
            ),
        )
        vault = Path("tmp/publication-manifest-tests/blocked-vault")
        with mock.patch.object(DeepSeekPaperReadingModel, "from_environment", return_value=object()):
            service = ReaderProductionService(
                ReaderConfig(normalized_root=Path("tmp/publication-manifest-tests/normalized"), mode="pedagogical"),
                vault,
                pedagogical=_Pipeline(),
            )
        service.reader.resolver.resolve = lambda candidate: paper  # type: ignore[method-assign]
        service.reader.read = lambda candidate: self.fail("发布素材失败后不得回退 generic")  # type: ignore[method-assign]

        result = service.process(PaperCandidate("paper-3", "Missing image", "https://example.test", "ai"))

        self.assertEqual("needs_review", result["status"])
        self.assertEqual("publication_manifest:missing_publication_asset", result["stop_reason"])
        self.assertIsNone(result["published_path"])

    def test_successful_copy_is_recorded_in_manifest(self) -> None:
        source = Path("tests/fixtures/one_pixel.ppm").resolve()
        assets_dir = Path("tmp/publication-manifest-tests/success/assets")
        paper = CanonicalPaperIR(
            "paper-4",
            "Available image",
            (
                PaperIRBlock("intro", "paragraph", "Introduction", "Readable body.", 0),
                PaperIRBlock("figure", "figure", "Method", "Figure", 1,
                             image_path=str(source), safe_image=True),
            ),
        )

        manifest = PublicationAssetPublisher(Path("unused")).publish(
            paper,
            f"# Note\n\n![图 1](assets/{source.name})",
            assets_dir,
        )

        self.assertTrue(manifest.passed)
        self.assertEqual((source.name,), manifest.referenced_files)
        self.assertEqual((source.name,), manifest.copied_files)
        self.assertEqual(source.read_bytes(), (assets_dir / source.name).read_bytes())


if __name__ == "__main__":
    unittest.main()
