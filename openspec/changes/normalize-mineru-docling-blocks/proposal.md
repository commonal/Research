# Proposal: normalize MinerU and Docling extraction blocks

将远程缓存的 MinerU 与 Docling 解析产物转换为统一、可追溯的 normalized blocks，供后续精读和证据适配层使用。MinerU 保留公式 LaTeX、表格 HTML、图像路径和按页阅读顺序；Docling 保留章节、页面、bbox、表格 cell 结构和对象锚点。两者按页、类型、bbox 和文本相似度做确定性一对一对齐。

本 change 只建立离线转换和对齐边界，不把原始 PDF/完整解析输出提交到 Git，不改变现有发布、RAG 或质量门禁。原始材料和生成的 normalized 输出作为 source_id 目录下的运行资产保存在服务器大盘，代码、schema、manifest 和小型 fixture 进入仓库。

## Non-goals

- 不实现新的 LLM 精读提示词、VLM 图像描述或公式语义解释。
- 不把未匹配或未解析对象伪装成完整事实，不丢弃单源块。
- 不把大 PDF、完整 MinerU/Docling JSON 或图片复制进 Git。
- 不改变现有 EvidenceBlock 的发布资格和 RAG 投影规则。
