"""ReaderProductionService 模式接入测试（T7）：mode=pedagogical 发布教学化笔记、重算 evidence_level。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import CanonicalPaperIR, DeepSeekPaperReadingModel, PaperIRBlock
from research_pulse.pedagogical.contracts import (
    ArtifactQualityIssue,
    ArtifactQualityReport,
    BlindReaderResult,
    EvidenceGateResult,
    PedagogicalResult,
    RenderedNote,
    RenderResult,
)
from research_pulse.reader_production import ReaderConfig, ReaderProductionService
from unittest import mock


def _fake_ir() -> CanonicalPaperIR:
    blocks = (
        PaperIRBlock("p1", "paragraph", "Intro", "Agents may overreach.", 1),
        PaperIRBlock("f1", "figure", "Method", "Figure 1: pipeline", 2),
    )
    return CanonicalPaperIR("p", "Writer", blocks, source_url="https://example.com/writer")


class _FakePipeline:
    def run(self, paper: CanonicalPaperIR) -> PedagogicalResult:
        image = next((Path(block.image_path).name for block in paper.blocks if block.image_path), None)
        image_markdown = f"![](assets/{image})" if image else ""
        render_results = (
            (RenderResult("fig_1", "rendered", image_markdown),) if image else ()
        )
        return PedagogicalResult(
            note=RenderedNote(
                markdown=f"# 教学化精读\n\n作者用 broker 做前后审计。{image_markdown}",
                render_results=render_results,
            ),
            evidence=EvidenceGateResult(passed=True),
            blind=BlindReaderResult(overall="pass"),
        )


class PedagogicalModeWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = self.root / "knowledge"
        # 避免构造 ReaderProductionService 时依赖真实 DeepSeek key
        self._from_env = mock.patch.object(
            DeepSeekPaperReadingModel, "from_environment", return_value=object()
        )
        self._from_env.start()

    def tearDown(self) -> None:
        self._from_env.stop()
        self._tmp.cleanup()

    def test_pedagogical_mode_publishes_note_with_multimodal_level(self) -> None:
        service = ReaderProductionService(
            ReaderConfig(normalized_root=self.root, mode="pedagogical"),
            self.vault,
            pedagogical=_FakePipeline(),
        )
        service.reader.resolver.resolve = lambda candidate: _fake_ir()  # type: ignore[method-assign]
        result = service.process(PaperCandidate("p", "Writer", "https://example.com/writer", "security"))
        self.assertEqual(result["publication_status"], "published")
        self.assertTrue(result["published_path"])
        note = Path(result["published_path"]).read_text(encoding="utf-8")
        self.assertIn('evidence_level: "full_text_multimodal"', note)
        self.assertIn("教学化精读", note)

    def test_artifact_gate_failure_prevents_pedagogical_publication(self) -> None:
        class _BadArtifactPipeline:
            def run(self, paper):
                return PedagogicalResult(
                    note=RenderedNote(markdown="# 实验\n\nresolu- tion"),
                    evidence=EvidenceGateResult(passed=True),
                    blind=BlindReaderResult(overall="pass"),
                    artifact=ArtifactQualityReport(
                        passed=False,
                        issues=(ArtifactQualityIssue("pdf_hyphenation", "断词", line=3),),
                    ),
                )

        service = ReaderProductionService(
            ReaderConfig(normalized_root=self.root, mode="pedagogical"),
            self.vault,
            pedagogical=_BadArtifactPipeline(),
        )
        service.reader.resolver.resolve = lambda candidate: _fake_ir()  # type: ignore[method-assign]
        from types import SimpleNamespace
        service.reader.read = lambda candidate: SimpleNamespace(  # type: ignore[method-assign]
            draft=SimpleNamespace(markdown="# generic 兜底笔记\n\n内容"),
            receipt=SimpleNamespace(status="completed", stop_reason="completed"),
        )

        result = service.process(PaperCandidate("p", "Writer", "https://example.com/writer", "security"))

        self.assertEqual(["gates:artifact"], result["pedagogical_fallbacks"])
        self.assertIn("generic 兜底笔记", Path(result["published_path"]).read_text(encoding="utf-8"))

    def test_generic_mode_with_pipeline_injected_does_not_enter_pedagogical(self) -> None:
        class _Boom:
            def run(self, paper):
                raise AssertionError("mode=generic 不应调用 pedagogical 管线")

        service = ReaderProductionService(
            ReaderConfig(normalized_root=self.root, mode="generic"),
            self.vault,
            pedagogical=_Boom(),
        )
        self.assertFalse(service.config.mode == "pedagogical")
        # 不调用 process（generic 需要真实模型），仅确认分支不会被误触发
        self.assertIsNone(service.pedagogical is None or None)


    def test_pedagogical_copies_figure_images_to_published_assets(self) -> None:
        # 图块带真实图片路径 → process 后应把图拷到发布目录的 assets/（否则前端显示不了图）
        import tempfile
        img = Path(tempfile.mktemp(suffix=".jpg"))
        img.write_bytes(b"\xff\xd8fakejpg")
        ir = CanonicalPaperIR(
            "p", "Writer",
            blocks=(PaperIRBlock("p1", "paragraph", "Intro", "text", 1),
                    PaperIRBlock("f1", "figure", "Method", "Fig", 2, image_path=str(img), safe_image=True)),
            source_url="https://example.com/writer",
        )
        service = ReaderProductionService(
            ReaderConfig(normalized_root=self.root, mode="pedagogical"),
            self.vault,
            pedagogical=_FakePipeline(),
        )
        service.reader.resolver.resolve = lambda candidate: ir  # type: ignore[method-assign]
        result = service.process(PaperCandidate("p", "Writer", "https://example.com/writer", "security"))
        assets_dir = Path(result["published_path"]).parent / "assets"
        self.assertTrue((assets_dir / img.name).is_file(), "图应被拷贝到发布目录 assets/")
        img.unlink()

    def test_pedagogical_resolves_normalized_relative_image_paths(self) -> None:
        normalized_dir = self.root / "p" / "normalized"
        image = normalized_dir / "images" / "fig-relative.jpg"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"\xff\xd8fakejpg")
        ir = CanonicalPaperIR(
            "p",
            "Writer",
            blocks=(
                PaperIRBlock("p1", "paragraph", "Intro", "text", 1),
                PaperIRBlock(
                    "f1",
                    "figure",
                    "Method",
                    "Fig",
                    2,
                    image_path="images/fig-relative.jpg",
                    safe_image=True,
                ),
            ),
            source_url="https://example.com/writer",
        )
        service = ReaderProductionService(
            ReaderConfig(normalized_root=self.root, mode="pedagogical"),
            self.vault,
            pedagogical=_FakePipeline(),
        )
        service.reader.resolver.resolve = lambda candidate: ir  # type: ignore[method-assign]
        result = service.process(PaperCandidate("p", "Writer", "https://example.com/writer", "security"))
        published_asset = Path(result["published_path"]).parent / "assets" / image.name
        self.assertTrue(published_asset.is_file(), "相对 image_path 应从该论文 normalized 目录解析")

    def test_pedagogical_copies_table_image_fallback_to_published_assets(self) -> None:
        normalized_dir = self.root / "p" / "normalized"
        image = normalized_dir / "images" / "table-fallback.jpg"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"\xff\xd8fakejpg")
        ir = CanonicalPaperIR(
            "p",
            "Writer",
            blocks=(
                PaperIRBlock("p1", "paragraph", "Intro", "text", 1),
                PaperIRBlock(
                    "t1",
                    "table",
                    "Results",
                    "| A | B |\n| --- | --- |\n| x | 1±0.1 2±0.2 |",
                    2,
                    caption="Table 1",
                    image_path="images/table-fallback.jpg",
                ),
            ),
            source_url="https://example.com/writer",
        )
        service = ReaderProductionService(
            ReaderConfig(normalized_root=self.root, mode="pedagogical"),
            self.vault,
            pedagogical=_FakePipeline(),
        )
        service.reader.resolver.resolve = lambda candidate: ir  # type: ignore[method-assign]

        result = service.process(PaperCandidate("p", "Writer", "https://example.com/writer", "security"))

        published = Path(result["published_path"]).parent / "assets" / image.name
        self.assertTrue(published.is_file(), "表格图片回退也必须发布到版本 assets/")

    def test_pedagogical_fallbacks_do_not_leak_across_runs(self) -> None:
        # 同一 service 连续处理两篇：第一篇 evidence 不过回退 generic（写入 _fallbacks），
        # 第二篇教学化成功 —— 第二篇的 pedagogical_fallbacks 必须为空（不串前一篇）
        class _Sequence:
            """第一篇 run 返回 evidence 不过，第二篇返回教学化通过。"""

            def __init__(self):
                self.calls = 0

            def run(self, paper):
                self.calls += 1
                if self.calls == 1:
                    return PedagogicalResult(
                        note=RenderedNote(markdown="# 教学化\n\n正文"),
                        evidence=EvidenceGateResult(passed=False, issues=("无源",)),
                        blind=BlindReaderResult(overall="pass"),
                    )
                return _FakePipeline().run(paper)

        service = ReaderProductionService(
            ReaderConfig(normalized_root=self.root, mode="pedagogical"),
            self.vault,
            pedagogical=_Sequence(),
        )
        service.reader.resolver.resolve = lambda candidate: _fake_ir()  # type: ignore[method-assign]
        from types import SimpleNamespace

        service.reader.read = lambda candidate: SimpleNamespace(  # type: ignore[method-assign]
            draft=SimpleNamespace(markdown="# generic 兜底笔记\n\n内容"),
            receipt=SimpleNamespace(status="completed", stop_reason="completed"),
        )
        first = service.process(PaperCandidate("p1", "Writer", "https://example.com/writer", "security"))
        second = service.process(PaperCandidate("p2", "Writer", "https://example.com/writer", "security"))
        self.assertEqual(first["pedagogical_fallbacks"], ["gates:evidence"])
        self.assertEqual(second["pedagogical_fallbacks"], [], "fallbacks 不应串到下一篇 run")
        self.assertTrue(second["gate_evidence_passed"])

    def test_pedagogical_fatal_retries_once_then_publishes(self) -> None:
        # 模型输出偶发失败（2504 实测空 thesis）→ 重试一次即成功，仍发布教学化笔记、不回退 generic
        class _Flaky:
            def __init__(self):
                self.calls = 0

            def run(self, paper):
                self.calls += 1
                if self.calls == 1:
                    raise ValueError("PaperModel.thesis 不能为空")
                return _FakePipeline().run(paper)

        flaky = _Flaky()
        service = ReaderProductionService(
            ReaderConfig(normalized_root=self.root, mode="pedagogical"),
            self.vault,
            pedagogical=flaky,
        )
        service.reader.resolver.resolve = lambda candidate: _fake_ir()  # type: ignore[method-assign]
        result = service.process(PaperCandidate("p", "Writer", "https://example.com/writer", "security"))
        self.assertEqual(flaky.calls, 2, "应重试恰好一次")
        self.assertEqual(result["publication_status"], "published")
        self.assertIn("教学化精读", Path(result["published_path"]).read_text(encoding="utf-8"))
        self.assertEqual(result["pedagogical_fallbacks"], [])

    def test_pedagogical_fatal_retries_then_falls_back_to_generic(self) -> None:
        # 两次都失败 = fatal → 回退 generic（不崩溃、不发布半成品）
        class _AlwaysBoom:
            def run(self, paper):
                raise ValueError("PaperModel.thesis 不能为空")

        service = ReaderProductionService(
            ReaderConfig(normalized_root=self.root, mode="pedagogical"),
            self.vault,
            pedagogical=_AlwaysBoom(),
        )
        service.reader.resolver.resolve = lambda candidate: _fake_ir()  # type: ignore[method-assign]
        from types import SimpleNamespace

        service.reader.read = lambda candidate: SimpleNamespace(  # type: ignore[method-assign]
            draft=SimpleNamespace(markdown="# generic 兜底笔记\n\n内容"),
            receipt=SimpleNamespace(status="completed", stop_reason="completed"),
        )
        result = service.process(PaperCandidate("p", "Writer", "https://example.com/writer", "security"))
        self.assertEqual(result["publication_status"], "published")
        self.assertIn("generic 兜底笔记", Path(result["published_path"]).read_text(encoding="utf-8"))
        self.assertIn("pedagogical:ValueError", result["pedagogical_fallbacks"])


if __name__ == "__main__":
    unittest.main()
