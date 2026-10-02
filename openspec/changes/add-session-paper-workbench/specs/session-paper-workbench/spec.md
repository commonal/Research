## Purpose

提供一个以研究问题和持久会话为入口的论文研究工作台：用户既能用固定流程阅读论文、生成正式笔记，也能在明确预算与证据边界内启动 Harness 探索；探索草稿与正式知识资产严格分离。

## ADDED Requirements

### Requirement: 产品必须提供以研究问题为中心的持久会话

系统 SHALL 以 `ResearchSession` 作为工作台顶层对象，持久化研究问题、消息、论文关联、运行记录和布局状态。系统 SHALL 同时支持先提出问题再寻找论文，以及先添加论文再提出问题；新建会话 SHALL 立即获得稳定 `session_id`，并支持重命名、归档、恢复和永久删除。

#### Scenario: 从研究问题开始
- **GIVEN** 用户位于工作台且没有添加论文
- **WHEN** 用户创建会话并提交研究问题
- **THEN** 系统保存问题并允许继续普通草稿对话或启动探索
- **THEN** 系统明确显示当前尚无论文上下文

#### Scenario: 从论文开始并恢复会话
- **GIVEN** 用户创建会话并先添加论文
- **WHEN** 用户刷新页面或切换会话后返回
- **THEN** 系统恢复该会话的论文、消息、运行状态和最近布局
- **THEN** 其他会话的数据不得混入

### Requirement: 会话必须安全关联可复用论文材料

系统 SHALL 支持本地 PDF、arXiv URL 和经安全验证的公开 PDF 直链。`paper_id` SHALL 使用完整 PDF 内容 SHA-256；arXiv ID 和规范化 URL SHALL 作为来源别名保存，使相同内容通过不同入口进入时仍复用同一 Paper。多个会话 SHALL 复用 Layer C 原始 PDF 与解析材料，但保持消息和研究运行隔离。领域和 API SHALL 使用论文关联列表；V1 界面 SHALL 最多开放一篇活动论文。

#### Scenario: 添加并复用同一论文
- **GIVEN** 一篇论文已在 Layer C 完成获取与解析
- **WHEN** 用户在另一会话添加相同论文
- **THEN** 系统复用既有材料而不重复下载或解析
- **THEN** 新会话不得获得原会话的消息、上下文或运行结果

#### Scenario: 上传与 URL 指向相同内容
- **GIVEN** 用户先上传一份 PDF，随后通过 arXiv 或公开 URL 添加字节内容相同的 PDF
- **WHEN** 系统完成远程内容校验与哈希
- **THEN** 两个入口解析为相同 `paper_id`，下载和解析材料只保留一份
- **THEN** arXiv ID 与规范化 URL 作为来源别名保留而不替换内容身份

#### Scenario: 拒绝不安全来源
- **GIVEN** 用户提交登录墙、普通网页、私网地址、危险重定向或无法验证的非 PDF 内容
- **WHEN** 系统校验来源
- **THEN** 系统拒绝添加并返回不含密钥或服务器绝对路径的错误
- **THEN** 当前会话和既有材料保持不变

### Requirement: 论文准备状态必须诚实门控阅读能力

系统 SHALL 分别暴露 PDF 获取和结构化解析状态。PDF 获取成功后用户 SHALL 能立即查看原始 PDF；依赖结构化材料的选区映射、论文范围问答、探索读块和正式笔记 SHALL 在解析完成前禁用。解析失败 SHALL 保留 PDF 阅读、失败阶段、脱敏错误和重试入口。

#### Scenario: PDF 已可读但仍在解析
- **GIVEN** PDF 已完整落盘且解析仍在进行
- **WHEN** 用户打开论文
- **THEN** 系统显示真实 PDF 与解析进度
- **THEN** 依赖解析材料的操作保持禁用并解释原因

#### Scenario: 解析失败后恢复
- **GIVEN** PDF 可读但解析失败
- **WHEN** 用户查看状态并选择重试
- **THEN** 系统保留 PDF、会话和既有运行记录并重新启动解析
- **THEN** 不得把失败状态伪装为材料已就绪

### Requirement: 每轮对话必须使用显式研究范围

