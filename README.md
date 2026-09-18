# 能碳会计 AI 智能体

这是一个面向企业能碳管理场景、可独立运行的智能应用项目。后端使用 Python，当前范围包括数据集管理、文档异步解析、企业文档大模型分析、BM25/Sparse/Dense 三路索引与混合检索、基于来源片段的 LLM 流式对话，以及通过独立 Node 服务运行的 Pi Agent；`frontend/` 提供不区分用户端与管理端的一体化 Web 界面。

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
- MySQL 保留 `dataset`、`document_folder`、`document`、`document_chunk`、`llm_config` 五张业务表；单管理员身份由部署配置提供，不新增用户表，也不提供注册接口。
- 不建立解析日志、阶段流水线、会话、消息、用量日志、厂商目录或模型目录表。
- `document.status` 使用 `QUEUED`、`PROCESSING`、`READY`、`FAILED`。只有 `READY` 文档可以参与检索。
- `POST /api/v1/documents/{document_id}/reports` 冻结文档、模板和模型版本后创建报告任务，经 outbox 投递到 RabbitMQ 交给独立 `report-worker`；生成过程可提问、可取消、可重试，产物为 Markdown、DOCX 与 HTML。
- `POST /api/v1/rag/stream` 在一次请求中完成三路召回、上下文拼装和 LLM SSE 输出；当前不持久化会话或回答历史。
- `POST /api/v1/agent/stream` 由 FastAPI 鉴权并代理独立 Pi Agent；Pi 只能调用当前运行范围内的受控知识库检索工具。前端历史仍仅存在当前页面，不冒充服务端会话。

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
| MySQL | 8.0，五张业务表的事实源；不能用 SQLite 替代 |
| MinIO | S3 兼容对象存储 |
| Qdrant | 1.17.1，Dense/Sparse 向量索引 |
| Manticore | 27.1.5，BM25 关键词索引 |
| Pi Agent | 0.84.2，独立 Node 22 无头 Agent 服务；源码固定在 `third_party/pi` |

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

生产环境的 Jenkins + SSH 镜像流式传输 + Docker Compose 发布方案与完整操作步骤见
[生产环境部署与 CI/CD 手册](docs/%E7%94%9F%E4%BA%A7%E7%8E%AF%E5%A2%83%E9%83%A8%E7%BD%B2%E4%B8%8ECI-CD.md)。
根目录 `docker-compose.yml` 仅用于本地开发，不应携带默认口令直接部署到公网服务器。

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

Compose 会等待 MySQL、MinIO 和 Manticore 就绪，并创建两个 MinIO 桶。API 容器随后执行 `alembic upgrade heads`，成功后才启动 Uvicorn。`dev` 可同时存在多个从 `master` 派生的独立候选迁移，因此启动阶段必须应用所有 head；后续在发布基线中再用 Alembic merge revision 收敛分支头。

常用入口：

- API 文档：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/health/live>
- MinIO 控制台：<http://127.0.0.1:9001>
- Qdrant：<http://127.0.0.1:6333/dashboard>
- Manticore HTTP：<http://127.0.0.1:9308>
- Pi Agent 仅在 Compose 内网开放；通过 `<API>/api/v1/agent/readiness` 检查完整链路

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

正常结果应包含当前代码的所有 head；单独的文件夹候选分支为 `0007_document_folders`。

## 本机开发启动

本机需要 Python 3.11、Java 11+（建议 21），以及可连接的 MySQL 8、MinIO、Qdrant 1.17.1、Manticore 27.1.5。

