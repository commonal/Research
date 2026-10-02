## 1. service 基础

- [ ] 1.1 从 `traceable_reading/cli.py` 抽出 `run_traceable_reading(...) -> receipt`，CLI 行为不变，CLI 测试回归
- [ ] 1.2 实现 `workbench/state.py`：status.json 读写、状态机校验、启动时非终态标 failed
- [ ] 1.3 实现 `workbench/runner.py`：单 worker 串行任务队列（下载→解析→状态推进，失败脱敏落 status.json）

## 2. 搜索与对话

- [ ] 2.1 移植 demo `McpServerConnection` 为 `workbench/mcp.py`（仅 list_tools/call_tool + MCP_SERVERS 解析），连接失败安全降级
- [ ] 2.2 实现极简 harness（工具注册 + 受限循环）与 `workbench/chat.py`：search_paper/read_section 工具、[B#] 引用约定、轮数上限、会话历史按论文持久化
- [ ] 2.3 实现 `workbench/index.py`：content_list blocks 的 BM25 索引（确定性可重建、落盘），search_paper/read_section 基于它实现并单测
- [ ] 2.4 实现选区↔块确定性匹配纯函数（规范化比对 + bbox IoU 兜底 + 未定位降级）并单测

## 3. API

- [ ] 3.1 `POST /api/workbench/search`：MCP 搜索，返回候选列表；未配置/失败返回明确错误
- [ ] 3.2 `POST /api/workbench/papers`（添加候选：下载+解析入队）与 `GET /api/workbench/papers`（列表+状态）
- [ ] 3.3 `POST /api/workbench/papers/{id}/chat`：全文对话（支持选区匹配参数）；`POST /api/workbench/papers/{id}/notes`：触发 traceable 后台任务
- [ ] 3.4 脚本化 API 测试：状态机推进、对话 prompt 组装、失败路径、笔记触发（脚本模型）

## 4. 前端

- [ ] 4.1 工作台面板：右侧候选论文列表（+号添加）与已添加论文列表（状态徽章）
- [ ] 4.2 单视图：左侧会话、右侧 PDF（pdf.js），点选模式移植（pdfBlockSelection.ts + 悬停预览），引用 `[B#]` 高亮回跳
- [ ] 4.3 笔记生成按钮（parsed 状态可用，reading 中转圈，published 跳转知识库）与对话输入框（含选区上下文提示条）
- [ ] 4.4 前端测试：面板渲染、状态徽章、点选回调、按钮状态机

## 5. 验收

- [ ] 5.1 一篇真实论文全路径验收：搜索 → 添加 → 解析完成 → 多轮对话（含点选公式/表格提问）→ 生成笔记 → 知识库时间线可见（source_linked_unverified/rag_eligible=false）
- [ ] 5.2 全量测试回归 + `openspec validate add-paper-workbench --strict` 通过
