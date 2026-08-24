from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
import os
import uuid

from research_pulse.acceptance.models import (
    BrowserStatus,
    FinalStatus,
    RealPaperAcceptanceReceipt,
    RunKind,
    StageStatus,
    stage_sequence,
)
from research_pulse.acceptance.preflight import AcceptanceConfig, PreflightDependencies
from research_pulse.acceptance.real_paper import (
    AcceptanceRunResult,
    RealAcceptanceDependencies,
    main,
    run_real_acceptance,
)
from research_pulse.acceptance.receipt import ReceiptPaths
from research_pulse.production.pipeline import PaperCandidate


ROOT = Path(__file__).resolve().parents[1]


class _Finder:
    def __init__(self, candidates=(), error=None) -> None:
        self.candidates = list(candidates)
        self.error = error

    def discover(self, **kwargs):
        if self.error:
            raise self.error
        return self.candidates[: kwargs["limit"]]


class _Registry:
    def was_processed(self, source_id: str) -> bool:
        return False


class _Graph:
    def __init__(self, candidate: PaperCandidate, status: str) -> None:
        self.candidate = candidate
        self.status = status
        self.calls = 0

    def invoke(self, state, config):
        self.calls += 1
        return {
            "candidate_ids": [self.candidate.source_id],
            "receipts": [
                {
                    "source_id": self.candidate.source_id,
                    "status": self.status,
                    "reason": "provider failed api_key=must-not-leak" if self.status != "published" else None,
                }
            ],
        }


def _candidate() -> PaperCandidate:
    from datetime import UTC, datetime

    return PaperCandidate(
        "2608.00001v1",
        "A Paper",
        "https://arxiv.org/abs/2608.00001v1",
        "llm_agent_memory",
        datetime(2026, 8, 22, tzinfo=UTC),
    )


class RealAcceptanceRunnerTests(TestCase):
    def setUp(self) -> None:
        suffix = uuid.uuid4().hex
        self.vault = ROOT / "data" / f"test-runner-vault-{suffix}"
        self.receipts = ROOT / "data" / f"test-runner-receipts-{suffix}"

    def tearDown(self) -> None:
        for root in (self.vault, self.receipts):
            if root.exists():
                for path in sorted(root.rglob("*"), reverse=True):
                    if path.is_file():
                        path.unlink()
                    elif path.is_dir():
                        path.rmdir()
                root.rmdir()

    def _config(self) -> AcceptanceConfig:
        return AcceptanceConfig(
            database_url="configured-database",
            deepseek_api_key="configured-secret",
            model="deepseek-chat",
            topic="LLM agent memory",
            domain="llm_agent_memory",
            vault_root=self.vault,
            receipt_root=self.receipts,
        )

    def _deps(self, *, finder, database_probe=lambda value: None, graph_factory=None):
        calls = []

        def forbidden_factory(candidate):
            calls.append("production")
            raise AssertionError("production must not run")

        return RealAcceptanceDependencies(
            run_kind=RunKind.SIMULATED,
            preflight=PreflightDependencies(
                database_probe=database_probe,
                docling_probe=lambda: None,
                writable_probe=lambda path: path.mkdir(parents=True, exist_ok=True),
                candidate_finder=finder,
            ),
            registry=_Registry(),
            rag=object(),
            production_graph_factory=graph_factory or forbidden_factory,
            interactive_graph_factory=lambda: (_ for _ in ()).throw(AssertionError("chat must not run")),
        ), calls

    def test_preflight_block_writes_simulated_receipt_and_never_runs_production(self) -> None:
        deps, calls = self._deps(
            finder=_Finder([_candidate()]),
            database_probe=lambda value: (_ for _ in ()).throw(RuntimeError("password=hidden")),
        )

        result = run_real_acceptance(self._config(), deps, run_id="blocked-run")

        self.assertEqual(result.receipt.run_kind, RunKind.SIMULATED)
        self.assertEqual(result.receipt.final_status, FinalStatus.ENVIRONMENT_BLOCKED)
        self.assertEqual(result.receipt.stages[0].status, StageStatus.BLOCKED)
        self.assertTrue(all(stage.status == StageStatus.SKIPPED for stage in result.receipt.stages[1:-1]))
        self.assertEqual(calls, [])
        self.assertNotIn("hidden", result.paths.json.read_text(encoding="utf-8"))
        self.assertEqual(result.exit_code, 1)

    def test_no_candidate_blocks_at_discovery_without_a_fixture_fallback(self) -> None:
        deps, calls = self._deps(finder=_Finder([]))

        result = run_real_acceptance(self._config(), deps, run_id="empty-run")

        self.assertEqual(result.receipt.final_status, FinalStatus.ENVIRONMENT_BLOCKED)
        self.assertEqual(result.receipt.stages[0].status, StageStatus.PASSED)
        self.assertEqual(result.receipt.stages[1].status, StageStatus.BLOCKED)
        self.assertEqual(calls, [])

    def test_failed_production_is_sanitized_and_never_retried(self) -> None:
        graph = _Graph(_candidate(), "failed")
        deps, _ = self._deps(finder=_Finder([_candidate()]), graph_factory=lambda candidate: graph)

        result = run_real_acceptance(self._config(), deps, run_id="failed-run")

        self.assertEqual(result.receipt.final_status, FinalStatus.ACCEPTANCE_FAILED)
        self.assertEqual(result.receipt.stages[2].status, StageStatus.FAILED)
        self.assertEqual(graph.calls, 1)
        self.assertNotIn("must-not-leak", result.paths.json.read_text(encoding="utf-8"))


