# Research Pulse

一个面向 AI 研究者的**研究工作台**：它替你把一篇论文读透，并且保证**每一句结论都能点回原文**。

> 项目定位、范围与"明确不做"以 [`docs/INDEX.md`](./docs/INDEX.md) 为唯一入口；工作台 V1 设计见 [`docs/research-assistant-workspace-design-v1.md`](./docs/research-assistant-workspace-design-v1.md)。

## 它解决什么问题

和 LLM 一起读论文有两个绕不过去的毛病，这个项目就是冲这两件事来的：

**1. 它可能编。** 你问"这篇论文的局限是什么"，它给你一段听起来很专业、但原文里根本没有的话。
→ 所以这里**没有"生成一段总结"这条路**。任何结论都必须挂在一个真实的 `block_id` 上，带着页码和坐标；发布前跑证据覆盖校验，**找不到出处的结论进不了产物**。

**2. 长任务会丢状态。** 读到一半关掉、预算用完、工具挂了、后端重启——进度就没了。
→ 所以研究状态**不放在对话历史里**，而是落盘成结构化 JSON；一次执行是一个可以恢复、可以取消、可以重来但不会覆盖历史的 `Attempt`。

## 两个能力面

### 一、可信论文精读

管线拆成 6 个职责分离的环节，不是一个大 prompt：

```text
导入 → 归一化 → 理解 → 规划 → 选材 → 读图 → 写作 → 渲染 → 验收
```

- **事实与讲法分离**：`PaperModel` 只装"论文是什么"（论点、论证链、实验、局限），`TeachingPlan` 只装"我准备怎么讲"。**改讲法不会污染事实层**，事实层可以被复用。
- **坐标级溯源**：结论保留 `source / block / 页码 / bbox`。MinerU 返回的是 1000×1000 归一化坐标，前端按来源坐标空间显式转换，**不靠数值大小猜**。
- **逐行高亮**：解析出的块级 bbox 会与 PDF.js 的文本 run 求交，按行分组渲染成多个贴合文字行的矩形，而不是一个大方块盖住整段。
- **引用降级**：找不到对应块的引用渲染成**灰色不可点击**的标记，而不是伪装成能跳转的死链。
- **盲读验收**：发布前有一道 `BlindReader` 门禁（证据门禁之后）——**输入只有渲染后的成品**，由读者视角按维度打分，检查"读者是否真的看懂了"，结果回落到 `BlindReaderResult`。

### 二、持久化研究工作台

一个 Workspace 是一个长期研究项目；具体的研究问题在探索过程中形成，由你确认后成为当前焦点。

```text
CREATED → INITIAL_RESEARCH → WAITING_FOR_USER_ACTION
        → INVESTIGATING → WAITING_FOR_USER_ACTION ↺ → ARCHIVED
```

- **Agent 持续在场**：不是"搜索按钮"，而是它持续推进研究，在**用户决策点**停下来汇报当前理解、给出候选方向，你选完继续。
- **四级执行模型**：`Workspace`（研究项目）→ `Session`（一次打开的对话上下文）→ `Run`（一条消息的一次执行）→ `Attempt`（可重试、可回滚的不可变执行实例）。
  **Session ≠ Run** 是刻意区分的——否则 HITL 暂停恢复、失败重跑、重新执行都会纠缠在一起。
- **唯一事实源**：结构化 canonical JSON 是唯一事实源，Markdown 永远是**只读投影**。任何编辑（包括界面上改的）都必须落到 JSON，再由程序刷新 Markdown。
- **受控写入口**：Agent 不能裸写文件，只能通过 `update_research_map` / `add_evidence` 等受控领域工具改状态，编辑同样落到 canonical JSON。
- **失败恢复**：预算耗尽、网络中断、工具失败、手动停止四类场景都有对应的恢复路径，旧 Attempt 保持不可变、事件不重复写入。

## 架构要点

| 关注点 | 做法 |
|---|---|
| 事实源 | canonical JSON（`workspace.json` / `research-map.json` / `evidence/*.json`），MD 为确定性投影 |
| 证据 | `source_id + block_id`，结论级 provenance，发布前证据覆盖门禁 |
| 执行 | Attempt 级 lease + 启动时收敛遗留 `running`，保证重开即可继续 |
| 权限 | capability policy + 独立 MCP server 隔离只读查询、发信与审批 |
| 预算 | 应用层 step / token / deadline 预算强制，超限产生可继续的失败而非静默截断 |
| 事件 | turn / run / attempt / note 事件流，UI 与回执都从事件重建 |

## 快速开始

```powershell
# 一键启动前后端（后端 8001，前端 5173）
.\scripts\start-dev.ps1

# 同时拉起 Docker PostgreSQL 并开启后端热重载
.\scripts\start-dev.ps1 -StartDatabase -Reload

# 停止
.\scripts\start-dev.ps1 -Stop
```

可手动启动（脚本不可用时）：

```bash
docker compose -f compose.researchrag.yaml up -d postgres
.venv/Scripts/python.exe -m uvicorn research_pulse.api.runtime:create_runtime_app --factory --host 127.0.0.1 --port 8001
curl http://127.0.0.1:8001/api/health      # 期望 {"status":"ok"}
```

配置见 [`.env.example`](./.env.example)（`.env` 与 `frontend/.env.local` 均不入库）。需要 DeepSeek key；论文解析默认走 MinerU，可选 PaddleOCR。

## 验证

