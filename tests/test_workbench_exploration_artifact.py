from unittest import TestCase

from research_pulse.workbench.models import ExplorationArtifact


class WorkbenchExplorationArtifactTests(TestCase):
    def test_no_candidate_questions_is_a_valid_completed_artifact(self) -> None:
        artifact = ExplorationArtifact(final_draft="目前证据只支持有限结论。")
        payload = artifact.public_dict()
        self.assertIsNone(payload["candidate_questions"])
        self.assertEqual(payload["draft_label"], "探索草稿/待验证")

    def test_method_map_always_carries_initial_evidence_scope_label(self) -> None:
        artifact = ExplorationArtifact(
            final_draft="形成两条候选路线。",
            candidate_questions=(),
            method_routes=("治理路线", "检索路线"),
        )
        payload = artifact.public_dict()
        self.assertEqual(payload["candidate_questions"], [])
        self.assertEqual(payload["method_map"]["label"], "当前证据范围内的初始地图")
