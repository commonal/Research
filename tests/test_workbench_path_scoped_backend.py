"""Path-scoped backend: raw file writes may not touch canonical workspace state."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import WriteResult

from research_pulse.workbench.deepagents_v0 import PathScopedBackend


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def __call__(self, action: str, path: str) -> None:
        self.events.append((action, path))


class PathScopedBackendTests(TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.backend = FilesystemBackend(root_dir=str(self.root))
        self.recorder = _Recorder()
        self.scoped = PathScopedBackend(self.backend, self.recorder)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_write_canonical_research_map_is_rejected(self) -> None:
        result = self.scoped.write("research-map.json", '{"nodes": []}')
        self.assertIsInstance(result, WriteResult)
        self.assertIsNotNone(result.error)
        self.assertIn("canonical", result.error)
        self.assertFalse((self.root / "research-map.json").exists())

    def test_write_workspace_json_is_rejected(self) -> None:
        result = self.scoped.write("workspace.json", "{}")
        self.assertIsNotNone(result.error)

    def test_write_evidence_file_is_rejected(self) -> None:
        result = self.scoped.write("evidence/E-012.json", "{}")
        self.assertIsNotNone(result.error)

    def test_write_subquestions_json_is_rejected(self) -> None:
        result = self.scoped.write("subquestions.json", '{"items": []}')
        self.assertIsNotNone(result.error)

    def test_write_research_plan_json_is_rejected(self) -> None:
        result = self.scoped.write("research-plan.json", '{"stage": "ready"}')
        self.assertIsNotNone(result.error)

    def test_write_scratch_notes_is_allowed(self) -> None:
        result = self.scoped.write("notes/evidence.md", "# finding")
        self.assertIsNone(result.error)
        self.assertTrue((self.root / "notes" / "evidence.md").exists())

    def test_write_drafts_report_is_allowed(self) -> None:
        result = self.scoped.write("drafts/report.md", "# draft")
        self.assertIsNone(result.error)

    def test_edit_canonical_path_is_rejected_even_after_scratch_write(self) -> None:
        # A scratch note writes fine, but an edit to a canonical file is still denied.
        self.scoped.write("notes/x.md", "content")
        result = self.scoped.edit("research-map.json", "0", "replaced")
        self.assertIsNotNone(result.error)

    def test_rejected_write_still_emits_safe_event(self) -> None:
        self.scoped.write("research-map.json", "{}")
        self.assertTrue(self.recorder.events)
        action, path = self.recorder.events[-1]
        self.assertEqual(action, "write_file")
        self.assertEqual(path, "research-map.json")


if __name__ == "__main__":
    import unittest

    unittest.main()