```bash
# 后端：934 个用例（testpaths 未配置，请显式指定 tests/ 并排除 tmp/）
python -m pytest tests --ignore=tmp

# 前端：13 个测试文件 / 96 个用例，实测全部通过
cd frontend && npm run test && npm run build
```

> `tmp/` 里存放的是一次性脚本，其中有 18 个 `.py` 会在导入期直接执行并 `raise SystemExit`，被 pytest 收集到会导致 `INTERNALERROR`。因此必须带上 `--ignore=tmp`。

```powershell
# 收束检查：文档、黄金任务、工作台回归、前端测试与构建
.\scripts\verify-resume-readiness.ps1
```

固定的验收任务集在 [`evals/workbench_golden_tasks.jsonl`](./evals/workbench_golden_tasks.jsonl)——**9 个任务，覆盖 4 类失败恢复 + 3 类选区交互 + 1 类研究推进 + 1 类基线对比**。

真实 UI 验收记录（含失败与修复过程）见 [`docs/workbench-real-ui-acceptance-2026-09-11.md`](./docs/workbench-real-ui-acceptance-2026-09-11.md)，例如：

- 真实论文 **DPO4Rec**，6 页 PDF.js 画布、**1532 个文本 run**
- 目标回答的 **10 个引用全部 resolved**，无不可定位引用
- 第 1 页摘要引用生成 **25 个逐行高亮矩形**，第 2 页 15 个，均与后端 bbox 对齐
- 一次真实排障：旧实现只清理已过期的 Attempt lease，导致刚中断的运行在一段时间内**假 `running`**；改为应用启动时收敛遗留运行，重启后立即变为可继续的 `abandoned`

## 已知限制

这部分是**有意写清楚的**，因为一个只说优点的项目不值得信任：

- **基线对比尚未完成。** `evals/baseline/runs/live-pilot` 只记录到 9 个任务中的 1 个（`complete: false`），工作台侧那次运行是 `failed`。评估框架、任务定义和汇总器都已就绪，缺的是完整回执，所以**目前不能声称工作台在质量上优于普通 RAG**。
- **后端测试当前是红的。** 实测 `python -m pytest tests --ignore=tmp` 的结果是 **910 passed / 10 failed / 14 skipped**。前端 96 个用例全绿。这 10 个失败可稳定复现，根因包括：`research_pulse/acceptance/models.py:227` 的阶段状态校验比部分测试的构造更严格（5 个），以及 `Writer` 修复路径实际被调用 2 次而契约测试期望 1 次（`test_v2_writer_contract.py`）。**这是待修复的真实缺陷，不是环境差异**，检查清单见下文。
- **证据抽取的准确率/召回率还没有数字。** 引用定位准确率与证据覆盖率已被列为待建立基线（见 [`docs/resume-readiness.md`](./docs/resume-readiness.md) 第 6 节），指标在首次完整运行前标记为 `UNKNOWN`，**不用估计值填充**。
- **论文换版本会错位。** provenance 锚定在特定解析版本的块上；arXiv 更新版本后块坐标可能失效。当前的兜底是降级为不可点击标记，而不是自动重定位。
- **V1 只支持单篇 Anchor Paper。** 允许围绕子问题检索、阅读、引用多篇 supporting papers，但不做多篇平级锚点，也不做开放式领域综述。
- **单人自用工具，没有外部用户数据。** 因此本项目不主张任何"提升研究效率 X%"之类的效果量。
- **开发过程中使用了 AI 编程工具。** 设计决策、验收纪律、缺陷根因定位与修复由作者完成；部分实现是与这些工具协作产出的。仓库里 `.agents/`、`.hermes/`、`.reasonix/` 即为相关痕迹。**这里主动说明，而不是等被发现。**

## 文档地图

| 文档 | 用途 |
|---|---|
| [`docs/INDEX.md`](./docs/INDEX.md) | **唯一入口**，定位权威 |
| [`docs/research-assistant-workspace-design-v1.md`](./docs/research-assistant-workspace-design-v1.md) | 工作台 V1 设计（已拍板） |
| [`docs/workbench-real-ui-acceptance-2026-09-11.md`](./docs/workbench-real-ui-acceptance-2026-09-11.md) | 真实 UI 验收记录 |
| [`docs/evidence-first-reading-mainline.md`](./docs/evidence-first-reading-mainline.md) | 证据优先精读主线 |
| [`docs/resume-readiness.md`](./docs/resume-readiness.md) | 收束基线、黄金路径与验收指标 |
| [`CONTEXT.md`](./CONTEXT.md) | 领域词汇表 |
| [`openspec/`](./openspec) | 变更记录（proposal / design / spec / tasks） |

## 目录结构

```text
research_pulse/
  workbench/          # 持久研究工作台运行时（执行内核、工作区存储、HITL、工具调度）
  production/         # 论文精读生产管线（归一化、证据、质量门禁、发布）
  pedagogical/        # 教学化精读：规划、选材、读图、写作、渲染
  traceable_reading/  # 可溯源精读契约与实现
  api/                # FastAPI 应用与运行时工厂
  rag/                # 分块、检索、充分性判定
  topics/             # 每日论文流与主题
frontend/src/         # React + TypeScript：论文笔记视图 + 工作台视图
evals/                # 黄金任务定义、基线评估器
tests/                # 后端测试
docs/                 # 设计与验收文档
```

## License

未声明许可证。如需引用或复用，请先联系作者。
