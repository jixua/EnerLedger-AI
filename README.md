# 能碳会计 AI 智能体

这是一个面向企业能碳管理场景、可独立运行的智能应用项目。后端使用 Python，当前范围包括数据集管理、文档异步解析、企业文档大模型分析、BM25/Sparse/Dense 三路索引与混合检索，以及基于来源片段的 LLM 流式对话；`frontend/` 提供不区分用户端与管理端的一体化 Web 界面。

本项目不是把 LinkRag 作为 SDK、wheel、Git 依赖或本地路径依赖安装后调用，也不会把请求转发给另一套 LinkRag 服务。解析、索引、召回、融合和模型适配源码均维护在当前仓库的 `app/rag` 中。

## 当前边界

- 后端只有 Python 应用，不运行 Java 应用服务。
- PDF 固定使用源码内的 OpenDataLoader；它在 Python 进程中调用 Java，因此运行镜像仍需 OpenJDK 21。
- 上传入口支持 PDF、DOC/DOCX、HTML/HTM；旧版二进制 `.doc` 由
  LibreOffice 独立进程限时转换为 DOCX，再进入同一套 Mammoth 结构解析。
- Word 表格先构建统一 IR：无合并、嵌套、图片或多块内容的简单表格输出 GFM
  Markdown；复杂表格输出 `table-rag-v2` 文字结构，并保留跨行、跨列及父子表格
  元数据供预览和检索使用。HTML 只作为异常诊断回退，出现回退时质量门禁会拒绝入库。
- Word 解析会将 OMML 公式转为 LaTeX，并传播文档中已保存的显式分页信息；分页和
  表格协议 marker 只承担定位/结构边界职责，不进入最终检索文本。
- 文档上传后立即向 RabbitMQ 发布只携带文档 ID 的持久消息，独立 `parse-worker` 通过 `basic_consume` 主动接收并完成解析、切分和三路索引；MySQL 仅保存状态、租约与幂等真值。
- MySQL 只保留 `dataset`、`document`、`document_chunk`、`llm_config` 四张业务表；单管理员身份由部署配置提供，不新增用户表，也不提供注册接口。
- 不建立解析日志、阶段流水线、会话、消息、用量日志、厂商目录或模型目录表。
- `document.status` 使用 `QUEUED`、`PROCESSING`、`READY`、`FAILED`。只有 `READY` 文档可以参与检索。
- `POST /api/v1/documents/{document_id}/analysis` 读取文档当前版本的全部主体分片，分批调用 Chat 模型提取证据，生成 Markdown 分析报告并保存到 MinIO；`GET` 同路径读取当前版本最近一次成功报告。
- `POST /api/v1/rag/stream` 在一次请求中完成三路召回、上下文拼装和 LLM SSE 输出；当前不持久化会话或回答历史。

Alembic 会额外创建自己的版本记录表 `alembic_version`，它不属于业务表。

## 数据与索引

| 位置 | 职责 |
| --- | --- |
| `dataset` | 数据集元数据，以及 Dense、Sparse、Chat 模型配置绑定 |
| `document` | 原文件与解析产物位置、最终处理状态、页数和 Chunk 数 |
| `document_chunk` | Chunk 正文、顺序、类型和稳定的 `chunk_id` |
| `llm_config` | 可直接执行的模型协议、端点、能力与加密 API Key |
| MinIO | 原文件和解析后的 Markdown、图片资产 |
| Qdrant 1.17.1 | 同一 collection 中的 `dense` 与 `sparse_text` named vectors |
| Manticore 27.1.5 | 按数据集建表的中文 BM25 索引，应用连接 9306 SQL 端口 |

三路召回复用迁入的 LinkRag 逻辑：BM25 与 Sparse 分数先做 `log1p`，各路再做 min-max 归一化，最后按 BM25 `0.15`、Sparse `0.15`、Dense `0.70` 融合。某一路没有命中时，只在实际有结果的来源间重新归一化权重。当前 `RECALL_LTR_MODE=off`，不启用 LambdaMART。

