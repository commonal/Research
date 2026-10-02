# V1 validation receipt

Date: 2026-09-01

## Passing gates

- Workbench backend regression: `89 tests`, passed.
- Failure-injection suite: `24 tests + 10 subtests`, passed.
- Worker suite: `15 tests`, passed.
- Frontend suite: `9 files / 36 tests`, passed.
- Frontend Markdown security check: passed.
- Frontend production build: passed.
- `openspec validate add-session-paper-workbench --strict`: passed.
- Real-browser question-first and paper-first receipts: passed; see `v1-browser-acceptance.md`.

The independent V0 Harness receipt remains under `experiments/workbench-harness-v0/receipts/v0-acceptance-2026-09-01.md`; it is not used as V1 product acceptance.

## Repository-wide gate: not passing (re-verified 2026-09-01, IDENTICAL to the 89-test run)

`python -m unittest discover -s tests -q` ran 573 tests and ended with 4 failures, 5 errors, and 13 skips. Every red item is OUTSIDE the Workbench test set (`tests/test_workbench_*.py`, `research_pulse/workbench/`), which contributed zero failures and zero skips. All items belong to the concurrently dirty note-only/reading refactor and were PRESERVED, not reverted or overwritten.

### 5 errors — owner: `research_pulse/acceptance/` + `api/runtime.py` (note-only/reading refactor)

| test | owner / cause |
|---|---|
| `test_environment_blocker_requires_all_later_core_stages_to_skip` (`test_real_paper_acceptance_models`) | `acceptance/models.py::stage_sequence` now requires a status for every stage; fixture still supplies seven retired stages → `ValueError` |
| `test_payload_round_trip_is_strict` (`test_real_paper_acceptance_models`) | same retired-stage `stage_sequence` mismatch |
| `test_writer_round_trips_json_and_markdown_without_temporary_files` (`test_real_paper_acceptance_models`) | same retired-stage `stage_sequence` mismatch |
| `test_cli_uses_safe_defaults_returns_nonzero_and_prints_no_secret` (`test_real_paper_acceptance_runner`) | `real_paper.py::main` references removed `DEFAULT_DEEPSEEK_TEXT_MODEL` → `NameError` |
| `test_runtime` (`unittest.loader._FailedTest`) | `api/runtime.py` imports retired `DeepSeekStructuredExtractor` from `production/adapters` → `ImportError` |

### 4 failures — owner: reading/acceptance refactor (+ 2 pre-existing baseline)

| test | owner / cause |
|---|---|
| `test_current_published_note_is_not_used_as_the_golden_fixture` (`test_paper_reading_quality`) | golden-note encoding fixture drifted from current published note (known baseline) |
| `test_writer_repair_rejects_a_full_document_replacement` (`test_v2_writer_contract`) | expects `repair_calls == 1`, current two-call behavior yields 2 (known baseline) |
| `test_simulated_run_cannot_be_a_real_pass` (`test_real_paper_acceptance_models`) | consequence of the `stage_sequence` error above: raises "A status is required…" before reaching the "simulated" assertion |
| `test_wrong_source_id_is_rejected` (`test_real_paper_acceptance_validation`) | message now "No published note exists for the selected source." vs assertion expecting retired "does not match" wording |

### 13 skips — environmental only, none in Workbench

`test_note_only_api` skips when `DEEPSEEK_API_KEY` unset; `test_runtime` skips when `RESEARCH_PULSE_TEST_DATABASE_URL` unset; remaining are network/GPU-conditional. Workbench tests: **0 failures, 0 skips**.

This receipt deliberately does not claim task 8.7 fully green. The Workbench-scoped gates all pass (below); the repository-wide gate is red only because of the user's concurrent, intentionally-preserved note-only/reading refactor. Fixing those items would modify files owned by that parallel work, so they are left untouched here.

## Remaining product-validation blocker

Task 8.6 requires at least one genuinely unguided external user. No synthetic agent run can satisfy that criterion. The browser journeys above validate function and integration, not first-user comprehension.