系统 SHALL 为每轮请求记录 `scope=none|selection|section|full|explore`。`none` 不得使用论文材料；`selection`、`section` 和 `full` SHALL 通过固定上下文装配流程生成一次草稿回答；只有 `explore` SHALL 启动 Harness 自主探索。系统不得在未告知用户时扩大范围。

#### Scenario: 使用固定论文范围提问
- **GIVEN** 当前论文已完成解析
- **WHEN** 用户选择选区、章节或全文范围并发送问题
- **THEN** 系统仅装配该显式范围与预算内最近历史
- **THEN** 回答永久显示实际使用的论文、范围和截断状态

#### Scenario: 启动探索
- **GIVEN** 用户明确选择探索模式并提交研究问题
- **WHEN** 系统创建运行
- **THEN** 系统创建独立 `ExplorationRun` 而非伪装为普通单轮回答
- **THEN** 界面显示探索进度、预算和停止操作

### Requirement: Harness 探索必须受只读工具和预算约束

每个 `Attempt` SHALL 通过 attempt-scoped `AgentKernel` 调用现有 Harness，仅暴露来源搜索、论文元数据读取、受管论文块读取和只读状态查询。系统 SHALL 禁止文件写入、代码或命令执行、任务清单写入、发布和子代理委派，并限制模型轮次、工具调用、读块数量、墙钟时间和可配置 token 预算。

#### Scenario: 探索正常完成
- **GIVEN** 探索运行具有未耗尽的预算
- **WHEN** Harness 搜索来源并读取材料
- **THEN** 系统持久化安全的结构化事件和实际读取块引用
- **THEN** 最终输出标识为探索草稿并报告预算使用情况

#### Scenario: 停止或耗尽预算
- **GIVEN** 探索正在运行
- **WHEN** 用户停止运行或任一硬预算耗尽
- **THEN** 系统停止继续调用工具并保存 `cancelled` 或 `budget_exhausted` 状态
- **THEN** 已取得的部分结果可查看但不得标记为完整研究结论

#### Scenario: Harness 能力越界
- **GIVEN** 底层 Harness 暴露了未获批准的写入、执行或委派能力
- **WHEN** adapter 启动前执行能力清单检查
- **THEN** 系统拒绝启动该运行并给出脱敏配置错误
- **THEN** 不得依靠提示词代替能力隔离

### Requirement: 探索运行必须可观察、可恢复且可重试

系统 SHALL 将一条探索意图持久化为稳定的 `ExplorationRun`，并将每次物理执行持久化为隶属于该 Run 的不可变 `Attempt`。输入快照、执行状态、预算计数、安全事件、工具结果和终态原因 SHALL 按 Attempt 记录；候选来源、实际读取块和最终草稿 SHALL 可按 Run 投影。进程重启后，租约过期的运行中 Attempt SHALL 进入 `abandoned` 或明确的可恢复终态，不得把终态 Attempt 重新排队。预算耗尽、可重试失败、取消后重启及显式继续 SHALL 保留原 `run_id`，并创建具有新 `attempt_id` 的下一 Attempt。

#### Scenario: 流式观察运行
- **GIVEN** 探索正在执行
- **WHEN** Harness 开始阶段、工具或来源读取
- **THEN** 系统流式发送不含密钥、内部推理或完整隐藏 prompt 的事件
- **THEN** 刷新后用户仍能从持久记录恢复已完成事件和当前状态

#### Scenario: 失败后重试
- **GIVEN** 探索因外部服务或进程中断而失败
- **WHEN** 用户点击重试
- **THEN** 请求携带调用方最后观察到的 `expected_attempt_id` 与幂等键，系统原子校验后在同一 `run_id` 下创建新 Attempt
- **THEN** 原 Attempt 的状态、事件、预算和部分结果保持不可变且可审计
- **THEN** 相同幂等键的重复请求至多创建一个新 Attempt，陈旧 `expected_attempt_id` 返回冲突

### Requirement: 引用必须来自本轮实际读取的证据块

草稿回答和探索结果中的有效引用 SHALL 只解析到本轮实际发送或实际读取的受管块。不存在、缩写不唯一、已移除、未读取或无法映射的块标识 SHALL 显示 `unresolved`，不得自动替换为相似块。有效引用 SHALL 可回跳并高亮 PDF 位置。

#### Scenario: 校验有效引用
- **GIVEN** 输出引用了属于本轮实际上下文的完整稳定块标识
- **WHEN** 用户点击引用
- **THEN** 系统打开对应 PDF 页面并高亮可定位区域

