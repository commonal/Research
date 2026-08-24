# Design

## Canonical block

`NormalizedBlock` 是解析器无关的中间对象，包含 `block_id`、`kind`、`text`、`latex`、`table_html`、`image_path`、`caption`、`section_path`、`page_start/page_end`、`bbox`、`sources`、`alignment` 和 `parse_status`。字段缺失表示解析边界，不用占位文本补齐。

## Source adapters

- MinerU `source_content_list_v2.json`：逐页读取 paragraph/title/equation/table/image/chart，提取 content、LaTeX、HTML、caption、image path 和 bbox。
- Docling `document.json`：读取 texts、tables、pictures、pages；从 `prov` 取得页码/bbox，从 table grid 取得行列和 cell 文本，从 refs 取得标题/图注。公式使用 `orig` 作为可追溯原始表示，但不宣称已解码。

## Alignment

先限制同 kind、同 page（允许相邻页）候选，再用归一化文本相似度和 bbox IoU 评分，贪心一对一匹配。达到阈值才合并；否则保留 `mineru_only` 或 `docling_only`。合并块优先使用 MinerU 内容，Docling locator/section/cell 结构作为补充，`sources` 永远记录两端 JSON 路径。

## Persistence boundary

转换器支持从服务器 source cache 读取，并写出 `normalized/blocks.jsonl` 与 `normalized/manifest.json`。manifest 记录输入文件 sha256、解析器版本、块计数和对齐计数；不写入原始全文或模型响应。
