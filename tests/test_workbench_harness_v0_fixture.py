from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from research_pulse.workbench.harness_v0_fixture import (
    FixtureValidationError,
    load_and_validate_fixture,
)


class WorkbenchHarnessV0FixtureTests(TestCase):
    def setUp(self) -> None:
        self.manifest = Path("experiments/workbench-harness-v0/manifest.json")

    def test_frozen_fixture_records_runtime_model_tools_budget_questions_and_materials(self) -> None:
        fixture = load_and_validate_fixture(self.manifest)

        self.assertEqual(fixture["harness"]["package"], "deepagents==0.7.11")
        self.assertEqual(fixture["model"]["profile_key"], "openai:deepseek-v4-flash")
        self.assertEqual(
            fixture["allowed_tools"],
            ["search_sources", "read_paper_metadata", "read_managed_blocks", "read_run_status"],
        )
        self.assertEqual(len(fixture["questions"]), 3)
        self.assertEqual(len(fixture["materials"]), 3)
        self.assertEqual(
            set(fixture["budget"]),
            {"model_rounds", "tool_calls", "block_reads", "wall_seconds", "input_tokens", "output_tokens"},
        )

    def test_changed_material_hash_fails_closed(self) -> None:
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        payload["materials"][0]["sha256"] = "0" * 64
        with TemporaryDirectory() as directory:
            altered = Path(directory) / "manifest.json"
            altered.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(FixtureValidationError, "material hash mismatch"):
                load_and_validate_fixture(altered, project_root=Path.cwd())
