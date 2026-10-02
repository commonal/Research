# PROTOTYPE — Harness 选型与最小论文工作流实验

这是可丢弃原型，不是生产实现。它只回答一个问题：

> 对同一篇已解析论文，完整 Agent Harness 是否比固定的“确定性选材 → 一次模型生成”工作流带来足以覆盖复杂度和成本的用户价值？

## 固定实验条件

- 样本：`data/normalized/2608.22767/normalized/blocks.jsonl`
- 论文：*The Retriever Should Remember: Experience-Amortized Reranking for Long-Term Agent Memory*
- 模型：`.env` 中相同的 `DEEPSEEK_MODEL` / `DEEPSEEK_API_KEY`
- 输出：中文可读笔记，包含研究问题、核心直觉、方法、实验、局限、精读建议和真实 block ID 引用
- PDF 解析不属于本实验：两个 arm 读取完全相同的 frozen normalized blocks

## 两个 arm

### `workflow`

程序按标题、摘要和章节关键词确定性选择材料，然后只调用一次模型。它代表“好 Prompt + 固定工作流”。

### `deepagents`

`deepagents==0.7.11` 只获得两个只读工具：`search_paper` 和 `read_blocks`。禁用 general-purpose subagent，并排除写入、编辑、执行和 task 工具。Agent 自己决定检索哪些 blocks。

## 运行

原型依赖被隔离在 `.prototype-deps/deepagents-0.7.11`，不修改正式 requirements。

```powershell
.\.venv\Scripts\python.exe experiments\harness_selection_prototype\run.py --arm workflow
.\.venv\Scripts\python.exe experiments\harness_selection_prototype\run.py --arm deepagents
```

输出写入忽略提交的 `outputs/`：

```text
outputs/<arm>/note.md
outputs/<arm>/receipt.json
```

## 人工比较

盲看两份 `note.md`，分别判断：

1. 是否帮助第一次接触该论文的人理解核心问题和机制；
2. 方法是否讲清楚，而不是复述摘要；
3. 实验结论是否具体且没有越过材料；
4. 局限是否来自论文，而非模板化猜测；
5. block 引用是否真实存在、是否与相邻主张相关；
6. 完整 Harness 增加的调用次数、时长和复杂度是否换来了肉眼可见收益。

若 `deepagents` 没有稳定胜出，产品 V1 采用固定工作流；只有真实失败模式要求自主检索时才引入完整 Harness。
