from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from research_pulse.production.pipeline import PaperCandidate
from research_pulse.topics.traceable_candidate_reader import TraceableCandidateReader


class _Parser:
    def __init__(self) -> None:
        self.calls = 0

    def parse_pdf(self, pdf_path, *, source_id, source_url, output_dir):
        self.calls += 1
        output_dir.mkdir(parents=True)
        (output_dir / "full.md").write_text("# Paper\n\nBody", encoding="utf-8")
        (output_dir / "content_list.json").write_text("[]", encoding="utf-8")
        return output_dir


class _Pipeline:
    def __init__(self) -> None:
        self.calls = []

    def run(self, document, *, domain):
        self.calls.append((document, domain))
        return type("Receipt", (), {"to_payload": lambda self: {
            "source_id": document.source_id,
            "publication_status": "published",
            "published_path": "note.md",
        }})()


class TraceableCandidateReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.candidate = PaperCandidate(
            source_id="2608.01234v1",
            title="Paper",
            source_url="https://arxiv.org/abs/2608.01234v1",
            domain="agents",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_downloads_parses_and_returns_batch_receipt(self) -> None:
        parser, pipeline = _Parser(), _Pipeline()
        downloads = []

        def download(source_id, destination, timeout):
            downloads.append((source_id, timeout))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"%PDF")

        reader = TraceableCandidateReader(
            cache_root=self.root / "cache",
            parser=parser,
            pipeline=pipeline,
            material_loader=lambda root, **meta: type("Doc", (), {"source_id": meta["source_id"]})(),
            downloader=download,
        )

        result = reader.process(self.candidate)

        self.assertEqual(result["status"], "published")
        self.assertEqual(result["receipt_status"], "published")
        self.assertEqual(parser.calls, 1)
        self.assertEqual(downloads[0][0], self.candidate.source_id)
        self.assertEqual(pipeline.calls[0][1], "agents")

    def test_reuses_complete_material_without_download_or_parse(self) -> None:
        material = self.root / "cache" / self.candidate.source_id / "material"
        material.mkdir(parents=True)
        (material / "full.md").write_text("# cached", encoding="utf-8")
        (material / "content_list.json").write_text("[]", encoding="utf-8")
        parser, pipeline = _Parser(), _Pipeline()
        reader = TraceableCandidateReader(
            cache_root=self.root / "cache",
            parser=parser,
            pipeline=pipeline,
            material_loader=lambda root, **meta: type("Doc", (), {"source_id": meta["source_id"]})(),
            downloader=lambda *args: self.fail("cached material must not download"),
        )

        result = reader.process(self.candidate)

        self.assertEqual(result["status"], "published")
        self.assertEqual(parser.calls, 0)


if __name__ == "__main__":
    unittest.main()
