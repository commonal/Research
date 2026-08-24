from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest import TestCase

from research_pulse.acceptance.orchestration import CostBoundary, SingleCandidateFinder, invoke_production_once
from research_pulse.acceptance.preflight import (
    AcceptanceConfig,
    EnvironmentBlocked,
    NoUnprocessedCandidate,
    PreflightDependencies,
    run_environment_preflight,
    select_unprocessed_candidate,
)
from research_pulse.production.pipeline import CandidateReceipt, PaperCandidate
from research_pulse.workflows.production import ProductionGraphDependencies, build_production_graph


ROOT = Path(__file__).resolve().parents[1]


def _candidate(source_id: str = "2608.00001v1") -> PaperCandidate:
    return PaperCandidate(
        source_id,
        f"Paper {source_id}",
        f"https://arxiv.org/abs/{source_id}",
        "llm_agent_memory",
        datetime(2026, 8, 22, tzinfo=UTC),
    )


class _Finder:
    def __init__(self, candidates=None, error=None, events=None) -> None:
        self.candidates = candidates or []
        self.error = error
        self.events = events if events is not None else []

    def discover(self, **kwargs):
        self.events.append("arxiv")
        if self.error:
            raise self.error
        return self.candidates[: kwargs["limit"]]


class _Registry:
    def __init__(self, processed=()) -> None:
        self.processed = set(processed)

    def was_processed(self, source_id: str) -> bool:
        return source_id in self.processed

    def mark_processed(self, source_id: str, knowledge_id: str) -> None:
        self.processed.add(source_id)


class _ProductionService:
    def __init__(self) -> None:
        self.calls = []

    def process(self, candidate: PaperCandidate) -> CandidateReceipt:
        self.calls.append(candidate.source_id)
        return CandidateReceipt(candidate.source_id, "published", f"kp:arxiv:{candidate.source_id}")


def _config(**changes) -> AcceptanceConfig:
    values = {
        "database_url": "configured-database",
        "deepseek_api_key": "configured-key",
        "model": "deepseek-chat",
        "topic": "LLM agent memory",
        "domain": "llm_agent_memory",
        "vault_root": ROOT / "knowledge",
        "receipt_root": ROOT / "evals" / "real-e2e",
    }
    values.update(changes)
    return AcceptanceConfig(**values)


class PreflightTests(TestCase):
    def test_checks_are_ordered_and_return_live_candidates_without_model_calls(self) -> None:
        events = []
        finder = _Finder([_candidate()], events=events)
        deps = PreflightDependencies(
            database_probe=lambda value: events.append("database"),
            docling_probe=lambda: events.append("docling"),
            writable_probe=lambda path: events.append(f"write:{path.name}"),
            candidate_finder=finder,
        )

        result = run_environment_preflight(_config(), deps)

        self.assertEqual(events, ["database", "docling", "write:knowledge", "write:real-e2e", "arxiv"])
        self.assertEqual(result.candidates, (_candidate(),))
        self.assertEqual(result.checks, ("configuration", "database", "docling", "writable_roots", "arxiv"))

    def test_each_preflight_failure_stops_later_checks(self) -> None:
        cases = (
            ("database", "database_unavailable"),
            ("docling", "docling_unavailable"),
            ("write", "vault_unwritable"),
            ("arxiv", "arxiv_unreachable"),
        )
        for fail_at, code in cases:
            events = []

            def probe(name):
                def operation(*args):
                    events.append(name)
                    if name == fail_at:
                        raise RuntimeError("secret provider body")
                return operation

            deps = PreflightDependencies(
                database_probe=probe("database"),
                docling_probe=probe("docling"),
                writable_probe=probe("write"),
                candidate_finder=_Finder([_candidate()], RuntimeError("offline") if fail_at == "arxiv" else None, events),
            )
            with self.subTest(fail_at=fail_at), self.assertRaises(EnvironmentBlocked) as raised:
                run_environment_preflight(_config(), deps)
            self.assertEqual(raised.exception.error_code, code)
            if fail_at != "arxiv":
                self.assertNotIn("arxiv", events)

    def test_missing_configuration_stops_before_probes(self) -> None:
        calls = []
        deps = PreflightDependencies(
            database_probe=lambda value: calls.append("database"),
            docling_probe=lambda: calls.append("docling"),
            writable_probe=lambda path: calls.append("write"),
            candidate_finder=_Finder([_candidate()], events=calls),
        )
        with self.assertRaises(EnvironmentBlocked) as raised:
            run_environment_preflight(_config(deepseek_api_key=None), deps)
        self.assertEqual(raised.exception.error_code, "deepseek_not_configured")
        self.assertEqual(calls, [])

    def test_selects_first_unprocessed_candidate_or_fails_without_fixture(self) -> None:
        candidates = [_candidate("old"), _candidate("new"), _candidate("newer")]
        selected = select_unprocessed_candidate(candidates, _Registry({"old"}))
        self.assertEqual(selected.source_id, "new")
        with self.assertRaises(NoUnprocessedCandidate):
            select_unprocessed_candidate(candidates, _Registry({"old", "new", "newer"}))


class SingleCandidateOrchestrationTests(TestCase):
    def test_existing_graph_processes_exactly_the_selected_live_candidate(self) -> None:
        candidate = _candidate()
        finder = SingleCandidateFinder(candidate)
        service = _ProductionService()
        graph = build_production_graph(ProductionGraphDependencies(finder, service))
        budget = CostBoundary()

        result = invoke_production_once(
            graph,
            run_id="run-1",
            topic="LLM agent memory",
            domain="llm_agent_memory",
            candidate=candidate,
            budget=budget,
        )

        self.assertEqual(result["candidate_ids"], [candidate.source_id])
        self.assertEqual(service.calls, [candidate.source_id])
        self.assertEqual(finder.calls, 1)
        self.assertEqual(budget.production_calls, 1)
        with self.assertRaisesRegex(RuntimeError, "only one production"):
            invoke_production_once(
                graph,
                run_id="run-1",
                topic="LLM agent memory",
                domain="llm_agent_memory",
                candidate=candidate,
                budget=budget,
            )

    def test_cost_boundary_allows_only_one_scoped_question(self) -> None:
        budget = CostBoundary()
        budget.consume_question()
        with self.assertRaisesRegex(RuntimeError, "only one scoped question"):
            budget.consume_question()
