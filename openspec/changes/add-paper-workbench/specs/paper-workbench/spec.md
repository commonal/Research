# paper-workbench 能力规格

## ADDED Requirements

### Requirement: 工作台必须通过 MCP 提供论文候选搜索

系统 SHALL 提供 MCP 搜索端点，通过 `MCP_SERVERS` 配置的 stdio MCP server 检索论文候选，且 SHALL 把候选作为右侧面板数据返回，不影响主视图。MCP server 连接失败或超时 SHALL 降级为明确错误消息，不得影响工作台其他功能。

#### Scenario: 搜索返回候选论文
- **GIVEN** 已配置可用的 MCP 论文检索 server
- **WHEN** 用户提交检索词
- **THEN** 系统返回候选论文列表（标题、作者、年份、摘要、来源标识）供右侧面板展示
- **THEN** 候选仅作为可选项存在，不触发下载或解析

#### Scenario: MCP server 不可用
- **GIVEN** MCP server 未配置、连接失败或超时
- **WHEN** 用户提交检索词
- **THEN** 系统返回明确的搜索错误消息
- **THEN** 上传、对话与笔记生成等其他工作台功能不受影响

### Requirement: 添加候选必须触发受管的后台下载与 MinerU 解析

系统 SHALL 在用户对候选论文点击添加时进入后台任务：下载 PDF → 调用 `MinerUApiParser` 解析 → 材料包落 `data/mineru/<source_id>/material`。任务状态 SHALL 通过列表端点暴露 `uploading|parsing|parsed|reading|published|failed`，进程重启后非终态任务 SHALL 诚实标记为 failed，不得伪造成功。

#### Scenario: 候选论文添加成功
- **GIVEN** 用户在候选面板点击某篇论文的添加按钮
- **WHEN** 系统创建后台任务
- **THEN** 状态沿 `uploading → parsing → parsed` 推进，材料包完整落盘（full.md、content_list.json、images）
- **THEN** 该论文出现在工作台"已添加论文"列表中，可打开

#### Scenario: 下载或解析失败
- **GIVEN** PDF 下载失败、MinerU 解析失败或材料包不完整
- **WHEN** 后台任务执行
- **THEN** 状态标记 `failed` 并保留具体错误消息（脱敏）
- **THEN** 系统不得伪造 `parsed` 状态或部分材料包

### Requirement: 对话必须通过检索工具按需获取论文内容

系统 SHALL 为每篇已解析论文建立确定性、可重建的块级检索索引（BM25/关键词，作用于 content_list blocks）。对话 SHALL 通过极简 harness 的工具按需拉取内容（`search_paper(query)` 返回 top-k 块、`read_section(section_id)` 返回章节有序块），工具轮数 SHALL 有上限；回答按约定带 `[B#]` 块引用；检索无结果 SHALL 诚实说明。系统 MUST NOT 建向量库，MUST NOT 引入查询改写模型或全文直塞。

#### Scenario: 单跳问题一次检索命中
- **GIVEN** 某论文已处于 `parsed` 状态
- **WHEN** 用户提问且块级检索命中相关内容
- **THEN** harness 拉取相关块后生成回答，回答引用块编号
- **THEN** 单次交互内输入上下文只包含被拉取的块，不包含论文全文

#### Scenario: 复杂问题在轮数上限内多次拉取
- **GIVEN** 用户问题需要论文中多处内容才能回答
- **WHEN** 首次检索结果不足以回答
- **THEN** harness 在轮数上限内追加检索或读取章节后再回答
- **THEN** 超过轮数上限时基于已获得的内容回答并说明局限

#### Scenario: 检索无结果时诚实回答
- **GIVEN** 用户问题在论文中没有依据
- **WHEN** 块级检索无命中或命中内容不相关
- **THEN** 回答明确说明论文中未找到相关内容
- **THEN** 系统不得编造论文内容或伪造块引用

#### Scenario: 索引可从材料包确定性重建
- **GIVEN** 同一材料包重新装载或服务重启
- **WHEN** 系统重建检索索引
- **THEN** 块 ID、页码与位置映射与 content_list 一致，不发生漂移
- **THEN** 重建过程不调用任何模型

### Requirement: 选区提问必须确定性定位到材料块

系统 SHALL 支持在 PDF 视图点选文本块后提问：前端点选产出选区（文本+页码+bbox），后端 SHALL 用确定性字符串匹配（规范化比对，bbox 同页重叠兜底）把选区映射到 content_list 块，并把选区原文、块类型、所在章节与邻块上下文组装进提问消息。回答引用的块 SHALL 可用于前端高亮回跳。

#### Scenario: 点选公式块提问
- **GIVEN** 用户点选第 3 页的一个公式区域并提问
- **WHEN** 系统组装提问 prompt
- **THEN** 用户消息包含选区原文、块类型（公式/LaTeX）、所在章节与邻块文本
- **THEN** 模型回答围绕该块展开并引用块编号

#### Scenario: 选区无法匹配到块
- **GIVEN** 点选文本因解析差异无法精确匹配 content_list 块
- **WHEN** 系统执行匹配
- **THEN** 系统按同页 bbox 重叠兜底定位
- **THEN** 仍无法定位时以"未定位块"降级组装，提问不失败

### Requirement: 笔记生成必须触发完整可追溯阅读管线

系统 SHALL 在用户点击笔记生成按钮后触发 `run_traceable_reading`（traceable 管线 service 入口）后台运行，发布后笔记进入知识库时间线。产物边界保持 `published/source_linked_unverified/rag_eligible=false`；任务状态可见，失败时保留脱敏错误。

#### Scenario: 一键生成笔记
- **GIVEN** 某论文已处于 `parsed` 状态
- **WHEN** 用户点击"生成笔记"
- **THEN** 系统后台执行完整 traceable 管线并推进 `reading → published` 状态
- **THEN** 发布的笔记出现在知识库时间线，保持 `source_linked_unverified/rag_eligible=false`

#### Scenario: 阅读管线失败
- **GIVEN** 管线执行中模型调用失败或材料无效
- **WHEN** 后台任务执行
- **THEN** 状态标记 `failed` 并保留脱敏错误消息
- **THEN** 已发布的旧笔记与知识库时间线不受影响