class RealAcceptanceCliTests(TestCase):
    def test_cli_uses_safe_defaults_returns_nonzero_and_prints_no_secret(self) -> None:
        captured = {}
        receipt = RealPaperAcceptanceReceipt(
            receipt_schema_version=1,
            run_id="cli-test",
            run_kind=RunKind.REAL,
            final_status=FinalStatus.ENVIRONMENT_BLOCKED,
            started_at="2026-08-22T08:00:00Z",
            topic="LLM agent memory",
            domain="llm_agent_memory",
            model="deepseek-chat",
            stages=stage_sequence(
                (
                    StageStatus.BLOCKED,
                    StageStatus.SKIPPED,
                    StageStatus.SKIPPED,
                    StageStatus.SKIPPED,
                    StageStatus.SKIPPED,
                    StageStatus.SKIPPED,
                    StageStatus.PASSED,
                )
            ),
            browser_status=BrowserStatus.SKIPPED,
        )
        fake_result = AcceptanceRunResult(
            receipt,
            ReceiptPaths(ROOT / "evals" / "real-e2e" / "blocked.json", ROOT / "evals" / "real-e2e" / "blocked.md"),
        )

        def fake_run(config, deps, *, question, run_id=None):
            captured["config"] = config
            captured["question"] = question
            return fake_result

        output = StringIO()
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://user:password@localhost/db",
                "DEEPSEEK_API_KEY": "sk-1234567890123456",
                "DEEPSEEK_MODEL": "deepseek-chat",
            },
            clear=False,
        ), patch(
            "research_pulse.acceptance.real_paper.build_real_dependencies", return_value=object()
        ), patch(
            "research_pulse.acceptance.real_paper.run_real_acceptance", side_effect=fake_run
        ), redirect_stdout(output):
            exit_code = main([])

        self.assertEqual(exit_code, 1)
        self.assertEqual(captured["config"].topic, "LLM agent memory")
        self.assertEqual(captured["config"].domain, "llm_agent_memory")
        self.assertIn("主要方法", captured["question"])
        self.assertNotIn("password", output.getvalue())
        self.assertNotIn("sk-", output.getvalue())
        self.assertIn("core_status=environment_blocked", output.getvalue())
