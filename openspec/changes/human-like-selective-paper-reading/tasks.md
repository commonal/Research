# Tasks

## 1. Preserve the validated foundation and failed-route evidence

- [x] 1.1 Preserve Canonical PaperIR/normalized formula, table, image, stable-anchor and safe-asset contracts; cache/download/parser redesign remains outside this change.
- [x] 1.2 Preserve the failed Unit Memo, dynamic Target, invalid-provider/visual-path and SectionDepthPlan experiment artifacts instead of rewriting them into apparent successes.
- [x] 1.3 Preserve the implemented deep `PaperReader.read(...) -> ReadingResult(draft, receipt, trace)` Interface, injected text/vision model port, deterministic fake Adapter and bounded Target fallback state.
- [x] 1.4 Preserve full-paper PaperModel, definition-neighborhood, table metric/value, selective vision, Writer allow-list and receipt regressions already implemented under the change.
- [x] 1.5 Record the final B/v2 decision in the domain glossary, product spec, ADR and change: v2 direct Writer is the quality baseline; CoverageLedger is retained; independent SectionDepthPlan is not a fixed stage.
- [x] 1.6 Preserve the source-complete Mamba and PaperBench generic experiment artifacts, including the accepted PaperBench v2 note and the v3/v4 depth-planning comparison.

## 2. Lock the final delta with red tests

- [x] 2.1 Add a failing CoverageLedger Interface regression proving every argument node, key experiment, eligible must-preserve fact, limitation and inline asset is assigned exactly once; missing, duplicate and invented obligations are rejected.
- [x] 2.2 Add a failing PaperModel regression proving ordered SectionExplanationContracts are produced in the same planning operation, contain reader questions/prerequisite bridges/reasoning steps/experiment slots/asset jobs/transitions/stop conditions, and contain no fixed length quota.
- [x] 2.3 Add a failing routing regression proving an independent section-planning repair runs only for ledger assignment conflict, while ReadingTarget fallback runs only for a named high-priority evidence gap.
- [x] 2.4 Add a failing v2 Writer regression proving all ledger obligations and allow-listed formula/table/mechanism facts survive one direct long-form generation without per-section model calls or Unit Memo synthesis.
- [x] 2.5 Add a failing quality-policy regression proving fixed character count is non-blocking when ledger, evidence, asset rendering and blind-reader checks pass, while missing obligations or unsupported facts remain blocking.

## 3. Implement the coverage-safe v2 path

- [x] 3.1 Add typed `SectionExplanationContract`, `CoverageObligation`, `CoverageLedger` and `CoverageConflict` domain state behind the existing PaperReader Interface.
- [x] 3.2 Extend the full-paper PaperModel Adapter contract to return SectionExplanationContracts in the same model call and validate their order, evidence handles, experiment slots and asset jobs.
- [x] 3.3 Implement deterministic CoverageLedger construction and unique assignment validation from the validated PaperModel and AssetPlan.
- [x] 3.4 Implement at most one section-planning repair for ledger conflicts; reuse existing reading state, send no images, perform no Target reread, and return bounded/failed if the repaired ledger remains invalid.
- [x] 3.5 Feed PaperModel, SectionExplanationContracts, CoverageLedger, DefinitionNeighborhoods, selected visual interpretations, evidence allow-list and unresolved boundaries to the v2 direct long-form Chinese Writer.
- [x] 3.6 Detect ledger omissions and unsupported Writer facts; allow at most one affected-section Writer repair without rewriting unrelated sections or inventing missing evidence.
- [x] 3.7 Keep the existing ReadingTarget/Bundle/Record/ArgumentMap loop only for recorded high-priority evidence gaps and preserve coverage/evidence-boundary/budget/no-progress stop reasons.
- [x] 3.8 Extend ReadingReceipt/Trace with planning and Writer repair calls, ledger totals/conflicts, asset decisions, non-blocking length observation, actual models, fallbacks and stop reason; keep trace outside Layer A/RAG.

## 4. Target-paper acceptance

- [x] 4.1 Replay `2608.18351v1` through the real PaperReader Interface without hand edits or paper-specific prompts; save PaperModel, contracts, ledger, neighborhoods, assets, receipt, trace and Chinese note.
- [x] 4.2 Compare the result with `evals/real-e2e/2608.18351v1-golden-human-note.md` and the best prior note on background/problem/gap, method hierarchy, broker/audit/reward mechanisms, experiment setup/results, transitions, limitations and unknowns.
- [ ] 4.3 Verify reward-symbol meanings, all key experiment obligations, exact main-table values, selected visual jobs and problem/method/experiment/limitation source facts pass the unchanged evidence gates.
- [ ] 4.4 Conduct a blind-reader review: a reader who has not opened the paper can explain why it exists, what it solves, how it works, how experiments are set up, the decisive results and what they do not establish.

## 5. Same-Module generalization

- [ ] 5.1 Only after the target paper passes, run Mamba and PaperBench through the same production PaperReader Interface and generic contracts; standalone eval success alone is insufficient.
- [ ] 5.2 Verify Mamba naturally places key formulas, method figures and result tables while PaperBench emits no invented formula and explains benchmark construction, judge validation, model experiments, human baseline, variants and boundaries.
- [ ] 5.3 Confirm neither paper uses paper-specific prompts, manual prose edits, fixed asset quotas, a mandatory independent SectionDepthPlan call or a fixed-length pass rule.

## 6. Regression and closure

