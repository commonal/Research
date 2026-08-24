from __future__ import annotations

from pathlib import Path
from unittest import TestCase
import json
import uuid

from research_pulse.acceptance.models import (
    BrowserStatus,
    FinalStatus,
    RealPaperAcceptanceReceipt,
    RunKind,
    StageStatus,
    stage_sequence,
)
from research_pulse.acceptance.receipt import AcceptanceReceiptWriter, assert_sanitized, safe_error


STARTED_AT = "2026-08-22T08:00:00Z"
ROOT = Path(__file__).resolve().parents[1]


def _receipt(
    *,
    run_kind: RunKind = RunKind.SIMULATED,
    final_status: FinalStatus = FinalStatus.ACCEPTANCE_FAILED,
) -> RealPaperAcceptanceReceipt:
    if final_status == FinalStatus.ENVIRONMENT_BLOCKED:
        statuses = (
            StageStatus.BLOCKED,
            StageStatus.SKIPPED,
            StageStatus.SKIPPED,
            StageStatus.SKIPPED,
            StageStatus.SKIPPED,
            StageStatus.SKIPPED,
            StageStatus.PASSED,
        )
    elif final_status == FinalStatus.ACCEPTANCE_FAILED:
        statuses = (
            StageStatus.PASSED,
            StageStatus.PASSED,
            StageStatus.FAILED,
            StageStatus.SKIPPED,
            StageStatus.SKIPPED,
            StageStatus.SKIPPED,
            StageStatus.PASSED,
        )
    else:
        statuses = (StageStatus.PASSED,) * 7
    return RealPaperAcceptanceReceipt(
        receipt_schema_version=1,
        run_id="run-test",
        run_kind=run_kind,
        final_status=final_status,
        started_at=STARTED_AT,
        topic="LLM agent memory",
        domain="llm_agent_memory",
        model="deepseek-chat",
        stages=stage_sequence(statuses, started_at=STARTED_AT),
        source_id="2608.00001v1" if final_status == FinalStatus.PASSED else None,
        knowledge_id="kp:arxiv:2608.00001v1" if final_status == FinalStatus.PASSED else None,
        knowledge_version="2026-08-22T08:00:00Z" if final_status == FinalStatus.PASSED else None,
        content_sha256="a" * 64 if final_status == FinalStatus.PASSED else None,
        browser_status=BrowserStatus.PENDING,
    )


class AcceptanceModelTests(TestCase):
    def test_simulated_run_cannot_be_a_real_pass(self) -> None:
        with self.assertRaisesRegex(ValueError, "simulated"):
            _receipt(run_kind=RunKind.SIMULATED, final_status=FinalStatus.PASSED)

        real = _receipt(run_kind=RunKind.REAL, final_status=FinalStatus.PASSED)

        self.assertTrue(real.is_real_core_pass)
        self.assertFalse(real.is_complete_product_pass)

    def test_environment_blocker_requires_all_later_core_stages_to_skip(self) -> None:
        receipt = _receipt(final_status=FinalStatus.ENVIRONMENT_BLOCKED)
        self.assertEqual(receipt.stages[0].status, StageStatus.BLOCKED)
        with self.assertRaisesRegex(ValueError, "after an environment blocker"):
            RealPaperAcceptanceReceipt(
                **{
                    **receipt.__dict__,
                    "stages": stage_sequence(
                        (
                            StageStatus.BLOCKED,
                            StageStatus.PASSED,
                            StageStatus.SKIPPED,
                            StageStatus.SKIPPED,
                            StageStatus.SKIPPED,
                            StageStatus.SKIPPED,
                            StageStatus.PASSED,
                        ),
                        started_at=STARTED_AT,
                    ),
                }
            )

    def test_payload_round_trip_is_strict(self) -> None:
        receipt = _receipt(final_status=FinalStatus.ENVIRONMENT_BLOCKED)
        self.assertEqual(RealPaperAcceptanceReceipt.from_payload(receipt.to_payload()), receipt)
        payload = receipt.to_payload()
        payload["raw_pdf"] = "forbidden"
        with self.assertRaisesRegex(ValueError, "unsupported"):
            RealPaperAcceptanceReceipt.from_payload(payload)


class AcceptanceReceiptTests(TestCase):
    def test_production_timeout_keeps_a_specific_safe_error_code(self) -> None:
        code, summary = safe_error(RuntimeError("Production did not publish: reading_timeout"), stage="production")

        self.assertEqual(code, "reading_timeout")
        self.assertNotIn("provider", summary)

    def test_safe_error_removes_secrets_connections_paths_and_provider_urls(self) -> None:
        code, summary = safe_error(
            RuntimeError(
                "api_key=top-secret postgresql://user:pass@localhost/db "
                "sk-1234567890123456 C:\\Users\\alice\\paper.pdf https://provider.example/body\nraw"
            ),
            stage="discovery",
        )
        self.assertEqual(code, "arxiv_unreachable")
        for forbidden in ("top-secret", "user:pass", "sk-", "alice", "provider.example", "\n"):
            self.assertNotIn(forbidden, summary)
        self.assertLessEqual(len(summary), 240)

    def test_writer_round_trips_json_and_markdown_without_temporary_files(self) -> None:
        receipt = _receipt(final_status=FinalStatus.ENVIRONMENT_BLOCKED)
        root = ROOT / "data" / f"test-acceptance-receipt-{uuid.uuid4().hex}"
        try:
            paths = AcceptanceReceiptWriter(root).write(receipt)

            self.assertEqual(
                RealPaperAcceptanceReceipt.from_payload(json.loads(paths.json.read_text(encoding="utf-8"))),
                receipt,
            )
            self.assertIn("environment_blocked", paths.markdown.read_text(encoding="utf-8"))
            self.assertEqual(list(root.glob("*.tmp")), [])
        finally:
            if root.exists():
                for path in root.iterdir():
                    path.unlink()
                root.rmdir()

    def test_sensitive_receipt_content_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "sensitive"):
            assert_sanitized('{"database":"postgresql://user:password@localhost/db"}')
