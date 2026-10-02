from __future__ import annotations

from contextlib import redirect_stdout
from pathlib import Path
from unittest import TestCase, mock
import io
import json
import sys
import tempfile

from research_pulse.traceable_reading import cli
from tests.test_traceable_reading_pipeline import ScriptedModel


class ProviderBridge:
    text_model = "scripted"

    def __init__(self) -> None:
        self.script = ScriptedModel()

    def call_json(self, operation, model, prompt, image=None):
        return self.script.call_json(operation, prompt)


class TraceableReadingCliTests(TestCase):
    def test_existing_material_cli_uses_independent_entry_and_publishes(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            root = Path(temp)
            material = root / "material"
            material.mkdir()
            (material / "full.md").write_text("# Paper\n\nMethod and results.", encoding="utf-8")
            content = [
                {"type": "text", "text_level": 1, "text": "Paper", "page_idx": 0, "bbox": [0, 0, 1000, 100]},
                {"type": "text", "text": "Method and results.", "page_idx": 0, "bbox": [0, 110, 1000, 300]},
            ]
            (material / "content_list.json").write_text(json.dumps(content), encoding="utf-8")
            vault = root / "knowledge"
            argv = ["traceable", "--material-root", str(material), "--source-id", "paper-1", "--source-url", "https://example.test/paper-1", "--domain", "agents", "--vault-root", str(vault)]
            output = io.StringIO()
            with mock.patch.object(sys, "argv", argv), mock.patch("research_pulse.traceable_reading.service.DeepSeekPaperReadingModel.from_environment", return_value=ProviderBridge()), redirect_stdout(output):
                status = cli.main()
            payload = json.loads(output.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["publication_status"], "published")
            self.assertFalse(payload["rag_eligible"])
            self.assertTrue(Path(payload["published_path"]).is_file())

    def test_cli_failure_receipt_redacts_tokens(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            root = Path(temp)
            vault = root / "knowledge"
            argv = ["traceable", "--material-root", str(root / "missing"), "--source-id", "paper-1", "--source-url", "https://example.test/paper-1", "--domain", "agents", "--vault-root", str(vault)]
            output = io.StringIO()
            with mock.patch.object(sys, "argv", argv), redirect_stdout(output):
                status = cli.main()
            payload = json.loads(output.getvalue())
            self.assertEqual(status, 1)
            receipt = Path(payload["receipt_path"]).read_text(encoding="utf-8")
            self.assertNotIn("MINERU_API_TOKEN", receipt)
            self.assertNotIn("full.md contents", receipt)
