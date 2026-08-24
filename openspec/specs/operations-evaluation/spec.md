# Operations and Evaluation

## Purpose

定义当前可运行服务的配置边界、健康检查和确定性检索评测能力。外部 provider 主要通过 fake adapter 验证；PostgreSQL 测试依赖显式环境变量。调度、持久化 checkpoint、真实外部调用回执与完整可观测性尚未实现。

## Requirements

### Requirement: API 从环境加载运行配置
系统 SHALL 从项目 `.env` 或进程环境读取 `DATABASE_URL`、可选 `DEEPSEEK_API_KEY` 和模型名称；数据库 URL 缺失时 SHALL 明确拒绝启动。

#### Scenario: 只有数据库配置
- **GIVEN** 环境包含合法 `DATABASE_URL` 但没有 DeepSeek Key
- **WHEN** 创建运行时 FastAPI 应用
- **THEN** 数据库和检索表完成初始化
- **THEN** 应用使用 citation-only 回答器成功启动

#### Scenario: 缺少数据库配置
- **GIVEN** 环境没有 `DATABASE_URL`
- **WHEN** 创建运行时应用
- **THEN** 启动失败并提示查看 `.env.example`

### Requirement: 服务提供存活检查
系统 SHALL 提供无需模型调用的健康检查接口。

#### Scenario: API 正常运行
- **GIVEN** FastAPI 已启动
- **WHEN** 客户端请求 `/api/health`
- **THEN** 返回 `status=ok`

### Requirement: 检索评测使用可复现黄金问题
系统 SHALL 从 JSONL 加载包含可回答标记、预期知识 ID 和可选领域的黄金问题，并计算确定性指标。

#### Scenario: 评测一组可回答和不可回答问题
- **GIVEN** 黄金集同时包含可回答和不可回答问题
- **WHEN** 使用固定 K 运行评测
- **THEN** 返回 Recall@K、MRR、不可回答问题意外命中率和预期知识命中中缺失版本标识的数量
- **THEN** 该计数只能识别版本标识缺失，不能判断一个非空版本是否为旧版本

#### Scenario: 黄金集为空或格式错误
- **GIVEN** 黄金集没有有效记录或字段类型不合法
- **WHEN** 加载评测数据
- **THEN** 系统拒绝运行并指出出错行或空集问题
