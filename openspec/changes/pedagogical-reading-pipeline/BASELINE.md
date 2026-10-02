# Baseline 测试快照（Gate B）— 改前标准答案

> 日期：2026-08-25。执行：`python -m unittest discover -s tests`
> 结果：**Ran 285 tests — FAILED (failures=4, errors=5, skipped=13)**
> 这份清单是**改前状态**。之后 pedagogical 实现**绝不能新增任何失败**；一旦出现不在下列清单里的新失败，必须视为回归并修复，而不是"本来就挂"。

## 5 个 ERROR（全在 acceptance / legacy 链）

| 测试 | 根因 |
|---|---|
| `test_environment_blocker_requires_all_later_core_stages_to_skip` | `acceptance/models.py:227` `ValueError: A status is required for every acceptance stage.` |
| `test_payload_round_trip_is_strict` | 同上 `acceptance/models.py:227` |
| `test_writer_round_trips_json_and_markdown_without_temporary_files` | 同上 `acceptance/models.py:227` |
| `test_cli_uses_safe_defaults_returns_nonzero_and_prints_no_secret` | `acceptance/real_paper.py:373` `NameError: DEFAULT_DEEPSEEK_TEXT_MODEL is not defined` |
| `test_runtime`（module load） | `api/runtime.py:18` `ImportError: cannot import name 'DeepSeekStructuredExtractor' from 'production.adapters'` |

> 根因：用户重构中途，`acceptance/` 与老的 `api/runtime.py`（Postgres-RAG legacy 链）引用了已移除的符号/字段。与本方案无关，属既有技术债。

## 4 个 FAIL

| 测试 | 根因 |
|---|---|
| `test_current_published_note_is_not_used_as_the_golden_fixture` | `knowledge/papers/2608.18351v1` 当前发布笔记内容与 golden rubric 不符（缺"安全成功"等概念）——已发布笔记是旧版 |
| `test_simulated_run_cannot_be_a_real_pass` | `acceptance/models.py` `ValueError` 未含期待的 "simulated" |
| `test_wrong_source_id_is_rejected` | 无该 source 的已发布笔记，`AcceptanceValidationError` 文案不满足断言 |
| `test_writer_repair_rejects_a_full_document_replacement` | `test_v2_writer_contract.py:85` `repair_calls 2 != 1`（与预存 `_blocks_publication` 重构相关） |

## 说明

- 以上 4+5 个 == 我之前反证过"**撤掉我改动也照样失败**"的那批，确认为预存失败。
- pedagogical 实现只应触碰 `ReaderProductionService` / pedagogical 模块 / 前端渲染，**不碰** `acceptance/`、`api/runtime.py`、`knowledge/papers/2608.18351v1`——从根上避免引入这批失败。
