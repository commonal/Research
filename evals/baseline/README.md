# Research Pulse 基线评估

这里比较两条路径在同一组任务上的差异：

- `baseline`：一次检索 + 一次回答，不保存研究工作区；
- `workbench`：Research Pulse 工作台路径，允许证据累积、用户确认焦点和继续执行。

评估器不读取完整回答来“猜”质量。每个任务的评审结果必须由运行器或人工评审填入 `records.jsonl`，评估器只汇总可复核的计数和耗时。

可以先复制 `records.template.jsonl` 为 `records.jsonl`，再逐行替换两侧的 `pending` 和评审字段。模板不会被当成已完成结果。

## 记录格式

每行一个任务，字段如下：

```json
{
  "task_id": "pdf_selection_question_citation",
  "baseline": {
    "status": "completed",
    "route": "paper",
    "route_correct": true,
    "citation_checks": [{"correct": true}, {"correct": false}],
    "claims_total": 3,
    "claims_supported": 2,
    "focus_continuity": null,
    "recovery_success": null,
    "latency_ms": 1820,
    "usage": {"input_tokens": 1200, "output_tokens": 460}
  },
  "workbench": {
    "status": "completed",
    "route": "paper",
    "route_correct": true,
    "citation_checks": [{"correct": true}, {"correct": true}],
    "claims_total": 3,
    "claims_supported": 3,
    "focus_continuity": null,
    "recovery_success": null,
    "latency_ms": 2450,
    "usage": {"input_tokens": 1600, "output_tokens": 510}
  }
}
```

`status` 可以是 `completed`、`failed`、`blocked` 或 `pending`。`pending` 不会被当成通过；它只表示该任务还没有真实回执。

不要把 API key、完整提示词、完整 provider 响应或未经脱敏的长回答提交到仓库。原始回答可以留在本机的未跟踪目录，提交的 `summary.json` 只包含指标和状态。

## 运行

先启动后端和前端，再按 [`docs/resume-readiness.md`](../../docs/resume-readiness.md) 的任务逐条收集记录。收集完成后运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-baseline-eval.ps1 `
  -Records .\evals\baseline\records.jsonl
```

也可以只运行离线汇总器：

```powershell
.venv\Scripts\python.exe .\evals\baseline\summarize.py `
  --records .\evals\baseline\records.jsonl `
  --output-dir .\evals\baseline\runs\2026-09-14
```

输出包括：

- `summary.json`：机器可读指标；
- `report.md`：不包含完整回答的可读报告。

如果记录不完整，命令会以非零状态退出，并把缺少的任务列出来。不要用 `--ignore-incomplete` 掩盖未完成评估；该选项只适合调试格式。
