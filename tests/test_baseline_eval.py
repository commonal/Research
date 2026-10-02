from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from evals.baseline.summarize import (
    InputError,
    TaskSpec,
    build_summary,
    load_records,
    load_tasks,
    main,
)
from evals.baseline.collect_http import normalize_exploration_status


def _side(*, status: str = "completed", citation_checks=None, claims_total=2, claims_supported=2):
    return {
        "status": status,
        "route": "paper",
        "route_correct": True,
        "citation_checks": citation_checks if citation_checks is not None else [{"correct": True}],
        "claims_total": claims_total,
        "claims_supported": claims_supported,
        "focus_continuity": None,
        "recovery_success": None,
        "latency_ms": 100,
        "input_tokens": 10,
        "output_tokens": 5,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


class BaselineEvaluationTests(unittest.TestCase):
    def test_direction_checkpoint_is_not_reported_as_still_pending(self):
        self.assertEqual(normalize_exploration_status("awaiting_user", "partial"), "blocked")
        self.assertEqual(normalize_exploration_status("awaiting_user_decision", "partial"), "blocked")
        self.assertEqual(normalize_exploration_status("completed", "answer"), "completed")
        self.assertEqual(normalize_exploration_status("completed", ""), "pending")

    def test_build_summary_reports_quality_and_latency_for_both_sides(self):
        tasks = {"task-1": TaskSpec("task-1", "quality", "comparison")}
        records = {
            "task-1": {
                "baseline": _side(citation_checks=[{"correct": True}, {"correct": False}], claims_supported=1),
                "workbench": _side(citation_checks=[{"correct": True}, {"correct": True}]),
            }
        }
        records["task-1"]["baseline"]["citation_checks"] = [True, False]
        records["task-1"]["workbench"]["citation_checks"] = [True, True]

        summary = build_summary(tasks, records)

        self.assertTrue(summary["complete"])
        self.assertEqual(summary["sides"]["baseline"]["citation_precision"], 0.5)
        self.assertEqual(summary["sides"]["workbench"]["citation_precision"], 1.0)
        self.assertEqual(summary["sides"]["workbench"]["latency_ms"]["median"], 100.0)

    def test_cli_requires_all_tasks_to_have_completed_paired_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = root / "tasks.jsonl"
            records = root / "records.jsonl"
            output = root / "output"
            tasks.write_text(json.dumps({"task_id": "task-1", "kind": "quality", "expected_route": "comparison"}) + "\n", encoding="utf-8")
            records.write_text(json.dumps({"task_id": "task-1", "baseline": _side(status="pending"), "workbench": _side(status="completed")}) + "\n", encoding="utf-8")

            self.assertEqual(main(["--tasks", str(tasks), "--records", str(records), "--output-dir", str(output)]), 2)
            self.assertFalse((output / "summary.json").exists())

    def test_unknown_task_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = root / "tasks.jsonl"
            records = root / "records.jsonl"
            tasks.write_text(json.dumps({"task_id": "task-1", "kind": "quality", "expected_route": "comparison"}) + "\n", encoding="utf-8")
            records.write_text(json.dumps({"task_id": "task-unknown", "baseline": _side(), "workbench": _side()}) + "\n", encoding="utf-8")

            loaded_tasks = load_tasks(tasks)
            with self.assertRaises(InputError):
                load_records(records, loaded_tasks)


if __name__ == "__main__":
    unittest.main()
