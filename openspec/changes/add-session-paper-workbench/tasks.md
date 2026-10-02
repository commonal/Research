## 1. 固化现有原型与 V0/V1 验收边界

- [x] 1.1 定义 `WorkbenchClient` 与 Session/Paper/Message/Context/Citation DTO，fixture 使用 `papers[]` 和 `active_paper_id`
- [x] 1.2 实现工作台/知识库一级切换、会话侧栏及新建/切换/重命名 fixture 交互
- [x] 1.3 集成本地 PDF.js worker 和真实 PDF，完成全宽/分屏及会话布局恢复
- [x] 1.4 实现 PDF 点选、“询问”和可移除上下文卡片，并覆盖双栏选择测试
- [x] 1.5 实现脚本回答、有效/无效 B#、引用回跳和 unresolved 状态
- [x] 1.6 用模拟 NoteRun 展示进度、失败、成功及预生成真实知识笔记链接，并通过前端测试和构建
- [x] 1.7 对照当前代码重新生成原型回执，消除旧 `prototype-receipt.md` 与 tasks/实现的状态漂移
- [x] 1.8 明确记录 V0 Harness validation 与 V1 Product validation 的独立清单，禁止以任一回执替代另一层
- [x] 1.9 在真实浏览器用不同版式内容完成现有原型旅程，记录实际用户摩擦并修复；用户于 2026-08-31 明确确认原型通过验收，可进入生产接线

## 2. 领域合同、SQLite 与独立状态机

- [x] 2.1 先为公开 Session service seam 添加失败测试，覆盖稳定 ID、问题入口、论文入口、标题、归档/恢复和非级联删除，再做最小实现
- [x] 2.2 定义 `ResearchSession`、`Paper`、`SessionPaperLink`、`Message`、`ExplorationRun`、`CandidateFinding`、`NoteRun` 及状态类型，并测试非法转换和 V1 第二篇限制
- [x] 2.3 建立 sessions、papers、session_papers、messages、context/citation refs、exploration runs/events/findings、note_runs 的 SQLite schema/repository
- [x] 2.4 用内存 SQLite 测试外键、去重、事务回滚、稳定 Run 下不可变 Attempt 历史和跨会话隔离；生产启用 WAL、短事务与可配置路径
- [x] 2.5 实现启动恢复：租约过期 Attempt 进入 abandoned/明确可恢复终态，完整材料可经完整性检查恢复 ready，且各状态机互不污染

## 3. Session API 与安全论文准备

- [x] 3.1 为会话创建、列表、详情、问题更新、重命名、布局、归档/恢复和删除编写 FastAPI 合同测试并实现薄路由
- [x] 3.2 把 workbench repository 与生命周期组合进现有 app，验证知识库、topic、scheduler 和单 Uvicorn 约束不回归
- [x] 3.3 实现本地 PDF 魔数/大小/SHA-256/原子提交，并测试非 PDF、截断、超限和重复文件
- [x] 3.4 实现 arXiv/公开 PDF URL 归一化与安全下载，测试 SSRF、危险重定向、超时、超限和非 PDF 拒绝
- [x] 3.5 实现以 PDF SHA-256 为 `paper_id`、`paper_sources` 保存 arXiv/URL 别名的共享 Paper 与准备队列，接入既有 MinerU/Layer C；证明 PDF ready 后立即可读，解析 ready 前相关能力诚实禁用
- [x] 3.6 实现论文关联、移除、重试、PDF Range 和块 locator API；测试路径穿越、绝对路径隐藏、V1 单论文和 detached 引用

## 4. 固定范围问答路径

- [x] 4.1 先为公开选区映射函数添加失败测试，覆盖空白、断词、ligature、同页文本、bbox、双栏和 unresolved，再实现确定性匹配
- [x] 4.2 实现 `scope=none|selection|section|full` resolver，验证不暗中扩大范围并记录实际块、章节和截断信息
- [x] 4.3 实现模型感知 token 预算与最近历史装配，验证旧历史仍持久可见、不调用摘要模型且完整 prompt/论文正文不落库
- [x] 4.4 实现一次调用 `ChatAssembler` 与消息 API，测试空会话无论文标记、论文范围 chips、生成失败和刷新恢复
- [x] 4.5 实现引用 allowlist 与 locator 投影，验证只有本轮实际发送的完整块 ID 可回跳，缩写/无效/未发送/detached 均 unresolved
- [x] 4.6 将生产 HTTP client 接到固定范围问答，验证生产错误绝不静默回退 fixture

## 5. V0 Harness 选型与最小工作流验证