## 源码基线

- 上游仓库：`ql-link/LinkRag`
- 上游分支：`dev`
- 固定提交：`861f24810c3482ec0d86768a24f952b1e08ae675`
- 迁移日期：2026-08-09
- 当前命名空间：`app.rag`

迁入后，`src.*` 导入已调整为 `app.rag.*`，PDF 默认后端固定为 `opendataloader`。部署不读取 `/Users/jixu/Project/Agent/LinkRag`。更细的来源说明见 [`app/rag/UPSTREAM.md`](app/rag/UPSTREAM.md)。

## 运行组件

| 组件 | 版本或用途 |
| --- | --- |
| Python | 3.11，FastAPI、SQLAlchemy 与 RAG 实现 |
| OpenJDK | 21 JRE，只作为 OpenDataLoader 的运行时，不承载业务服务 |
| MySQL | 8.0，四张业务表的事实源；不能用 SQLite 替代 |
| MinIO | S3 兼容对象存储 |
| Qdrant | 1.17.1，Dense/Sparse 向量索引 |
| Manticore | 27.1.5，BM25 关键词索引 |

## 重要：旧开发库必须重建

当前迁移链是新的单一根版本 `0001_minimal_rag`。它与此前 LinkRag 的 39 版迁移链没有继承关系，也没有提供旧表到四表模型的数据转换。

因此，已有旧开发库不能直接执行 `alembic upgrade head`，也不能把旧库 `stamp` 成新基线。这样做会保留不兼容的旧表，并让 Alembic 错误地认为结构已经更新。

如果旧数据需要保留，应先停止升级，另行设计导出、转换和重新索引流程。对于确认可以丢弃的本地开发环境，可重建整套 Compose 数据卷：

```bash
# 破坏性操作：删除当前 Compose 项目的 MySQL、MinIO、Qdrant、Manticore 数据。
docker compose down -v
docker compose up --build -d
```

不要在生产环境或仍有价值的数据上执行 `docker compose down -v`。

## Docker Compose 启动

首次启动先准备配置：

```bash
cp .env.example .env
```

至少替换 `.env` 中的 `MYSQL_ROOT_PASSWORD`、`DB_PASSWORD`、`MINIO_SECRET_KEY` 和 `API_KEY_ENCRYPTION_SECRET`。若修改数据库用户名、密码或库名，也必须同步更新 `DATABASE_URL`；Compose 内的数据库主机名保持 `mysql`。

启动：

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f api parse-worker
```

Compose 会等待 MySQL、MinIO 和 Manticore 就绪，并创建两个 MinIO 桶。API 容器随后执行 `alembic upgrade head`，成功后才启动 Uvicorn。

常用入口：

- API 文档：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/health/live>
- MinIO 控制台：<http://127.0.0.1:9001>
- Qdrant：<http://127.0.0.1:6333/dashboard>
- Manticore HTTP：<http://127.0.0.1:9308>

### 前端联调

```bash
cd frontend
cp .env.example .env.local
npm install
npm run dev -- --host 0.0.0.0 --port 4175 --strictPort
```

前端默认进入真实 API 模式，由 Vite 将 `/api` 和 `/health` 代理到 Python 服务；
后端不可达时会显示离线状态，不会静默切换成预览数据。只有显式设置
`VITE_DEMO_MODE=true` 才使用前端预览数据。完整测试矩阵、命令与本轮实测结果见
[前后端联调方案](docs/前后端联调方案.md)。

检查数据库版本：

```bash
docker compose exec api alembic current
docker compose exec api alembic heads
```

正常结果应指向 `0006_document_dispatch_outbox`。

## 本机开发启动

本机需要 Python 3.11、Java 11+（建议 21），以及可连接的 MySQL 8、MinIO、Qdrant 1.17.1、Manticore 27.1.5。

```bash
cp .env.example .env
uv sync --dev
uv run python -m nltk.downloader -d ./nltk_data punkt punkt_tab stopwords wordnet omw-1.4
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

