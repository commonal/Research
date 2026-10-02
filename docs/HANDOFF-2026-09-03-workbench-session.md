# HANDOFF — Workbench / 研究助理 会话交接(2026-09-03 21:45)

> 本会话做了大批「文献检索 + 断点续跑 + 证据治理」改动(**全部未提交**,见 `git status`),遗留 3 个已诊断 bug 待修(方案已备,**均等用户批准,未动手**)。后端此刻 **8001 DOWN**(托管进程屡被外部 SIGTERM,最后一次被杀)。下一会话第一步:重启后端、读本文件、找用户确认「待批清单」。

## 0. 一句话状态

代码侧大改已完成并测试(后端 workbench 系 59 passed、前端 40 passed、build 通过);产品侧三处不满(续跑冻结/标题数字/研究地图被顶掉)已只读定位根因;修复方案待批准。

## 1. 本会话钉死的设计(勿回退,均有测试)

1. **Tier-0 显式检索意图 override**(`scope_resolver.py`):`RetrievalIntent`(local_only/expand_external/unspecified)→ EXPAND 即使有锚论文也进 `research_synthesis` 且保留锚点;UNSPECIFIED 走原 UI context。示例:「搜索近两年的 DPO 相关工作」→ synthesis(之前全被 paper_local 吞掉)。
2. **引用 tokenizer 对齐**(`WorkbenchApp.tsx` `tokenizeCitations` + 4 处提示词):识别 `text:/figure:`、`normalized:<64hex>:(text|figure):<12hex>`、裸 12hex 缩写;括号内多 id 拆分去重成上标;`[16]` 等普通文本不动。提示词强制"完整 id、每 id 单独括号、禁缩写合并编造"。
3. **搜索纪律**:单 run `search_arxiv` 硬上限 4 次(第 5 次起返回"停止检索开始总结");空 query 给格式提示不白烧;检索后**候选持久化**:`SafeAgentEvent.extras`(arxiv_id/title/abstract_preview≤400/categories/published/**relevance**)经 sqlite payload 落库。
4. **相关度**:`research_tools.search_arxiv` 按 query 词 ∩ (title×2 + abstract) 算 0~1,排序降序;事件 stable_ids 带 `relevance` → 来源行 chip 显示「· 相关度 xx%」。
5. **断点续跑 = 同一个 run**(`exploration.py retry()` 语义):budget_exhausted/failed → **同 run_id 续跑**(预算清空、事件序号续接容差 append、resume digest 注入 context);**仅 cancelled 开新 attempt**。digest 含:检索次数/读过块数/候选清单+摘要+相关度,规则"不重复搜、候选由用户加"。
6. **证据治理(用户强烈主张,勿再放开)**:agent **永不自行下载/解析论文**——`runtime.py` importer=None,工具面无 `import_supporting_paper`;候选一律由用户点「＋」才走 `/papers/url`(FastAPI BackgroundTasks 下载→排队→解析),标题随请求存入。
7. **论文标题**:`papers.title` 列(create_schema 迁移表加 `"papers": {"title": "TEXT"}`)+ Paper 模型 + register_pdf(title)/submission/api payload/前端 paperFromApi 全链。
8. **解析排空修复**(此前 import 永远卡 queued):`kick_parse_drain()` 后台守护线程循环 `run_next()`,启动时 `requeue_interrupted_parses()` 恢复遗留 queued/parsing 行并排空。
9. **网络健壮性**:arxiv MCP roundtrip `asyncio.wait_for` 60s;网络类工具 `_call_network_tool` 看门狗 ≤150s、cancel 可中断;`ArxivMCPClient(port=free_port())` 防残留进程占死端口。
10. **删除语义 Ownership**(此前完成并记录在 skill reference):workspace/session 删除接磁盘清理;三条架构约束 + 总原则 "Ownership 决定物理生命周期;Association 决定作用域;Provenance 决定删除是否安全"。

## 2. 已诊断待修 bug(方案待批准,勿自作主张)

### ① 续跑静默冻结(最严重)
- 现象:run 失败(budget_exhausted / WorkspaceGateError)→ 用户点继续 → 同 run 追加 `run_started` 后**零事件零异常 ≥3 分钟**,DB 状态 running(attempt 12、13 同款,证据见 exploration_events)。
- 卡点范围:run_started 之后、首个模型/工具事件之前(预检探针 preflight / 恢复后首次 DeepSeek 调用 / FilesystemBackend-TODO 恢复 三者之一)。
- 已备方案(等批):A1=加阶段日志+整段 wall 兜底,复现一次钉死(约 30min);A2=只加兜底(卡死→预算耗尽可再续,约 15min)。

### ② 论文显示数字非标题
- 证据:`papers.title` 列在,但 18 篇全 NULL(右侧那批是 agent 时代下载、从未带标题;列后加无回填)。API 返回 `title:null` → 前端回退 url 尾段。
- 无需代码的解法:**对同一篇重新点一次「＋」**(同 sha 复用行,upsert 会 UPDATE 回填标题)。批量回填(B1, export API 逐篇抓,需限速,约 20min)待批。
- 附:右栏 header 对非 fixture 恒显示"论文上下文",论文列表 item 才逐个显示标题。

### ③ 研究地图/状态被论文面板"顶掉"
- 原因:`WorkbenchApp.tsx` 曾经在 `.workbench-paper-pane` 与 `.workbench-overview-rail` 之间做右栏单槽互斥，未打开论文时右侧出现空白。
- 当前修复:未打开论文且未进入文档视图时，`.workbench-session-view.with-overview` 持久显示研究概览；会话内容与概览独立滚动。打开论文时右侧切换为论文阅读面板，打开研究文档时主区域切换为文档视图。

## 3. 环境事实(重要)

- **arXiv**:`export.arxiv.org/api` 可达(HTTP 200,直连/代理都通);`arxiv.org/pdf` 被 GFW **不稳定重置**(HTTP 200 后 RST/SSL EOF,时通时断)→ 搜索元数据 OK、PDF 下载不可靠。导入失败已改为不崩 run + 明确报错。
- **代理**:FlClash HTTP 7890(env 已设);uvx arxiv-mcp-server 首启可能慢。
- 解析器 = MinerU API 远程(`MINERU_API_TOKEN` 在 .env),单篇分钟级;drain 串行。
- 模型:DeepSeek v4-flash(慢、偶发工具调用畸形,如空 query)。
- **后端托管**:本会话用 Hermes background uvicorn(`--no-reload`)托管 8001,**已被外部 SIGTERM 杀死 ≥4 次**(uptime 4~29min 不等,来源不明,疑似有其他会话/脚本在管 8001)。改代码后必须重启才生效。重启命令:`./.venv/Scripts/python.exe -m uvicorn research_pulse.api.runtime:create_runtime_app --factory --host 127.0.0.1 --port 8001`(workdir=项目根)。
- 前端 5173 dev(热更新,pid 41948);`npm run build` 通过。

## 4. 数据与关键表

- DB:`data/workbench/workbench.sqlite3`(WAL);workspace 目录 `data/workbench/workspaces/<id>/{sessions,materials}`,会话状态 `sessions/<sid>/{evidence,findings,notes}`。
- `exploration_events.payload_json` = {summary, stable_ids, counters, **extras**}。`exploration_runs`: status/attempt/previous_run_id/config_snapshot(**可含 resume_context**)/budgets/final_draft。`papers`: title 列已加。
- 恢复遗留:重启即 `requeue_interrupted_parses()` + `kick_parse_drain()`。

## 5. 给下一会话的执行建议

1. 重启 8001 后端 → `curl /api/workbench/sessions` 200 确认;
2. 把本文件第 2 节清单递给用户勾选(A1/A2/B1/C1/C2),**不要自作主张开修**;
3. 若用户要验证:触发一次搜索 run 看事件里 `source_discovered` 的 relevance;对某篇重加「＋」看标题回填;budget 耗尽后点「继续」观察是否仍冻结(复现①用);
4. 旧 11 篇误下载论文处置(保留/删除)也问一下用户。

## 6. 血泪教训

- `.hermes/*` 是受保护 agent-instruction 文件,直接写入会触发审批/被拦 → **交接类文档放 `docs/`**。
- 用户反感"不问就自主修 + 反复重启";每改动前先确认范围。诊断可只读,修复要批准。
- 多文件补丁用 execute_code 串行 patch;含 `(` 的 grep pattern 会被工具吃成 `/`,先拆词。
- sqlite.py 里 exploration 与 note 事件结构雷同,补丁要带函数签名消歧。
