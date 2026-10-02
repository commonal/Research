# V1 failure-injection receipt

Date: 2026-09-01

Command:

`uv run --with pytest python -m pytest -q tests/test_workbench_preparation.py tests/test_workbench_agent_runtime.py tests/test_workbench_budget_enforcer.py tests/test_workbench_exploration_artifact.py tests/test_workbench_exploration_service.py tests/test_workbench_note_runs.py tests/test_workbench_deepagents_v0.py`

Result: `24 passed, 10 subtests passed`.

Covered contracts include parsing failure and retry, Harness external failure, user cancellation, every hard budget dimension, partial/terminal state persistence, completed exploration with no candidate questions, independent NoteRun failure/retry, and fail-closed Deep Agents capability inventory.

Frontend failure presentation is covered by the Workbench component suite, including a visible retryable NoteRun failure and exploration stop/retry/partial-result controls.