- [x] 6.1 Run focused PaperReader, evidence-quality, production-adapter and real-material replay tests with no unexpected skips.
- [ ] 6.2 Run the relevant full Python/worker/frontend/Markdown/build suites and strict OpenSpec validation; report environmental blockers instead of counting skipped tests as passing.
- [ ] 6.3 Produce a final acceptance receipt that distinguishes implemented production behavior from standalone experiments and records remaining unknowns/costs.
- [ ] 6.4 Close only this reading change. Do not enable the strategy as production default, modify cache/Renderer/Publisher/API/browser/RAG, or archive unrelated changes.

## Execution note (2026-08-24)

- Read-only re-audit invalidated the previous assumption that ledger assignment implied final prose coverage. Tasks 2.4, 2.5, 3.6, 3.8, 4.3, 4.4 and 6.1 were reopened instead of treating historical receipts or checkboxes as acceptance.
- The first repair slices are now covered through the public `PaperReader.read` seam: unresolved fallback ledger conflict blocks Writer and returns failed; incomplete SectionExplanationContracts are rejected instead of receiving fabricated defaults; full-document Writer repair responses are rejected in favor of section patches; recovered Target source facts survive into the draft; named mechanism and experiment setup/result obligations trigger local repair; table DefinitionNeighborhood includes footnotes.
- The repaired Writer/quality path now requires blind-review supporting quotes for every SectionExplanationContract and every assigned CoverageLedger obligation; fabricated or absent quotes block completion without rewriting the note. ReadingReceipt records full-read, blind-review, unknown, PaperIR/prompt, asset-decision and image-byte fields across the implemented exit paths. The expanded focused suite passes 132/132 with no skips, and strict OpenSpec validation passes.
- The first post-audit real replay is preserved under `evals/real-e2e/full-paper-b-production-v28-contract-repair`. It correctly failed before Writer with `43` obligations, `0` assigned and a blocking coverage conflict instead of fabricating a note. The trace showed a local `P`-term definition detail incorrectly promoted to a high-priority Target and no fallback planning repair. Follow-up red→green fixes downgrade `not fully defined` local details, allow one ownership repair after a genuine Target, and prefer `coverage` over `budget` when the last allowed Target resolves all questions. The focused suite now passes 133/133; no automatic v29 replay was run.
- The single user-authorized v29 replay is preserved under `evals/real-e2e/full-paper-b-production-v29-contract-repair`. It reached `41/41` ledger coverage, one real vision call and one direct Writer call, retained the required mechanism names, reward symbols and exact headline values, and stopped for `coverage`. The note was still rejected because the independent blind-review response truncated at its 4,096-token provider limit; all nine sections therefore failed closed. The note also contains repeated internal-result, prompt-ablation and external-benchmark passages. Tasks 4.3 and 4.4 remain open; no automatic retry was run.

- 3.x focused implementation is complete and verified locally.
- 4.1 prompt audit has been implemented but target acceptance remains open: full-paper, note planning, and direct Writer instructions now derive preservation constraints from validated PaperModel state, MustPreserveFacts, experiment evidence, asset decisions, definition neighborhoods, and conclusion boundaries. The regression `python -m unittest tests.test_dynamic_writer_constraints tests.test_reading_provider_boundary tests.test_coverage_ledger tests.test_section_explanation_contract tests.test_reading_routing_contract tests.test_v2_writer_contract tests.test_reading_quality_policy` passes 18/18.
- 4.x target-paper replay is not complete. The provider-boundary repair is now recorded by the non-acceptance reference run `evals/real-e2e/full-paper-b-production-v6-provider-fix/reading-receipt.json`: it completed through real `full_paper`, recorded 38/38 ledger obligations, zero Target calls, one visual JSON failure, and one bounded section-repair failure. A fresh v7 replay is still required after the generic prompt audit, and 4.x/5.x must remain unchecked until its content and blind-reader gates pass.
- v7 attempt 1 recorded full-paper truncation after the single allowed retry; v7 attempt 2 reached full-paper but its note planner response truncated, leaving 37 obligations unassigned and ending at `target_fallback/bounded/evidence_boundary`; v7 attempt 3 completed 42/42 coverage but the generated note still missed required content (broker label and exact main-table row values); v7 attempt 4 again had a truncated note plan and an unsupported-symbol degradation; v7 attempt 5 completed 41/41 coverage with formula neighborhoods and reward-symbol meanings, but still omitted the broker label and exact main-table counts; v7 attempt 6 contained those content items and exact table rows but was correctly blocked by an unsupported structural-symbol diagnostic. The structural-boundary guard is now narrowed and its focused regression suite passes 29/29. All diagnostics are retained under `evals/real-e2e/full-paper-b-production-v7`; no 4.x task is complete.
- After the user requested stopping repeated provider retries, attempt 7 was terminated without treating its partial output as evidence. The best durable v7 diagnostic remains `attempt-6-best-content-unsupported-symbol`; 4.1–4.4 remain unchecked.
- The final target-paper route is accepted in `evals/real-e2e/full-paper-b-production-v27-final/acceptance-review.md`. The fresh PaperModel response came from v20 and was replayed without prose edits through the current production Interface. v27 completed with 34/34 ledger obligations, one real vision call, one direct Writer call, one bounded local Writer repair, no degradation, and no raw IDs or broken asset links. The target-paper 4.x gates and 119 focused tests pass. The remaining section-planning provider truncation is explicitly recorded as a non-blocking deterministic-recovery issue; 5.x and change-level 6.2–6.4 remain open.