#### Scenario: 拒绝伪引用
- **GIVEN** 输出引用了未读取块、无效块或无法唯一解析的缩写标识
- **WHEN** 系统渲染输出
- **THEN** 正文可保留但引用标为 `unresolved`
- **THEN** 系统不得把它计为证据或自动补配相似来源

### Requirement: 探索产物不得自动成为正式知识

系统 SHALL 将研究问题、探索计划、候选来源、`CandidateFinding` 和最终探索回答标识为草稿或待验证对象。`candidate_questions` SHALL 为 optional；没有候选问题 SHALL 仍可构成合法成功结果。探索生成的“方法路线地图” SHALL 明确标识为“当前证据范围内的初始地图”。

#### Scenario: 探索没有候选问题
- **GIVEN** 探索已完成且没有产生可靠的后续候选问题
- **WHEN** 系统保存结果
- **THEN** 运行仍可为 `completed`
- **THEN** 输出明确显示候选问题为空而不伪造建议

#### Scenario: 展示方法路线地图
- **GIVEN** 探索归纳了若干方法路线
- **WHEN** 用户查看路线地图
- **THEN** 标题或说明明确限定为当前证据范围内的初始地图
- **THEN** 未验证候选发现不得进入知识库或 RAG

### Requirement: 正式笔记必须通过固定流程独立生产

系统 SHALL 通过独立 `NoteRun` 从受管论文材料触发现有可靠阅读管线。聊天消息、探索回答和 `CandidateFinding` 不得作为正式事实来源；它们至多可作为待验证问题输入，管线 SHALL 重新读取原始材料。笔记状态 SHALL 与论文准备、普通消息和探索运行独立。

#### Scenario: 生成正式笔记
- **GIVEN** 当前论文解析材料完整
- **WHEN** 用户点击生成正式笔记
- **THEN** 系统从受管材料启动固定 NoteRun 并显示独立进度
- **THEN** 管线不把聊天或探索文本当作证据内容

#### Scenario: 笔记成功或失败
- **GIVEN** NoteRun 已结束
- **WHEN** 运行成功
- **THEN** 会话显示可点击的版本化知识库链接并保持当前页面
- **WHEN** 运行失败
- **THEN** PDF、消息、探索结果和既有笔记仍可用，并提供脱敏错误与重试入口

### Requirement: 工作台必须保持可逆分屏和独立生命周期

系统 SHALL 默认全宽显示对话，打开论文时切换为左侧对话、右侧 PDF 的分屏，关闭后恢复全宽，并按会话恢复最近布局。移除论文 SHALL 保留历史消息、运行和引用记录，但引用 SHALL 显示论文已移除；删除会话不得删除共享 Layer C 材料或已发布知识资产。

#### Scenario: 切换会话与布局
- **GIVEN** 两个会话具有不同论文、消息和面板状态
- **WHEN** 用户在历史侧栏切换会话
- **THEN** 系统只恢复目标会话的数据和布局

#### Scenario: 移除被引用论文
- **GIVEN** 历史消息或运行引用当前论文
- **WHEN** 用户从会话移除论文
- **THEN** 历史记录继续存在且引用显示 `detached`
- **THEN** 后续请求不得继续读取该论文

### Requirement: V0 Harness 与 V1 产品必须分别验收

系统 SHALL 将 Harness 选型与最小工作流实验作为 V0 技术验证，将真实工作台用户旅程作为 V1 产品验证；任一层通过不得替代另一层验收。

#### Scenario: V0 Harness validation
- **GIVEN** 候选 Harness 已接入最小只读工具集
- **WHEN** 使用固定研究题和固定预算运行实验
- **THEN** 验收 SHALL 覆盖真实工具调用、能力隔离、预算终止、事件可观察和引用闭环
- **THEN** 演示性输出质量不得单独构成通过

#### Scenario: V1 Product validation
- **GIVEN** 工作台已连接真实 API 和真实论文
- **WHEN** 用户完成“问题或论文入口 → 阅读或探索 → 引用回跳 → 固定笔记 → 知识库”的浏览器旅程
- **THEN** 验收 SHALL 同时检查状态诚实性、失败恢复、交互可理解性和知识边界
- **THEN** 单元测试、构建成功或 V0 回执不得替代真实浏览器验收