本机直跑时，把 `.env` 中的 `DATABASE_URL`、`MINIO_ENDPOINT`、`QDRANT_URL`、`QDRANT_HOST`、`MANTICORE_HOST` 从 Compose 服务名改成 `127.0.0.1`。

确认 OpenDataLoader 的 Java 运行时：

```bash
java -version
```

### OpenDataLoader 表格策略 A/B

可用独立工具将一个 PDF 或目录内的 PDF 分别按 `default` 和 `cluster`
策略解析，然后基于本仓库的结构化表格提取器生成 JSON 对比报告：

```bash
uv run python scripts/evaluate_odl_table_strategies.py \
  /path/to/document.pdf \
  --timeout-seconds 600 \
  --cluster-markdown-with-html \
  --output /tmp/odl-table-ab.json
```

目录默认递归扫描，可用 `--no-recursive` 关闭。`--markdown-with-html` 同时
影响两组，`--default-markdown-with-html` 和 `--cluster-markdown-with-html` 可分别
覆盖。报告保留每组耗时、解析元数据、错误码、可重试标记、表格结构指标和
推荐策略；任一策略失败时进程返回码为 `1`，且不会用 Naive/MinerU
的降级结果冒充 OpenDataLoader 样本。

### PDF 金标验收

解析链路之外提供独立金标评估器，按页序、OCR、正文 CER、关键数值/单位/公式、
简单与复杂表格、跨页续表、图表关系、引用来源及 Top 5 噪声共 13 项指标验收：

```bash
uv run python scripts/evaluate_pdf_acceptance.py \
  --gold /path/to/gold.json \
  --prediction /path/to/prediction.json \
  --output /tmp/pdf-acceptance.json
```

通过、失败、缺少金标分别返回退出码 `0`、`1`、`2`；输入错误返回 `3`。缺少人工
金标时指标会明确标为 `NOT_EVALUABLE`，不会把结构检查或单元测试冒充准确率达标。

## 模型配置与调用顺序

Dense 与 Sparse 使用真实模型服务，不存在本地哈希向量兜底。开始解析前，需要分别创建
`EMBEDDING` 和 `SPARSE_EMBEDDING` 配置；使用 SSE 对话还需要 `CHAT` 配置。包含扫描页、
图表或流程图的 PDF 应在数据集绑定可选 `VISION` 配置，供页级 OCR/视觉兜底使用。

- 最终 `EMBEDDING` 输出维度必须等于 `DENSE_VECTOR_DIMENSION`，默认 2048；语义切片模型不受该维度约束。
- `SPARSE_EMBEDDING` 可使用 `bge_m3` 或 `doubao_vision` 等已迁入协议。
- API Key 经 AES-256-GCM 加密后写入 `llm_config`，接口只返回掩码。
- `provider_type`、`protocol` 和端点直接保存在 `llm_config`；当前没有 Provider catalog API 或相关表。

最小调用顺序：

