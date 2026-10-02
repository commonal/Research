# Workbench Harness V0 固定实验夹具

本目录只冻结 V0 技术验证的输入和运行约束，不代表 V1 产品验收通过，也不把探索结果发布到知识库。

固定条件：

- Harness：`deepagents==0.7.11`
- 模型：`deepseek:deepseek-v4-flash`，temperature 0
- 材料：三份受管 normalized blocks，以 `manifest.json` 中的 SHA-256 校验
- 搜索范围：固定材料目录，不访问可变 Web 搜索
- 工具：来源搜索、论文元数据、受管块读取、只读运行状态
- 预算：模型轮次、工具调用、读块、墙钟、输入 token、输出 token 六维统一限制
- 运行隔离：每个问题单独进程，避免 Harness profile 全局状态污染

完整 parser 输出仍归 Layer C/受管材料目录所有，本夹具不复制论文正文。缺少材料或哈希变化时 preflight 必须失败，不能在线重新下载后继续。

## 可重复校验命令

在仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe -m research_pulse.workbench.harness_v0_fixture --preflight
```

## V0 结论

Deep Agents 0.7.11 已通过 V0。首轮把静态编译图库存误当成模型运行时可见库存，因此产生 blocker；修正后的门禁用 Recording Model 检查 middleware 处理后的 effective tool inventory，并同时锁定不支持 shell 或主机文件系统的 `StateBackend`、关闭原始 checkpoint。模型最终只看到四个领域只读工具。

复现实验与门禁：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_workbench_deepagents_v0 tests.test_workbench_capability_preflight tests.test_workbench_budget_enforcer -v
```

真实三题回执为 `receipts/v0-live-latest.json`，验收摘要为 `receipts/v0-acceptance-2026-09-01.md`。旧 `v0-blocker-2026-09-01.md` 保留为被后续实验取代的历史 attempt。
