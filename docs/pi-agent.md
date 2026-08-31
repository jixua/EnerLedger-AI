# Pi Agent 集成

## 架构边界

本项目采用与 LinkCV 相同的服务隔离方式，但工具和业务语义按能碳知识库重新实现：

```text
Web --管理员 JWT--> FastAPI --PI_SERVICE_TOKEN--> Pi Agent
                           ^                         |
                           +--内部受控检索工具-------+
                              ENERLEDGER_INTERNAL_AGENT_TOKEN
```

- `third_party/pi` 固定保存 Pi `0.84.2` 源码与离线模型目录快照，不在构建期动态追踪上游。
- `apps/pi-service` 是独立无头 Node 22 服务，只运行 Agent loop。
- FastAPI 仍是身份、数据集归属、模型密钥解密、召回和 SSE 的唯一业务边界。
- Pi 不连接 MySQL、MinIO、Qdrant、Manticore 或 RabbitMQ，也不启用默认的
  `bash`、`read`、`edit`、`write` 编码工具。
- Pi 只注册工作流读取以及 `get_retrieval_scope`、`hybrid_recall`、`expand_evidence`、
  `get_document_outline`、`read_document_section` 五个知识工具；检索范围
  由 FastAPI 根据不可预测的 `run_id` 反查，Pi 不能传入或覆盖 `user_id`、真实数据集 ID、
  文档范围、三路权重或召回深度。
- Agent 模式未选择具体知识库时，FastAPI 会解析当前用户全部 `ACTIVE` 知识库；明确选择
  一个或多个知识库时只使用该子集。Pi 看到的是 run 内不透明 `knowledge_base_ref`。
- 多知识库召回使用系统级 `system_cross_kb_v1` 策略，单知识库仍使用该库的 RecallConfig，
  不再让数据集排列顺序决定跨库召回参数。

当前不新增会话表。前端仅把当前页面最近 5 条用户/助手消息随请求发送；刷新页面后历史
仍会丢失，这与原有产品边界一致。若以后需要跨设备历史，应先独立设计会话、消息、运行和
工具审计的 MySQL 模型，不能把 Pi 进程内 session 当作业务事实源。

## 接口

- `GET /api/v1/agent/readiness`：验证 FastAPI → Pi → FastAPI 双向令牌和网络链路，不调用模型。
- `POST /api/v1/agent/stream`：管理员鉴权的 Agent SSE 入口；请求字段与 RAG 流一致，另可带
  当前页 `history`。
- `/internal/agent/**`：仅供 Pi 使用，不进入 OpenAPI；内部检索始终重新校验运行上下文。

SSE 保持现有前端协议：`stream_started`、`recall_done`、`answer_delta`、`answer_done` 和
`error`。对话页提供“普通对话 / 智能体”切换：普通对话继续调用原有
`/api/v1/rag/stream`，智能体调用 `/api/v1/agent/stream`。默认进入智能体模式，但原 RAG
模式及其请求、流式生成和引用展示链路继续保留。

`recall_done` 在兼容 `hits` 和 `failed_sources` 的基础上增加：

- `scope`：实际使用的全部或指定知识库范围；
- `retrieval`：三路启用状态、实际融合权重、候选数、上下文数、降级状态与耗时；
- `per_knowledge_base_counts`：各知识库候选与上下文分布；
- `hits`：全局排名、原始分、归一化分、加权贡献、知识库来源、稳定 `evidence_id` 和
  `selected_for_context`。

FastAPI 在单次 Agent run 内维护 Evidence Ledger。同一片段被多次召回时复用证据 ID；只有
进入模型上下文的片段才获得稳定的 `[片段N]` 引用编号。候选命中可以在前端展示，但不能被
Agent 当作已经使用的回答依据。

Pi 每轮必须读取 `knowledge-rag`、`knowledge-query-routing`、
`evidence-grounded-answering` 和 `document-reading` 四个 Skill。路由 Skill 允许寒暄、能力介绍等非知识请求不召回；
政策、标准、资料事实和核算依据必须调用 `hybrid_recall`，证据 Skill 要求只能使用返回的
`evidence_blocks` 作答。

第二阶段的阅读接口也位于 `/internal/agent/runs/{run_id}/...`：

- `evidence/expand` 只接受 Evidence Ledger 已登记的 `evidence_id`，最多向前、向后各扩展
  3 个同文档同版本 Chunk；
- `documents/outline` 只接受已登记证据或 run 内 `document_ref`，返回不透明的章节引用；
- `documents/sections/read` 每页最多读取 8 个 Chunk，用 run 内不透明游标继续，并返回
  `coverage.has_more` / `next_cursor`；
- 扩展和章节阅读返回的正文都会重新登记到同一 Evidence Ledger，继续分配稳定的
  `[片段N]`，并进入最终 SSE 来源列表。

只有片段存在截断、指代、公式/表格上下文不足时才扩展证据；只有整篇总结、结构梳理、
跨章节比较或指定章节阅读才进入目录—章节流程。未读完分页时 Agent 不得声称已读全文。

## 本地与容器运行

Pi 要求 Node `>=22.19.0`：

```bash
npm run pi:setup
npm run check:pi
npm run dev:pi
```

Compose 会构建并启动内部 `pi-agent` 服务。复制 `.env.example` 后，将
`AGENT_ENABLED=true`，并为以下两项提供不同的、至少 32 字符的高熵值：

- `PI_SERVICE_TOKEN`：FastAPI 调 Pi；
- `ENERLEDGER_INTERNAL_AGENT_TOKEN`：Pi 回调 FastAPI。

Production 不得使用示例 token。Pi 镜像先安装锁定依赖，再在禁网层校验模型数据并执行
`build:offline`；运行镜像不需要 npm 安装或模型目录网络访问。

## 模型兼容

Agent 复用现有 `llm_config` 的 `CHAT` 配置和加密 API Key，不增加第二套模型管理界面。
当前 Pi bridge 支持 `openai`、`anthropic`、`google` 和 OpenAI-compatible 的
`dashscope` protocol；其他 protocol 返回 `AGENT_MODEL_UNSUPPORTED`，不会静默改用别的模型。