| 顺序 | 接口 | 作用 |
| --- | --- | --- |
| 1 | `POST /api/v1/auth/login` | 使用部署配置中的管理员账号换取 Bearer JWT |
| 2 | `POST /api/v1/llm/configs` | 分别创建 Dense、Sparse、Chat，以及按需创建 Vision 配置 |
| 3 | `POST /api/v1/datasets` | 绑定模型配置并创建数据集 |
| 4 | `POST /api/v1/datasets/{dataset_id}/documents` | 流式上传原文件，返回 `202 + QUEUED` |
| 5 | `GET /api/v1/documents/{document_id}` | 查询排队、处理、成功或失败状态 |
| 6 | `GET /api/v1/documents/{document_id}/preview/content` | 流式读取当前版本的完整解析 Markdown |
| 7 | `GET /api/v1/documents/{document_id}/preview/map` | 一次读取当前版本的主体分片边界图 |
| 8 | `GET /api/v1/documents/{document_id}/preview/versions/{version}/assets/{asset_ref}` | 租户校验后流式读取 Markdown 内的私有图片 |
| 9 | `GET /api/v1/documents/{document_id}/chunks` | 按当前文档版本分页查看分片正文、顺序、类型与来源信息 |
| 10 | `GET /api/v1/documents/{document_id}/analysis` | 从 MinIO 读取当前版本最近一次成功的分析报告 |
| 11 | `POST /api/v1/documents/{document_id}/analysis` | 分析当前版本全文，持久化并返回 Markdown 报告与引用映射 |
| 12 | `GET /api/v1/documents` | 按用户查询全局解析队列，可按数据集和状态筛选 |
| 13 | `POST /api/v1/recall` | 仅执行三路召回与融合 |
| 14 | `POST /api/v1/rag/stream` | 混合检索后用 Chat 模型流式生成回复 |
| 15 | `GET /api/v1/system/status` | 查询中间件、持久队列和可观测 worker 状态 |

除存活检查和登录外，所有业务接口都要求 `Authorization: Bearer <token>`。当前产品只配置一个管理员，不提供注册入口；管理员密码只以 scrypt 哈希保存在部署环境中。接口字段以运行中的 OpenAPI `/docs` 为准。

SSE 示例：

```bash
curl -N http://127.0.0.1:8000/api/v1/rag/stream \
  -H 'Authorization: Bearer <登录接口返回的 access_token>' \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d '{"query":"企业天然气燃烧排放如何核算？","dataset_ids":[1]}'
```

可能返回的事件包括 `stream_started`、`recall_done`、`answer_delta`、`answer_done` 和 `error`；没有可用检索上下文时仍会依次返回 `recall_done`、固定说明文本和 `answer_done`，不会调用 Chat 模型。

### 企业文档分析

只有当前租户的 `READY` 文档可以发起分析。默认使用数据集绑定的 Chat 模型，也可以在请求体中用 `llm_config_id` 指定当前租户可用的 Chat 配置：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/documents/123/analysis \
  -H 'Authorization: Bearer <登录接口返回的 access_token>' \
  -H 'Content-Type: application/json' \
  -d '{}'
