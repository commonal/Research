"""ServerPaperParser + runner integration tests (transport mocked; no ssh)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
import json
from datetime import UTC, datetime
from pathlib import Path

from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.normalized import load_complete_normalized
from research_pulse.topics.daily_runner import ReadingBatchRunner
from research_pulse.topics.remote_parse import RemoteParseError, ServerParseConfig, ServerPaperParser
from research_pulse.reader_production import ReaderMaterialResolver


class _FakeRunner:
    """Simulates ssh/scp transport without assuming platform-specific scp layout."""

    def __init__(self, *, fail_push=False, fail_parse=False, fail_pull=False):
        self.calls: list[list[str]] = []
        self._fail_push = fail_push
        self._fail_parse = fail_parse
        self._fail_pull = fail_pull

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(args)
        joined = " ".join(args)
        if args[0] == "scp" and ":/exp/" in args[-2] and args[-2].endswith("/normalized/blocks.jsonl"):
            if self._fail_pull:
                rc = 99
            else:
                rc = 0
                destination = Path(args[-1])
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(
                    json.dumps({
                        "block_id": "normalized:2608.18351v1:figure:1",
                        "kind": "figure",
                        "text": "Figure 1: architecture",
                        "image_path": "images/fig-1.jpg",
                        "sources": [{"parser": "mineru", "locator": "mineru:content#/1"}],
                        "alignment": "mineru_only",
                        "parse_status": "available",
                        "confidence": 0.55,
                    }) + "\n",
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess(args, rc)
        if args[0] == "scp" and ":/exp/" in args[-2] and args[-2].endswith("/normalized/manifest.json"):
            destination = Path(args[-1])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps({
                "schema_version": 1,
                "source_id": "2608.18351v1",
                "block_count": 1,
                "input_hashes": {"mineru_content_list": "m", "docling_document": "d"},
                "complete": True,
            }) + "\n", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0)
        if args[0] == "scp" and "-r" in args and args[-2].endswith("/mineru/source/auto/images"):
            destination_parent = Path(args[-1])
            images = destination_parent / "images"
            images.mkdir(parents=True, exist_ok=True)
            (images / "fig-1.jpg").write_bytes(b"fake-image")
            return subprocess.CompletedProcess(args, 0)
        if "parse_paper.sh" in joined:
            rc = 99 if self._fail_parse else 0
        elif "mkdir" in joined and args[0] == "ssh":
            rc = 0
        elif args[0] == "scp":
            rc = 99 if self._fail_push else 0
        else:
            rc = 0
        return subprocess.CompletedProcess(args, rc)


def _downloader(source_id: str, dest: Path, timeout: int) -> None:
    dest.write_bytes(b"%PDF fake")


def _candidate(source_id: str = "2608.18351v1") -> PaperCandidate:
    return PaperCandidate(source_id=source_id, title="T", source_url=f"https://arxiv.org/abs/{source_id}", domain="d")


class ServerPaperParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.norm_root = self.root / "normalized"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _parser(self, runner) -> ServerPaperParser:
        return ServerPaperParser(
            self.norm_root,
            ServerParseConfig(remote_exp_root="/exp", remote_pipeline="/pipeline"),
            runner=runner,
            downloader=_downloader,
            html_first=False,  # 本组测试专测服务器双路;HTML 优先在 HtmlFirstParsingTests 覆盖
        )

    def test_local_already_normalized_is_idempotent(self) -> None:
        local = self.norm_root / "2608.18351v1" / "normalized"
        local.mkdir(parents=True)
        (local / "blocks.jsonl").write_text(json.dumps({
            "block_id": "normalized:2608.18351v1:text:1",
            "kind": "text",
            "text": "Readable body text.",
            "sources": [{"parser": "mineru", "locator": "mineru:content#/1"}],
            "alignment": "mineru_only",
            "parse_status": "available",
            "confidence": 0.55,
        }) + "\n", encoding="utf-8")
        (local / "manifest.json").write_text(json.dumps({
            "schema_version": 1,
            "source_id": "2608.18351v1",
            "block_count": 1,
            "input_hashes": {"mineru_content_list": "m", "docling_document": "d"},
            "complete": True,
        }) + "\n", encoding="utf-8")
        runner = _FakeRunner()
        result = self._parser(runner).ensure_normalized(_candidate())
        self.assertEqual(result, local)
        self.assertEqual(runner.calls, [])

    def test_incomplete_local_cache_is_refreshed_instead_of_reused(self) -> None:
        local = self.norm_root / "2608.18351v1" / "normalized"
        local.mkdir(parents=True)
        (local / "blocks.jsonl").write_text("partial\n", encoding="utf-8")

        runner = _FakeRunner()
        result = self._parser(runner).ensure_normalized(_candidate())

        self.assertEqual(result, local)
        self.assertTrue(runner.calls, "缺少 manifest 的缓存不能被当作完整结果复用")
        self.assertTrue((local / "manifest.json").is_file())

    def test_full_loop_downloads_pushes_triggers_pulls(self) -> None:
        runner = _FakeRunner()
        local = self._parser(runner).ensure_normalized(_candidate())
        self.assertIsNotNone(local)
        joined = [" ".join(c) for c in runner.calls]
        self.assertEqual(len(runner.calls), 6)  # mkdir -> push -> parse -> two files -> images
        self.assertTrue(any(c.startswith("ssh ") and "mkdir" in c for c in joined), "remote mkdir")
        self.assertTrue(any(c.startswith("scp ") and "source.pdf" in c for c in joined), "push pdf")
        self.assertTrue(any("parse_paper.sh" in c for c in joined), "trigger parse")
        self.assertTrue(any(c.endswith("normalized/blocks.jsonl " + str(local / "blocks.jsonl")) for c in joined))
        self.assertTrue((local / "images" / "fig-1.jpg").is_file())
        self.assertFalse(
            any(c.startswith("ssh ") and str(local).replace("\\", "/") in c for c in joined),
            "本地目录不得通过远端 ssh mkdir 创建",
        )

        paper = ReaderMaterialResolver(self.norm_root).resolve(_candidate())
        figure = next(block for block in paper.blocks if block.kind == "figure")
        self.assertEqual(Path(figure.image_path), (local / "images" / "fig-1.jpg").resolve())

    def test_reader_rejects_manifest_block_count_mismatch(self) -> None:
        local = self.norm_root / "2608.18351v1" / "normalized"
        local.mkdir(parents=True)
        (local / "blocks.jsonl").write_text(json.dumps({
            "block_id": "normalized:2608.18351v1:text:1",
            "kind": "text",
            "text": "Only one block.",
            "sources": [{"parser": "mineru", "locator": "mineru:content#/1"}],
            "alignment": "mineru_only",
            "parse_status": "available",
            "confidence": 0.55,
        }) + "\n", encoding="utf-8")
        (local / "manifest.json").write_text(json.dumps({
            "schema_version": 1,
            "source_id": "2608.18351v1",
            "block_count": 2,
            "input_hashes": {"mineru_content_list": "m", "docling_document": "d"},
            "complete": True,
        }), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "block_count"):
            ReaderMaterialResolver(self.norm_root).resolve(_candidate())

    def test_parse_failure_raises(self) -> None:
        parser = self._parser(_FakeRunner(fail_parse=True))
        with self.assertRaises(RemoteParseError):
            parser.ensure_normalized(_candidate())

    def test_pull_failure_raises_when_blocks_missing(self) -> None:
        parser = self._parser(_FakeRunner(fail_pull=True))
        with self.assertRaises(RemoteParseError):
            parser.ensure_normalized(_candidate())


class RunnerWithServerParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_unpublished_candidate_is_parsed_then_read(self) -> None:
        class DummyParser:
            def __init__(self, root):
                self.root = root
                self.called = []

            def ensure_normalized(self, candidate):
                self.called.append(candidate.source_id)

        class FakeFinder:
            def discover(self, *, topic, domain, limit, window_start=None, window_end=None):
                return [_candidate("p1"), _candidate("p2")]

        seen = []
        reader = type("R", (), {"process": lambda self, c: seen.append(c.source_id) or {"source_id": c.source_id, "status": "published"}})()

        parser = DummyParser(self.root)
        runner = ReadingBatchRunner(
            candidate_finder=FakeFinder(),
            reader_service=reader,
            vault_root=self.root / "knowledge",
            server_parser=parser,
        )
        result = runner.run(run_id="r", topic="t", domain="d", limit=3, window_start=None, window_end=datetime.now(UTC))
        self.assertEqual(parser.called, ["p1", "p2"])  # both unpublished -> parsed
        self.assertEqual(seen, ["p1", "p2"])            # both read

    def test_published_candidate_skips_parser_and_reader(self) -> None:
        pub = self.root / "knowledge" / "papers" / "p1"
        pub.mkdir(parents=True)
        (pub / "n.md").write_text("---\npublication_status: \"published\"\n---\n", encoding="utf-8")

        class FakeFinder:
            def discover(self, **kw):
                return [_candidate("p1"), _candidate("p2")]

        class DummyParser:
            def __init__(self):
                self.called = []

            def ensure_normalized(self, candidate):
                self.called.append(candidate.source_id)

        parser = DummyParser()
        seen = []
        reader = type("R", (), {"process": lambda self, c: seen.append(c.source_id) or {"source_id": c.source_id, "status": "published"}})()
        runner = ReadingBatchRunner(candidate_finder=FakeFinder(), reader_service=reader, vault_root=self.root / "knowledge", server_parser=parser)
        result = runner.run(run_id="r", topic="t", domain="d", limit=3, window_start=None, window_end=datetime.now(UTC))
        statuses = [r["status"] for r in result["receipts"]]
        self.assertEqual(statuses, ["skipped_duplicate", "published"])
        self.assertEqual(seen, ["p2"])
        # p1 is published so its parser/reader never run
        self.assertEqual(parser.called, ["p2"])

    def test_parser_failure_yields_failed_receipt_not_batch_abort(self) -> None:
        class BoomParser:
            def ensure_normalized(self, candidate):
                raise RemoteParseError("nope")

        class FakeFinder:
            def discover(self, **kw):
                return [_candidate("p1")]

        reader = type("R", (), {"process": lambda self, c: (_ for _ in ()).throw(AssertionError("should not read"))})()
        runner = ReadingBatchRunner(candidate_finder=FakeFinder(), reader_service=reader, vault_root=self.root / "knowledge", server_parser=BoomParser())
        result = runner.run(run_id="r", topic="t", domain="d", limit=3, window_start=None, window_end=datetime.now(UTC))
        self.assertEqual(result["receipts"][0]["status"], "failed")


def _kind(joined: str) -> str:
    words = joined.split()
    if "parse_paper.sh" in joined:
        return "parse"
    if words and words[0] == "scp" and "-r" in words:
        return "pull"
    if words and words[0] == "scp":
        return "push"
    if words and words[0] == "ssh" and "mkdir" in joined:
        return "mkdir"
    return "other"


def _html_page_fetcher(source_id: str, dest: Path, timeout: int) -> None:
    """返回含真实结构锚点(LaTeXML figure/table/math)的最小 HTML 页。"""
    introduction = (
        "This paper studies reliable long-context memory for autonomous agents. "
        "It explains the task setting, the failure mode, and the assumptions used by the method. "
    ) * 4
    method = (
        "The method retrieves task-relevant memories before generation and applies a bounded relevance filter. "
        "The retrieved evidence remains attached to the generation request for later inspection. "
    ) * 4
    results = (
        "Experiments compare the proposed method with representative baselines under the same evaluation protocol. "
        "The analysis reports the observed result together with limitations and unresolved failure cases. "
    ) * 4
    dest.write_text(
        "<!DOCTYPE html><html><head><title>T</title></head><body>"
        '<h1 class="ltx_title">Paper Title</h1>'
        '<h2 class="ltx_title">1 Introduction</h2>'
        f'<p id="S1.p1.1" class="ltx_p">{introduction} First paragraph with inline '
        '<math id="S1.p1.1.m1" class="ltx_Math" alttext="\\\\alpha" display="inline">'
        "<semantics><mi>\u03b1</mi></semantics></math> symbol.</p>"
        '<h2 class="ltx_title">2 Method</h2>'
        f'<p id="S2.p1.1" class="ltx_p">{method}</p>'
        '<table id="S2.E1" class="ltx_equation ltx_eqn_table"><tbody>'
        '<tr class="ltx_eqn_row"><td class="ltx_eqn_cell">'
        '<math id="S2.E1.1.m1" class="ltx_Math" alttext="\\\\displaystyle y=mx+b" display="inline">'
        "<semantics><mi>y</mi></semantics></math></td></tr></tbody></table>"
        '<figure id="S3.F1" class="ltx_figure">'
        '<img src="2608.18351v1/arch.png" id="S3.F1.g1" class="ltx_graphics" alt="arch">'
        '<figcaption class="ltx_caption"><span class="ltx_tag">Figure 1: </span>The architecture.</figcaption>'
        "</figure>"
        '<figure id="S4.T1" class="ltx_table">'
        '<table id="S4.T1.8" class="ltx_tabular"><tbody>'
        '<tr id="S4.T1.8.1" class="ltx_tr"><td id="S4.T1.8.1.1" class="ltx_td">Model</td><td class="ltx_td">Acc</td></tr>'
        '<tr class="ltx_tr"><td class="ltx_td">Mamba</td><td class="ltx_td">60.1</td></tr>'
        "</tbody></table>"
        '<figcaption class="ltx_caption"><span class="ltx_tag">Table 1: </span>Main results.</figcaption>'
        "</figure>"
        '<h2 class="ltx_title">3 Results and limitations</h2>'
        f'<p id="S3.p1.1" class="ltx_p">{results}</p>'
        '<section class="ltx_bibliography"><p class="ltx_p">[1] Someone. A paper. 2020.</p></section>'
        "</body></html>",
        encoding="utf-8",
    )


def _html_image_fetcher(url: str, target: Path, timeout: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fake-image-bytes")


class HtmlFirstParsingTests(unittest.TestCase):
    """HTML 优先主路:成功零远程调用;失败自动回退服务器双路。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.norm_root = self.root / "normalized"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _parser(self, runner) -> ServerPaperParser:
        return ServerPaperParser(
            self.norm_root,
            ServerParseConfig(remote_exp_root="/exp", remote_pipeline="/pipeline"),
            runner=runner,
            downloader=_downloader,
            html_page_fetcher=_html_page_fetcher,
            html_image_fetcher=_html_image_fetcher,
        )

    def _write_legacy_dual_cache(self) -> Path:
        local = self.norm_root / "2608.18351v1" / "normalized"
        local.mkdir(parents=True)
        (local / "blocks.jsonl").write_text(json.dumps({
            "block_id": "normalized:2608.18351v1:text:legacy",
            "kind": "text",
            "text": "Legacy PDF parser material.",
            "sources": [{"parser": "mineru", "locator": "mineru:content#/1"}],
            "alignment": "mineru_only",
            "parse_status": "available",
            "confidence": 0.55,
        }) + "\n", encoding="utf-8")
        (local / "manifest.json").write_text(json.dumps({
            "schema_version": 1,
            "source_id": "2608.18351v1",
            "block_count": 1,
            "input_hashes": {"mineru_content_list": "m", "docling_document": "d"},
            "complete": True,
        }), encoding="utf-8")
        return local

    def test_html_success_produces_local_cache_without_remote_calls(self) -> None:
        runner = _FakeRunner()
        local = self._parser(runner).ensure_normalized(_candidate())
        self.assertIsNotNone(local)
        blocks = load_complete_normalized(local, expected_source_id="2608.18351v1", image_roots=(local,))
        kinds = {block.kind for block in blocks}
        self.assertIn("formula", kinds)
        self.assertIn("figure", kinds)
        self.assertIn("table", kinds)
        self.assertEqual(runner.calls, [], "fallback loop must stay cold: no ssh/scp call expected")
        manifest = json.loads((local / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["parser"], "arxiv_html")
        self.assertEqual(manifest["parser_version"], "arxiv-html-v3")
        self.assertEqual(manifest["available_assets"]["figures_fetched"], 1)
        self.assertTrue((local / "page.html").is_file())
        self.assertTrue(all(block.alignment == "html_native" for block in blocks))
        paper = ReaderMaterialResolver(self.norm_root).resolve(_candidate())
        paragraph = next(block for block in paper.blocks if block.kind == "paragraph")
        self.assertEqual(paragraph.source_locators[0].source_kind, "arxiv_html")
        self.assertIn("page.html#S1.p1.1", paragraph.source_locators[0].locator)

    def test_current_html_cache_is_reused_without_refetch(self) -> None:
        fetches: list[str] = []

        def counting_fetcher(source_id: str, dest: Path, timeout: int) -> None:
            fetches.append(source_id)
            _html_page_fetcher(source_id, dest, timeout)

        runner = _FakeRunner()
        parser = ServerPaperParser(
            self.norm_root,
            ServerParseConfig(remote_exp_root="/exp", remote_pipeline="/pipeline"),
            runner=runner,
            downloader=_downloader,
            html_page_fetcher=counting_fetcher,
            html_image_fetcher=_html_image_fetcher,
        )

        first = parser.ensure_normalized(_candidate())
        second = parser.ensure_normalized(_candidate())

        self.assertEqual(first, second)
        self.assertEqual(fetches, ["2608.18351v1"])
        self.assertEqual(runner.calls, [])

    def test_incomplete_html_material_falls_back_to_server_parsers(self) -> None:
        def incomplete_fetcher(source_id: str, dest: Path, timeout: int) -> None:
            dest.write_text(
                '<html><body><p id="S1.p1" class="ltx_p">Only one paragraph.</p></body></html>',
                encoding="utf-8",
            )

        runner = _FakeRunner()
        parser = ServerPaperParser(
            self.norm_root,
            ServerParseConfig(remote_exp_root="/exp", remote_pipeline="/pipeline"),
            runner=runner,
            downloader=_downloader,
            html_page_fetcher=incomplete_fetcher,
            html_image_fetcher=_html_image_fetcher,
        )

        local = parser.ensure_normalized(_candidate())

        self.assertTrue(any("parse_paper.sh" in " ".join(call) for call in runner.calls))
        manifest = json.loads((local / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotEqual(manifest.get("parser"), "arxiv_html")

    def test_legacy_dual_cache_is_upgraded_to_current_html(self) -> None:
        local = self._write_legacy_dual_cache()
        runner = _FakeRunner()

        result = self._parser(runner).ensure_normalized(_candidate())

        self.assertEqual(result, local)
        self.assertEqual(runner.calls, [])
        manifest = json.loads((local / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["parser"], "arxiv_html")
        self.assertEqual(manifest["parser_version"], "arxiv-html-v3")

    def test_failed_html_upgrade_keeps_valid_dual_cache(self) -> None:
        local = self._write_legacy_dual_cache()
        runner = _FakeRunner()

        def broken_fetcher(source_id: str, dest: Path, timeout: int) -> None:
            raise RemoteParseError("simulated html outage")

        parser = ServerPaperParser(
            self.norm_root,
            ServerParseConfig(remote_exp_root="/exp", remote_pipeline="/pipeline"),
            runner=runner,
            downloader=_downloader,
            html_page_fetcher=broken_fetcher,
            html_image_fetcher=_html_image_fetcher,
        )

        result = parser.ensure_normalized(_candidate())

        self.assertEqual(result, local)
        self.assertEqual(runner.calls, [])
        manifest = json.loads((local / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotEqual(manifest.get("parser"), "arxiv_html")

    def test_html_failure_falls_back_to_server_loop(self) -> None:
        def broken_fetcher(source_id: str, dest: Path, timeout: int) -> None:
            raise RemoteParseError("simulated html outage")

        runner = _FakeRunner()
        parser = ServerPaperParser(
            self.norm_root,
            ServerParseConfig(remote_exp_root="/exp", remote_pipeline="/pipeline"),
            runner=runner,
            downloader=_downloader,
            html_page_fetcher=broken_fetcher,
            html_image_fetcher=_html_image_fetcher,
        )
        local = parser.ensure_normalized(_candidate())
        self.assertIsNotNone(local)
        kinds = [call for call in runner.calls if "parse_paper.sh" in " ".join(call)]
        self.assertEqual(len(kinds), 1, "server parse must run exactly once after html failure")
        load_complete_normalized(local, expected_source_id="2608.18351v1", image_roots=(local,))

    def test_html_first_disabled_keeps_server_only(self) -> None:
        runner = _FakeRunner()
        parser = ServerPaperParser(
            self.norm_root,
            ServerParseConfig(remote_exp_root="/exp", remote_pipeline="/pipeline"),
            runner=runner,
            downloader=_downloader,
            html_first=False,
            html_page_fetcher=_html_page_fetcher,
            html_image_fetcher=_html_image_fetcher,
        )
        parser.ensure_normalized(_candidate())
        self.assertTrue(any("parse_paper.sh" in " ".join(call) for call in runner.calls))


if __name__ == "__main__":
    unittest.main()
