## 1. 审核草稿领域模型与持久化

- [x] 1.1 定义审核草稿 ID、knowledge ID、状态、版本候选、正文哈希、来源 URL、质量问题和创建时间，并用单元测试覆盖 `needs_review`、`rejected`、`published`、`expired` 的合法状态转换
- [x] 1.2 增加独立审核草稿 Markdown/元数据存储，验证草稿不被正式知识扫描、时间线或 RAG 索引读取，且不保存 PDF、完整解析全文、prompt 或 provider 原始响应
- [x] 1.3 将生产图和重新精读 CLI 的 `needs_review` 回执写入最新审核草稿，验证旧当前 bundle、manifest、processed registry 和 RAG 数据保持不变

## 2. 审核 API 与确认发布

- [x] 2.1 增加待审核草稿列表和详情 DTO/API，验证只返回安全字段、最新草稿优先且不存在时返回明确 404/空状态
- [x] 2.2 增加确认/拒绝接口和幂等状态机，验证旧草稿不可重复操作、拒绝不改变当前版本、未知状态不会泄露路径或原始响应
- [x] 2.3 将确认发布接入重新下载/解析/抽取/质量门/标准发布路径，验证确认不能绕过 facet、锚点、数字和独立蕴含门禁，失败不产生半成品
- [x] 2.4 增加 PostgreSQL/文件系统集成测试，验证通过确认后只生成一个新 current 版本，旧版本保留且 RAG 只投影新版本合格事实

## 3. 前端待审核预览

- [x] 3.1 增加审核草稿 API 类型和论文详情状态，验证当前正式版本优先显示，待审核草稿显示明确徽章和加载/空/错误状态
- [x] 3.2 实现精读预览、质量问题、证据边界、来源链接和新旧版本差异展示，验证待审核正文不被标记为正式知识
- [x] 3.3 实现确认发布、拒绝和刷新交互，验证确认中禁用重复操作、失败保留旧正文、问答入口始终只绑定当前正式版本
- [x] 3.4 运行前端组件测试、`npm run verify:markdown` 和 `npm run build`，验证旧 schema-v2 详情仍可读

## 4. 端到端验收与归档准备

- [x] 4.1 增加真实或受控回执验收：`needs_review` 草稿可预览但不可检索，拒绝保持旧版本，质量通过后确认发布生成新版本；测试不得伪造 passed 回执
- [x] 4.2 运行全量 Python、worker、前端回归、PostgreSQL 集成和敏感内容扫描，记录命令与结果
- [x] 4.3 真实浏览器复验首页、论文详情、待审核预览、确认/拒绝和“问这篇论文”，确认只有发布后版本进入 RAG
- [x] 4.4 同步主 Specs、运行 `openspec validate --all --strict`，完成 ARCHIVE_SUMMARY 后再归档本 change

### 当前验证记录

- 已通过：`python -m unittest discover -s tests -q`（160 个测试，含受控闭环验收）、`python -m unittest discover -s worker/tests -q`（7 个测试）、PostgreSQL 审核草稿集成测试、`npm run test -- --run src/App.test.tsx`（7 个测试）、`npm run verify:markdown`、`npm run build`、`openspec validate --all --strict`。
- 已完成：4.3 的带审核草稿真实浏览器验收，以及 4.4 的主 Specs 同步、严格校验和归档。
