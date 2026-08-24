# research-worker

独立于 AnythingLLM 的研究情报生产模块。

当前已实现：从 arXiv Atom API 获取候选论文，标准化为本地 JSON。

```powershell
python -m worker.main discover --subscription subscriptions/llm-agent-memory.json
```

输出中包含标题、作者、发布时间、摘要、arXiv 链接和分类；不会下载或保存论文原文。

也已实现 `analyze-abstract`：使用 DeepSeek JSON Output 仅基于标题和摘要生成 Markdown 草稿。它会在产物中明确标记 `analysis_level: abstract_only`，并且固定写出“图表与公式未分析”。

后续阶段：

```text
候选论文 -> 摘要级 Markdown -> 临时全文解析 -> 图表/公式分析 -> AnythingLLM Developer API 发布
```

任何 API Key 都通过本机环境变量或 `.env` 提供，不进入版本控制。