```bash
cp .env.example .env
uv sync --dev
uv run python -m nltk.downloader -d ./nltk_data punkt punkt_tab stopwords wordnet omw-1.4
uv run alembic upgrade heads
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
- 本机开发可创建 `codex_cli` 协议的 `CHAT` 配置。该配置固定调用本机
  `codex exec` 的 `gpt-5.4-mini` 与 `medium`（中等）推理档位，不填写 API 地址或 API Key，
  复用启动 API 服务的操作系统账号所持有的 Codex 登录态。

Codex CLI 模式中的“本机”指子进程在 API 服务所在主机执行，模型推理仍请求 OpenAI
服务。使用前先运行 `codex login status` 确认登录态，并确认该登录主体有
`gpt-5.4-mini` 权限；登录成功本身不代表指定模型可用。Docker Compose 默认镜像不包含
Codex CLI，因此该模式默认面向本机直接启动的 API，不能把宿主机登录目录或凭据直接打包
进镜像。

最小调用顺序：

| 顺序 | 接口 | 作用 |
| --- | --- | --- |
| 1 | `POST /api/v1/auth/login` | 使用部署配置中的管理员账号换取 Bearer JWT |
| 2 | `POST /api/v1/llm/configs` | 分别创建 Dense、Sparse、Chat，以及按需创建 Vision 配置 |
| 3 | `POST /api/v1/datasets` | 绑定模型配置并创建数据集 |
| 4 | `POST /api/v1/datasets/{dataset_id}/folders` | 按需创建仅用于分类的虚拟文件夹 |
| 5 | `POST /api/v1/datasets/{dataset_id}/documents` | 流式上传原文件，可选传入 `folder_id`，返回 `202 + QUEUED` |
| 6 | `GET /api/v1/documents/{document_id}` | 查询排队、处理、成功或失败状态 |
| 7 | `GET /api/v1/documents/{document_id}/preview/content` | 流式读取当前版本的完整解析 Markdown |
| 8 | `GET /api/v1/documents/{document_id}/preview/map` | 一次读取当前版本的主体分片边界图 |
| 9 | `GET /api/v1/documents/{document_id}/preview/versions/{version}/assets/{asset_ref}` | 租户校验后流式读取 Markdown 内的私有图片 |
| 10 | `GET /api/v1/documents/{document_id}/chunks` | 按当前文档版本分页查看分片正文、顺序、类型与来源信息 |
| 11 | `POST /api/v1/documents/{document_id}/reports` | 创建 R1–R7 报告任务，冻结文档、模板与模型版本并投递队列 |
| 12 | `GET /api/v1/report-runs` | 汇总当前用户跨文档的报告任务、状态与可下载产物 |
| 13 | `GET /api/v1/documents` | 按用户查询全局解析队列，可按数据集和状态筛选 |
| 14 | `POST /api/v1/recall` | 仅执行三路召回与融合 |
| 15 | `POST /api/v1/rag/stream` | 混合检索后用 Chat 模型流式生成回复 |
| 16 | `GET /api/v1/system/status` | 查询中间件、持久队列和可观测 worker 状态 |
| 17 | `GET /api/v1/agent/readiness` | 检查 FastAPI 与 Pi Agent 的双向鉴权和网络链路 |
| 18 | `POST /api/v1/agent/stream` | 使用当前授权数据集运行受控 Pi Agent 对话 |

Pi Agent 的信任边界、模型兼容、离线构建和服务令牌要求见
[`docs/pi-agent.md`](docs/pi-agent.md)。

除存活检查和登录外，所有业务接口都要求 `Authorization: Bearer <token>`。当前产品支持一个管理员和一个受限资料审核员，不提供注册入口；密码只以 scrypt 哈希保存在部署环境中。可用以下命令分别生成 `ADMIN_PASSWORD_HASH` 或 `REVIEWER_PASSWORD_HASH`：

```bash
uv run python -c 'from app.domain.auth import hash_admin_password; print(hash_admin_password("replace-me"))'
```

接口字段以运行中的 OpenAPI `/docs` 为准。

SSE 示例：

```bash
curl -N http://127.0.0.1:8000/api/v1/rag/stream \
  -H 'Authorization: Bearer <登录接口返回的 access_token>' \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d '{"query":"企业天然气燃烧排放如何核算？","dataset_ids":[1]}'
```

可能返回的事件包括 `stream_started`、`recall_done`、`answer_delta`、`answer_done` 和 `error`；没有可用检索上下文时仍会依次返回 `recall_done`、固定说明文本和 `answer_done`，不会调用 Chat 模型。

### 报告任务（R1–R7）

只有当前租户的 `READY` 文档可以发起报告任务。报告类型由 `reporting/templates/registry.json` 注册，R1–R7 覆盖产品碳足迹、组织温室气体清单、ESG/可持续性、能源审计、CBAM、SBTi 目标核定和核查声明：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/documents/123/reports \
  -H 'Authorization: Bearer <登录接口返回的 access_token>' \
  -H 'Content-Type: application/json' \
  -d '{"report_type":"R1","llm_config_id":1,"reporting_year":2025,"output_formats":["ONLINE","DOCX","HTML"]}'
```

任务创建时冻结文档版本、模板版本和模型配置，写入 `report_run` 后经 outbox 投递到 RabbitMQ，由独立 `report-worker` 领取并驱动 Pi report-agent 生成。Pi 侧按模板要求的证据、字段台账和章节逐块落库，缺失关键信息时任务转入 `NEEDS_INPUT` 并通过 `GET /api/v1/report-runs/{run_id}/questions` 提问，作答后继续，澄清轮次不消耗重试预算。

报告正文以 ReportIR（结构化中间表示）为真值，`GET /api/v1/report-runs/{run_id}/report` 返回该结构用于在线预览。完成后按 `output_formats` 渲染产物上传对象存储，并写入 `report_artifact` 记录；`GET /api/v1/report-runs/{run_id}/artifacts/{artifact_id}` 提供下载。渲染不重新调用模型。

