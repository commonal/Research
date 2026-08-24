# Proposal: integrate normalized extraction into paper reading

将已完成的 MinerU + Docling normalized blocks 接入现有 `SourceMaterial` 和 evidence 质量门，作为深度精读的可选输入。这样精读读取的是包含公式、表格、图表和双源 provenance 的统一块，而不是只读取 Docling Markdown。

本 change 不改变产品边界、不新增 RAG、不改变质量门，也不把未解析公式或单源块伪装成已验证事实。旧 Docling parser 保留兼容；normalized parser 通过显式配置启用。

## Non-goals

- 不实现 VLM 图像描述或公式语义解释。
- 不修改现有深度阅读提示词的产品结构。
- 不把服务器原始 PDF/完整 JSON 复制到仓库或数据库。
- 不降低表格、公式、facet、数字和独立蕴含质量门。
