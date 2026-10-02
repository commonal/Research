import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from research_pulse.production import main as production_main
from research_pulse.production.pipeline import PaperCandidate


class ProductionMainObservabilityTests(unittest.TestCase):
    def test_cli_prints_delivered_route_fallback_and_receipt_path(self) -> None:
        receipt = {
            "source_id": "paper-1",
            "receipt_status": "completed",
            "status": "published",
            "stop_reason": "completed",
            "published_path": "vault/papers/paper-1/note.md",
            "receipt_path": "vault/receipts/paper-1/note.json",
            "audit_candidate_path": "vault/audit/paper-1/note/final-candidate.md",
            "requested_route": "pedagogical",
            "delivered_route": "generic",
            "fallback_used": True,
            "pedagogical_fallbacks": ["gates:evidence"],
        }
        service = mock.Mock()
        service.process.return_value = receipt

        with (
            mock.patch.object(production_main, "ReaderProductionService", return_value=service),
            mock.patch.object(production_main, "PedagogicalService", return_value=object()),
            mock.patch.object(production_main.DeepSeekPaperReadingModel, "from_environment", return_value=object()),
            mock.patch.object(
                production_main,
                "_single_candidate",
                return_value=PaperCandidate("paper-1", "Paper", "https://example.test", "ai"),
            ),
            mock.patch(
                "sys.argv",
                ["research-pulse", "--source-id", "paper-1", "--domain", "ai"],
            ),
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = production_main.main()

        self.assertEqual(0, exit_code)
        rendered = output.getvalue()
        self.assertIn("route=pedagogical->generic", rendered)
        self.assertIn("fallback=gates:evidence", rendered)
        self.assertIn("stop=completed", rendered)
        self.assertIn("receipt=vault/receipts/paper-1/note.json", rendered)
        self.assertIn("audit=vault/audit/paper-1/note/final-candidate.md", rendered)


if __name__ == "__main__":
    unittest.main()