`output_formats` 可取值 `ONLINE`（仅在线预览，不落盘）、`MARKDOWN`、`DOCX`、`HTML`，默认 `["ONLINE", "DOCX", "HTML"]`。三种产物都直接从 ReportIR 渲染，不经过中间格式：块的读法（列名、分项、平行数组的别名）集中在 `app/services/report_blocks.py`，四个渲染器共用，因此同一份报告在页面与三种导出件里内容一致。Word 里的表格是真表格（表头加底色、数值列右对齐、跨页重复表头），图表用单元格底色画成条形与占比条，页码走 Word 域、打开时刷新；HTML 产物是单文件：样式内联、不引用外部资源、不加载脚本，正文全部转义并附带禁止外部加载的 CSP，可直接双击打开或打印成 PDF。

上下文预算在创建时和 worker 预检两处把关：文档正文加结构提示超出模型窗口时返回 `REPORT_DOCUMENT_TOO_LARGE`，不会静默截断。

报告来源有两种，由 `report_run.source_kind` 区分：

- `DOCUMENT`：路径上的 `document_id`，即已入库解析的知识库文档。
- `INLINE`：对话里直传的材料。文件经 `POST /api/v1/agent/materials` 就地提取文本后在服务端暂存（**不进知识库**——不建文档记录、不切块入库、不建索引、不进召回），返回 `material_id`；创建报告时只传这个 id，正文不再回传浏览器。材料超过大小/页数/字符阈值时返回 413，提示先导入知识库。

`INLINE` 来源在内部会被切成报告专用的分片并冻结在该任务上（分片 ID 形如 `M-1`），agent 照旧按游标读完、提交时附带覆盖清单供服务端逐项核对——对用户是「没入库」，对链路是「该有的都有」，防伪造的覆盖校验一行没放松。因此 `report_run` 上 `document_id` / `document_version` / `dataset_id` 三个字段对 `INLINE` 为空，判断来源一律看 `source_kind`。

**版式模板同样支持直传**，走 `template_material_id`：文件和来源材料一样暂存在服务端、切成 `M-` 分片后冻结在任务的 `custom_template_manifest` 里（用 `source_kind` 区分知识库模板与内联模板），agent 通过 `read_custom_template_chunks` 读它。模板只控制章节标题、顺序、内容表达与版式，改不了业务字段、公式、证据与免责声明。

一轮对话最多带两份文件，**材料与模板都挂在对话上**（不随发送清空、重开对话时按最后一轮带附件的那一轮恢复），因此后续几轮可以继续用同一份材料或模板。角色由模型判定（`decide_material_roles`）：判不出来时回落规则——报告特征更明显的那份当模板；只有一份且像一份成型的报告时，认成模板而不是来源，此时若用户直接要报告，会明确提示缺少来源材料。问答链路只把**本轮新增**的附件正文交代给模型，挂着的历史附件不重复注入，避免几轮下来把上下文吃光。

一轮里判出多份来源时不静默只取一份，而是明确拒绝并说明（`REPORT_SOURCE_AMBIGUOUS`）。报告类型不唯一时沿用确认卡片（`TEMPLATE_SELECTION`），卡片同时带上材料与文档两套引用。

**对话里可以 `@` 一份知识库文档**（输入框打 `@` 弹出候选，一次一份，候选只列当前知识库范围内已解析到可检索状态的文档）。引用写成文本里的 `@文件名`——模型据此知道「这份文件」指哪一份，用户回头读这句话也读得懂；结构化身份另走附件的 `document_id`，并显式声明 `role: SOURCE`，免得一份长得像报告模板的文档被当成版式模板。之后两条路都通：说「用 @X 生成报告」走报告任务，来源为 `DOCUMENT` 并冻结该文档的版本（产物里带「来源文档」链接）；直接问它就只是提问，检索范围会被收窄到这一份文档（`scoped_doc_ids`），需要通读时模型再按目录与章节读取全文。与挂在对话上的材料不同，**`@` 引用只对当前这一轮有效**，下一轮要用再 `@` 一次。

前端在对话页和文档详情页都能发起报告任务；「报告中心」（`GET /api/v1/report-runs`）汇总当前用户跨文档的全部任务、状态和可下载产物，其中「来源」列对 `INLINE` 任务显示上传时的原文件名。

## 文档状态