- [x] 5.1 用一组固定研究题、固定论文材料和统一预算定义 V0 实验夹具，记录模型、Harness 版本、工具清单、配置和可重复运行命令
- [x] 5.2 定义 attempt-scoped `AgentKernel`、安全事件 DTO 与 adapter 合同测试，确保产品领域不依赖 Harness 专有消息/checkpoint 类型
- [x] 5.3 实现只读工具 facade：来源搜索、论文元数据、受管块读取、只读状态；测试拒绝任意路径、写入、执行、todo、发布和 subagent
- [x] 5.4 实现启动前 capability allowlist preflight，并用故意加入禁用工具的测试证明 fail closed 而非仅靠提示词
- [x] 5.5 在 adapter 外实现模型轮次、工具调用、读块、墙钟和 token 多维硬预算，以及用户取消和 `budget_exhausted` 终态
- [x] 5.6 运行 V0 实验，验证真实工具调用、停止、超预算、外部失败、部分结果、重试、事件可观察和实际读取块引用闭环
- [x] 5.7 根据 V0 数据锁定首个 adapter 版本与默认预算；锁定 Deep Agents 0.7.11、StateBackend、无原始 checkpoint、四工具 effective inventory 与 8/16/24/300/40000/8000 默认预算

## 6. ExplorationRun 产品接入

- [x] 6.1 先为 ResearchRunCoordinator 公开 seam 添加失败测试，再实现创建、读取、Attempt-scoped 取消和继续；继续保留 run_id、创建新 attempt，旧 attempt 不得被覆盖，且校验 expected_attempt_id 与幂等键
- [x] 6.2 持久化 append-only 安全事件并实现流式投影，测试刷新恢复、先持久化后推送及敏感字段过滤
- [x] 6.3 接入 `scope=explore` 路由，保证普通 scope 永不隐式升级为 Harness，explore 始终创建独立运行
- [x] 6.4 实现 `source_authority` 与 `representation_preference` 两个独立字段和排序合同，测试易读二手表示不冒充更权威来源
- [x] 6.5 实现 CandidateFinding、实际读取块 allowlist 和引用解析；未读取、缩写不唯一或无效引用保持 unresolved
- [x] 6.6 实现可选 `candidate_questions` 与“当前证据范围内的初始地图”展示，测试空候选问题仍是合法 completed 结果
- [x] 6.7 在前端实现探索运行卡、阶段、来源、预算、停止、部分结果、失败与重试，并明确持续显示草稿/待验证标签
- [x] 6.8 用两个会话添加同一论文，通过完整 API 验证下载/解析只执行一次且消息、布局和 ExplorationRun 完全隔离

## 7. 固定正式笔记路径

- [x] 7.1 从 traceable CLI 抽取窄 service seam，并先增加 CLI 成功、失败回执、退出码和脱敏行为回归测试
- [x] 7.2 实现独立 NoteRun repository/runner，测试 queued/generating/published/failed/retry 不修改 Paper、Message 或 ExplorationRun
- [x] 7.3 接入固定两阶段流程：确定性覆盖优先，只在证据门禁发现明确缺口时允许受限补读
- [x] 7.4 测试 messages、探索草稿和 CandidateFinding 不作为事实材料传入；候选 claim 被采用前必须重新读取原始块
- [x] 7.5 实现 NoteRun API、进度卡和成功后的版本化知识库链接；失败不清空 PDF、聊天、探索或旧笔记
- [x] 7.6 验证既有 provenance、evidence_level、rag_eligible、发布回执和 Markdown 安全合同不变

## 8. V1 生产前端与浏览器验收

- [x] 8.1 接线真实会话、问题优先/论文优先入口、上传/URL/拖放、准备状态、重试和布局恢复
- [x] 8.2 接线五种 scope、固定消息卡、探索运行卡、引用回跳、unresolved/detached、NoteRun 和知识库链接
- [x] 8.3 用真实浏览器验证问题优先旅程：“研究问题 → 探索 → 候选来源 → 添加论文 → 原文回跳 → 固定笔记 → 知识库”
- [x] 8.4 用真实浏览器验证论文优先旅程：“上传 PDF → 立即阅读 → 等待解析 → 选区/章节/全文问答 → 引用回跳 → 固定笔记”
- [x] 8.5 注入解析失败、Harness 失败/取消/预算耗尽、无候选问题和 NoteRun 失败，验证状态诚实、部分结果、重试及能力隔离
- [ ] 8.6 邀请至少一名未受指导用户完成黄金旅程，记录理解偏差与交互摩擦并修复阻断问题
- [ ] 8.7 运行相关后端全量测试、前端全量测试/构建、Markdown 安全校验和严格 OpenSpec 校验，保存 V0 与 V1 两份独立回执
