from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import httpx

from research_pulse.production.mineru_api import (
    MinerUApiConfig,
    MinerUApiError,
    MinerUApiParser,
)
from research_pulse.production.normalized import load_complete_normalized


def _result_archive() -> bytes:
    content = [
        {"type": "text", "text_level": 1, "text": "Method", "page_idx": 0, "bbox": [10, 20, 900, 60]},
        {"type": "text", "text": "A complete method paragraph.", "page_idx": 0, "bbox": [10, 70, 900, 180]},
        {"type": "equation", "text": "d_m=(R_m,c_m,l_m,v_m)", "page_idx": 1, "bbox": [100, 200, 800, 300]},
        {
            "type": "table",
            "table_body": "<table><tr><td>Metric</td><td>0.836</td></tr></table>",
            "table_caption": ["Table 1: Results"],
            "img_path": "images/table-1.jpg",
            "page_idx": 1,
            "bbox": [50, 320, 950, 700],
        },
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("paper/full.md", "# Method\n\nA complete method paragraph.\n")
        archive.writestr("paper/paper_content_list.json", json.dumps(content))
        archive.writestr("paper/images/table-1.jpg", b"image")
    return buffer.getvalue()


class MinerUApiParserTests(unittest.TestCase):
    def setUp(self) -> None:
        workspace_tmp = Path.cwd() / ".mineru-api-test-tmp"
        workspace_tmp.mkdir(exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=workspace_tmp)
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_upload_poll_and_commit_preserves_page_bbox_and_assets(self) -> None:
        pdf = self.root / "paper.pdf"
        pdf.write_bytes(b"%PDF-test")
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if request.url.path.endswith("/v4/file-urls/batch"):
                self.assertEqual(request.headers["authorization"], "Bearer secret-token")
                return httpx.Response(200, json={
                    "code": 0,
                    "data": {"batch_id": "b1", "file_urls": ["https://upload.test/signed"]},
                })
            if request.url.host == "upload.test":
                self.assertEqual(request.method, "PUT")
                return httpx.Response(200)
            if request.url.path.endswith("/v4/extract-results/batch/b1"):
                return httpx.Response(200, json={
                    "code": 0,
                    "data": {"extract_result": [{
                        "file_name": "paper.pdf",
                        "state": "done",
                        "full_zip_url": "https://download.test/result.zip",
                    }]},
                })
            if request.url.host == "download.test":
                return httpx.Response(200, content=_result_archive())
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        output = self.root / "normalized"
        parser = MinerUApiParser(
            MinerUApiConfig(token="secret-token", poll_interval_seconds=0),
            transport=httpx.MockTransport(handler),
            sleeper=lambda _: None,
        )
        result = parser.parse_pdf(
            pdf,
            source_id="paper-1",
            source_url="https://example.test/paper-1",
            output_dir=output,
        )

        self.assertEqual(result, output)
        blocks = load_complete_normalized(
            output,
            expected_source_id="paper-1",
            image_roots=(output,),
        )
        self.assertEqual([block.kind for block in blocks], ["text", "text", "formula", "table"])
        self.assertEqual(blocks[1].section_path, ("Method",))
        self.assertEqual(blocks[1].bbox, (10.0, 70.0, 900.0, 180.0))
        self.assertEqual(blocks[2].latex, "d_m=(R_m,c_m,l_m,v_m)")
        self.assertEqual(blocks[3].table_html, "<table><tr><td>Metric</td><td>0.836</td></tr></table>")
        self.assertEqual(blocks[3].image_path, "images/table-1.jpg")
        self.assertTrue((output / "images" / "table-1.jpg").is_file())
        self.assertIn("A complete method paragraph", (output / "full.md").read_text(encoding="utf-8"))
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["parser"], "mineru_api")
        self.assertEqual(manifest["parser_version"], "precision-v4")
        self.assertNotIn("secret-token", repr(parser))
        self.assertTrue(any(call.method == "PUT" for call in calls))

    def test_failed_job_does_not_commit_partial_cache_or_expose_token(self) -> None:
        pdf = self.root / "paper.pdf"
        pdf.write_bytes(b"%PDF-test")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/v4/file-urls/batch"):
                return httpx.Response(200, json={
                    "code": 0,
                    "data": {"batch_id": "b1", "file_urls": ["https://upload.test/signed"]},
                })
            if request.url.host == "upload.test":
                return httpx.Response(200)
            return httpx.Response(200, json={
                "code": 0,
                "data": {"extract_result": [{
                    "file_name": "paper.pdf",
                    "state": "failed",
                    "err_msg": "unsupported PDF",
                }]},
            })

        output = self.root / "normalized"
        parser = MinerUApiParser(
            MinerUApiConfig(token="secret-token", poll_interval_seconds=0),
            transport=httpx.MockTransport(handler),
            sleeper=lambda _: None,
        )
        with self.assertRaisesRegex(MinerUApiError, "unsupported PDF") as raised:
            parser.parse_pdf(
                pdf,
                source_id="paper-1",
                source_url="https://example.test/paper-1",
                output_dir=output,
            )
        self.assertNotIn("secret-token", str(raised.exception))
        self.assertFalse((output / "manifest.json").exists())

    def test_missing_full_markdown_does_not_commit_partial_material(self) -> None:
        pdf = self.root / "paper.pdf"
        pdf.write_bytes(b"%PDF-test")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("paper/paper_content_list.json", "[]")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/v4/file-urls/batch"):
                return httpx.Response(200, json={"code": 0, "data": {"batch_id": "b1", "file_urls": ["https://upload.test/signed"]}})
            if request.url.host == "upload.test":
                return httpx.Response(200)
            if request.url.path.endswith("/v4/extract-results/batch/b1"):
                return httpx.Response(200, json={"code": 0, "data": {"extract_result": [{"file_name": "paper.pdf", "state": "done", "full_zip_url": "https://download.test/result.zip"}]}})
            return httpx.Response(200, content=buffer.getvalue())

        output = self.root / "normalized"
        parser = MinerUApiParser(MinerUApiConfig(token="secret", poll_interval_seconds=0), transport=httpx.MockTransport(handler), sleeper=lambda _: None)
        with self.assertRaisesRegex(MinerUApiError, "full.md"):
            parser.parse_pdf(pdf, source_id="paper-1", source_url="https://example.test/paper-1", output_dir=output)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