- `PENDING_REVIEW`：外部系统提交的原文件已保存到 MinIO，等待管理员或资料审核员审核，不会投递解析任务。
- `QUEUED`：原文件已持久化，等待独立 `parse-worker` 领取。
- `PROCESSING`：worker 已持有可续租 lease，正在解析、切分或写入索引。
- `READY`：Markdown/资产、PDF 质量门禁与三路索引均完成，可以召回。
- `FAILED`：自动退避重试耗尽后失败，原因记录在 `error_code/error_message`，不会参与召回。
- `REJECTED`：外部提交资料未通过人工审核，保留原文件和审核记录，但不会解析或召回。

## 外部文档上传与审核

部署时为 API 配置独立的 `CRAWLER_UPLOAD_API_KEY`；留空会关闭外部上传入口。外部系统使用
`POST /api/v1/document-submissions` 提交 multipart 表单，其中 `dataset_id` 和 `file` 必填，
支持 PDF、Word（DOC/DOCX）和 UTF-8 Markdown（MD/MARKDOWN）；`source_url`、`title`、
`source_name` 与 JSON 对象字符串 `metadata` 可选：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/document-submissions \
  -H 'X-Document-Submission-Key: <submission_api_key>' \
  -F 'dataset_id=1' \
  -F 'file=@article.pdf;type=application/pdf' \
  -F 'source_url=https://example.org/articles/1' \
  -F 'title=文章标题' \
  -F 'source_name=partner-system' \
  -F 'metadata={"external_id":"article-1"}'
```

旧的 `POST /api/v1/crawler/uploads`、`X-Crawler-Api-Key` 和 `crawler_name` 参数继续兼容。
成功响应为 `201`，文档保持 `PENDING_REVIEW` 且 outbox 为 `IDLE`。管理员或资料审核员在前端
“资料审核”页面查看原文件、选择目标数据集后执行通过，或者填写原因后拒绝；通过操作调用
`POST /api/v1/document-submissions/{document_id}/review`，在同一事务中将文档切为 `QUEUED`
并创建待投递 outbox，随后才发送 RabbitMQ 解析消息。待审核和已拒绝资料不会出现在普通文档
列表或解析队列中。

受限资料审核员账号通过 `REVIEWER_USERNAME` 和 `REVIEWER_PASSWORD_HASH` 配置。
密码哈希的生成方式与管理员一致；`REVIEWER_PASSWORD_HASH` 留空时该账号禁用。审核员仅可：

- 读取待审资料、查看原文件、通过或拒绝，并在通过时选择入库数据集；
- 读取对话所需的数据集可用状态和脱敏模型摘要，使用 AI 对话。

数据集、文档、模型和系统配置的其他管理接口仍仅限管理员。

RabbitMQ 负责主动投递，MySQL `document` 行同时保存解析 lease 和 outbox 投递状态。
文档状态与待投递标记在一次事务内提交；API 随后尝试发布，后台补偿器会用
`FOR UPDATE SKIP LOCKED` 领取未投递或投递 lease 已过期的记录。因此进程在数据库提交后、
RabbitMQ 确认前后崩溃都能恢复；极端窗口可能重复投递，但文档版本和 lease fencing 会拒绝
重复处理。outbox 仍直接内聚在 `document` 表，不增加独立任务表。失败任务按
`DOCUMENT_QUEUE_RETRY_DELAYS_SECONDS` 退避，达到 `DOCUMENT_QUEUE_MAX_ATTEMPTS` 后收敛为
`FAILED`。可使用 `POST /api/v1/documents/{id}/retry` 重试失败/过期任务，或使用
`POST /api/v1/documents/{id}/reparse` 基于同一原文件创建新版本；重新解析版本递增，普通
重试不递增。`PATCH /api/v1/documents/{id}` 可修改展示文件名或 `folder_id`；文件夹仅是数据集内的虚拟分类，移动文档不会重命名 MinIO 对象、重新解析或重建索引。删除文件夹时，其中文档保留并回到“未分类”。文档 `DELETE` 会同步清理原文件、
解析产物、三路索引与 chunk（仍在有效 lease 内的 `PROCESSING` 文档拒绝删除）。
每个版本和 lease 尝试都写入独立的解析产物目录；只有 chunk 真值集与三路索引
已完成时，才会在同一次数据库提交中把 `parsed_object_key` 切换到新版本。失败或
失租只清理当次尝试目录，不覆盖、也不删除上一个 READY 产物；新版成功后
会清理被替代的旧版本，删除文档时再按文档级根目录全量收敛。

文档上传默认上限为 128 MiB，可通过 `DOCUMENT_UPLOAD_MAX_BYTES` 调整；上传和 worker 下载
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

`boundary_precision=line` 表示分片边界与 Markdown 行边界对齐；
`approximate_line` 表示切分点落在行内，在尚未持久化字符 offset 时只能近似到行、
因此 `map_reliable=false`。语义细分若沿换行切开仍属于精确行边界；
`legacy_line` 表示历史分片缺少结构元数据。
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
