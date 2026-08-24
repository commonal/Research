# schedule-incremental-production 归档摘要

## 用户可见行为

- 研究方向默认启用每日自动更新，用户可暂停、恢复并设置每日最新 1～3 篇。
- 首页展示下一次计划时间、上次成功发现水位以及首次/手动/自动运行标签。
- 每日计划运行按 UTC 水位选择最新论文，不补跑停机或暂停期间的每个历史周期。
- 自动生产复用现有 LangGraph、来源锚点、独立蕴含判断、Markdown 原子发布和 ResearchRAG 索引路径。
- API 新增方向设置更新与安全调度状态读取，不暴露密钥、连接串、原始论文材料或 provider 响应。

## 实施边界

- 当前为单 Uvicorn 进程内的 APScheduler 3.x MVP，不支持分布式调度或持久化 checkpoint 恢复。
- 产品语义是每方向每日最新有限数量的信息流，不承诺穷尽窗口内全部论文。
- Markdown 与 provenance sidecar 继续作为长期知识源真相；PDF 和解析全文不持久化。

## 验证结果

- OpenSpec Apply：16/16 任务完成。
- 后端与 PostgreSQL：83 项测试通过。
- Worker：6 项测试通过。
- React：9 项测试通过，TypeScript/Vite 生产构建通过。
- Markdown 安全检查通过。
- fake 调度集成验证通过：启用方向自动创建计划运行，经同一质量门禁发布并推进水位。
- Specs 同步后 `openspec validate --specs --strict`：9 项通过、0 项失败。