```

服务会按顺序读取文档当前版本的全部主体分片，排除解析产生的派生元素，并采用“分批证据提取 → 汇总成文”的两阶段模型调用。输入超过 `DOCUMENT_ANALYSIS_MAX_INPUT_TOKENS` 时返回明确错误，不会静默截断。报告结构参考产品碳足迹评价报告，包含以下固定 Markdown 章节：

1. 报告摘要；
2. 评价对象和目标；
3. 评价方法和工具；
4. 评价边界界定；
5. 功能单位或核算口径；
6. 生命周期与活动数据清单分析；
7. 碳足迹核算及评价；
8. 量化数据质量与可靠性；
9. 不确定性分析；
10. 文档规范性检查；
11. 资料缺口与整改建议；
12. 结论与分析限制。

报告中的事实性发现使用 `[文档片段N]` 标注来源，响应同时返回编号、分片 ID、页码和内容摘要的映射。当前阶段仅以被分析文档为事实依据，尚未把外部标准知识库纳入判定，因此通用检查维度不能视为具体标准条款，报告也不能替代正式审查或认证。

生成成功后，Markdown 以不可变内容哈希文件保存到当前解析版本目录下的 `analysis/`，`latest.json` 保存引用映射、模型、用量、生成时间和 Markdown 对象指针，并在最后更新作为提交标记。页面刷新后通过 `GET` 接口复用已保存报告；重新生成会切换指针并尽力清理旧报告。分析期间若文档版本或解析产物发生变化，本次结果不会持久化。重新解析与删除文档沿用解析目录前缀清理，可一并清除对应版本的分析产物。

前端可直接预览报告、下载 Markdown，或调用 `GET /api/v1/documents/{document_id}/analysis/docx` 将当前已持久化报告导出为 DOCX。DOCX 导出不重新调用大模型。

## 文档状态

- `QUEUED`：原文件已持久化，等待独立 `parse-worker` 领取。
- `PROCESSING`：worker 已持有可续租 lease，正在解析、切分或写入索引。
- `READY`：Markdown/资产、PDF 质量门禁与三路索引均完成，可以召回。
- `FAILED`：自动退避重试耗尽后失败，原因记录在 `error_code/error_message`，不会参与召回。

RabbitMQ 负责主动投递，MySQL `document` 行同时保存解析 lease 和 outbox 投递状态。
文档状态与待投递标记在一次事务内提交；API 随后尝试发布，后台补偿器会用
`FOR UPDATE SKIP LOCKED` 领取未投递或投递 lease 已过期的记录。因此进程在数据库提交后、
RabbitMQ 确认前后崩溃都能恢复；极端窗口可能重复投递，但文档版本和 lease fencing 会拒绝
重复处理。该方案不增加第五张业务表。失败任务按
`DOCUMENT_QUEUE_RETRY_DELAYS_SECONDS` 退避，达到 `DOCUMENT_QUEUE_MAX_ATTEMPTS` 后收敛为
`FAILED`。可使用 `POST /api/v1/documents/{id}/retry` 重试失败/过期任务，或使用
`POST /api/v1/documents/{id}/reparse` 基于同一原文件创建新版本；重新解析版本递增，普通
重试不递增。`PATCH /api/v1/documents/{id}` 可修改展示文件名，`DELETE` 会同步清理原文件、
解析产物、三路索引与 chunk（仍在有效 lease 内的 `PROCESSING` 文档拒绝删除）。
每个版本和 lease 尝试都写入独立的解析产物目录；只有 chunk 真值集与三路索引
已完成时，才会在同一次数据库提交中把 `parsed_object_key` 切换到新版本。失败或
失租只清理当次尝试目录，不覆盖、也不删除上一个 READY 产物；新版成功后
会清理被替代的旧版本，删除文档时再按文档级根目录全量收敛。

文档上传默认上限为 100 MiB，可通过 `DOCUMENT_UPLOAD_MAX_BYTES` 调整；上传和 worker 下载
均分块落盘，不会把 100 MiB 文件整体读入内存。PDF 仍以 OpenDataLoader 结构解析为主，
随后强制核对原页数与 `ODL_PAGE` 页序，统计正文/图片覆盖、旋转和 OCR 置信度。
对可靠文本层同时使用顺序敏感的 source recall 和 output precision 门禁，
两者默认均不低于 97%，避免截断、乱序、重复正文或追加幻觉文本进入索引。扫描页按
`250–300 DPI`（默认 280）整页送数据集绑定的 Vision 模型做 OCR；图表页补充结构化实体、
数值和箭头关系，再按原页码合并。页数不一致、OCR 未完成/低置信、视觉结构未完成或
表格/图片/公式专项验证失败都只会进入 `FAILED`，不会写入可检索 `READY`。质量报告直接
保存在 `document.parse_quality_status/parse_quality`，没有新增业务表。
独立 OpenDataLoader 进程同时受输出目录、文件数和日志容量硬限制：默认最多
`10000` 个输出文件（`PDF_MAX_OUTPUT_FILES`），stdout/stderr 合计最多 `16 MiB`
（`OPENDATALOADER_MAX_LOG_BYTES`）。运行中超限会终止整个进程组，并以确定性资源错误结束，
不会重试同一份输入。单页模型结构字段只保留白名单且最多 `64 KiB`，整份文档默认
最多 `4 MiB`（`PDF_FALLBACK_MAX_STRUCTURED_REPORT_BYTES`）；OCR 正文不会在质量 JSON 中重复持久化。

## 分片实现与查看

文档解析成功后，实际调用的是当前仓库 `app/rag/core/splitter` 中的结构化语义分片实现，
不是运行时安装或调用 LinkRag 包。该目录从上游 LinkRag 固定提交迁入并完成命名空间适配：
第一阶段按标题、段落、列表、表格、代码、公式等结构边界构造候选块，第二阶段依据配置执行
块内切分，再补充受控的相邻上下文。表格、图片、代码和公式等受保护元素不会被普通文本规则
任意截断。

当前默认配置与 LinkRag 基线一致，第一阶段使用 `candidate_boundary`，第二阶段使用 `noop`；
仓库已经包含基于 embedding 深度差异的 `semantic_depth_window` 实现，但默认不会额外调用模型
执行二次语义切分。需要启用时应通过数据集分片配置显式选择，而不是修改全局默认行为。

`GET /api/v1/documents/{document_id}/chunks` 只允许查看租户自己的 `READY` 文档，并且只返回
该文档当前版本的分片。接口支持 `offset`、`limit`、`q` 和 `chunk_type`，按 `chunk_index`
稳定排序；不会返回稠密/稀疏向量、内容哈希或数据库内部主键。新解析的分片会保存标题路径、
切分策略、分片角色和派生关系等可展示元数据。历史分片在迁移后仍可查看，但其结构元数据
可能为空。页码展示为“主体页”，因为相邻上下文可能包含邻页文本。

连续阅读页使用两个独立真值源：`preview/content` 从对象存储分块返回完整
Markdown，并在 `X-Document-Version` 响应头中返回版本；`preview/map` 一次返回同一
版本的全部主体分片行边界。边界图会排除 `chunk_role=derived_element` 的表格/图片
派生分片，也不返回 Chunk 正文，因此不会把 neighbor overlap 重复拼接成原文。
前端应校验两个接口的版本一致后，再按从 0 开始的 `start_line/end_line` 绘制分割线。
对私有 MinIO 中属于当前 Markdown 目录的图片，`preview/content` 会在流出前重写为
`preview/.../assets/{asset_ref}` 代理地址，不会暴露 bucket 或完整 object key。资产代理
会重新校验 Bearer JWT、READY 状态、当前版本和路径边界。因此前端渲染图片时
需先使用当前 API 身份头 `fetch` 该地址，再将返回的 Blob URL 交给 `<img>`；浏览器
原生 `<img src>` 不会自动携带自定义请求头。

`boundary_precision=line` 表示默认 `noop` 结构边界可靠；`approximate_line` 表示经过
`semantic_depth_window` 语义细分，在尚未持久化字符 offset 时仅能近似到行、
因此 `map_reliable=false`；`legacy_line` 表示历史分片缺少结构元数据。
历史数据只在 `chunk_index` 严格递增、行范围合法且互不重叠时返回降级边界，
同时标记 `map_reliable=false` 和 `reparse_required=true`；不满足条件时不猜测边界。
预览接口只允许读取当前租户的 `READY` 文档，不暴露 MinIO bucket 或 object key。

## 验证

```bash
uv run python -m compileall -q app migrations
uv run pytest
uv run ruff check app/api app/domain app/services app/main.py migrations tests
```

基础设施探活：

```bash
curl -fsS http://127.0.0.1:8000/health/live
curl -fsS http://127.0.0.1:6333/readyz
curl -fsS http://127.0.0.1:9308/
```

本地 Docker 整栈、真实 Dense/Sparse/Chat 模型、HTML 解析、RabbitMQ 主动解析队列、
MinIO/Qdrant/Manticore 写读、三路召回和 SSE 流式对话已于 2026-08-10 完成联调验证。
本轮另用真实 4 页正文 PDF 验证 ODL 页序与表格 A/B，并用真实 4 页扫描 PDF 验证全部页面
进入 280 DPI OCR 门禁。真实 Vision 模型的字符/公式/图表准确率仍必须通过上述人工金标工具
验收；单元测试或结构门禁通过不能替代准确率金标。
