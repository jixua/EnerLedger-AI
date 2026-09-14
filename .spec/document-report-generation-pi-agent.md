# 基于 Pi Agent 的七类能碳报告生成系统设计

> 状态：设计草案  
> 日期：2026-08-28  
> 资料来源：`/Users/jixu/Downloads/报告生成规范.html`、`/Users/jixu/Downloads/模板文件库.rar`

## 1. 目标

在现有文档解析、分片、模型配置和 MinIO 存储能力之上，新增 R1-R7 七类业务报告生成能力。用户在指定文档的详情页选择报告类型，后端冻结源文档版本、模板版本和模型配置，Pi Agent 按对应 Skill 分析材料、提取字段、调用受控工具、发现缺口并生成结构化 ReportIR，最后由确定性校验和渲染组件输出在线报告、DOCX 和 PDF。

本设计不把大模型输出视为正式审计、认证、核查或法律意见，也不允许模型伪造第三方机构、签名、印章、认可编号或验证状态。

## 2. 已确认决策

1. 报告针对用户当前进入的固定文档生成。
2. 报告类型由用户在前端表单中选择，取值限定为 R1-R7。
3. 后端不自动替用户判断报告类型，Pi Agent 也不得改变用户选择。
4. 每类报告拥有独立的模板定义、Skill、样式配置和验收样例。
5. Pi Agent 在类型确认后启动，负责证据分析、字段提取、工具编排、缺口识别和报告生成。
6. 报告类型、模板版本、文档版本和模型配置在任务创建时冻结。
7. 最终权威产物是 ReportIR；Markdown、HTML、DOCX 和 PDF 都是派生产物。
8. 用户补充信息必须标记为 `USER_SUPPLIED`，不能伪装成源文档证据。
9. FastAPI继续负责权限、任务、模型治理、持久化、校验、渲染和最终发布。

## 3. 产品边界

系统区分两种模式：

| 模式 | 输入 | 输出 |
| --- | --- | --- |
| `ASSESS` | 一份已有报告或业务文档 | 完整性、规范性、资料缺口和整改建议 |
| `GENERATE` | 原始材料、数据文件或已有报告 | 用户指定的 R1-R7 业务报告草稿 |

现有文档分析能力可以继续作为 `ASSESS`，本设计主要新增 `GENERATE`。前端建议分别提供“文档审查”和“生成业务报告”，避免用户无法判断系统是在评价原文还是编制目标报告。

### 七类报告

| 编号 | 报告类型 | 重点内容 | 核心限制 |
| --- | --- | --- | --- |
| R1 | 产品碳足迹评价 | 产品、功能单位、系统边界、生命周期、GWP、阶段排放 | 不能将组织排放直接当作产品碳足迹 |
| R2 | 组织温室气体清单 | 组织边界、报告期、基准年、Scope 1/2/3、排放因子 | 必须区分 Scope 2 位置法和市场法 |
| R3 | ESG/可持续发展 | 治理、战略、风险、情景分析、指标与目标 | 不得凭部分披露宣称完整符合某框架 |
| R4 | 能源审计 | 能源账单、基准期、系统能耗、节能措施、经济性 | 节能率和回收期必须确定性计算 |
| R5 | CBAM | 报告期、进口商、CN 编码、设施、嵌入排放、碳价 | 冻结法规版本、币种和报告期，不提供法律保证 |
| R6 | SBTi | 基准年、Scope 覆盖、近期目标、净零目标、路径 | 不能将目标草案写成已验证通过 |
| R7 | 核查/验证 | 范围、保证等级、实质性、抽样、发现、意见 | 只能生成草稿，不能冒充核查机构签发 |

附件 PDF 是规范材料、优秀案例和视觉参考，不是七份可直接复制的统一母版。内容规范、视觉样式和品牌资产必须分开建模；附件说明与压缩包案例不一致的部分需要人工确定权威来源。

## 4. 总体架构

```text
前端文档详情/报告工作台
  → 选择 R1-R7、创建任务、查看进度、补充信息、下载产物
FastAPI 业务控制面
  → 权限、版本、模板、模型、状态机、内部 Tool API、校验和渲染
MySQL + RabbitMQ + MinIO
  → 权威状态、异步任务、检查点和报告产物
Pi Agent Service（内部 Node 服务）
  → 公共 Skill + 当前类型 Skill + 受控 Tools
EvidenceLedger + FieldLedger + ReportIR
  → Validator → HTML / DOCX / PDF
```

FastAPI是业务状态的唯一权威来源。Pi Session 可以用于恢复和审计，但不能成为字段结果、用户回答或任务状态的唯一存储。

## 5. 模板资产

```text
reporting/
├── common/
│   ├── report-ir.schema.json
│   ├── field-ledger.schema.json
│   ├── evidence.schema.json
│   └── evidence-grounded-report-generation/SKILL.md
└── templates/
    ├── r1-product-carbon-footprint/
    │   ├── definition.json
    │   ├── SKILL.md
    │   ├── style-profile.json
    │   ├── references.md
    │   └── fixtures/
    ├── r2-organizational-ghg/
    ├── r3-esg-sustainability/
    ├── r4-energy-audit/
    ├── r5-cbam/
    ├── r6-sbti/
    └── r7-verification/
```

### `definition.json`

模板定义是机器可执行的内容规范，应包含：模板 ID、报告类型、语义版本、适用材料、不适用场景、章节、字段、类型、单位、枚举、必填性、数据来源、公式 ID、校验规则、引用要求、禁止声明、样式配置和模板状态。

字段和公式规则不能只写在 Skill 中，必须由程序校验。模板首期保存在 Git 中，数据库只记录某次运行使用的版本。

### 每类 `SKILL.md`

每类 Skill 应说明：

1. 适用和不适用文档类型；
2. 输入材料最低要求；
3. 分析和读取顺序；
4. 必须调用的 Tool；
5. 字段映射和知识检索原则；
6. 计算工具使用规则；
7. 缺失、冲突和无法判断的处理；
8. 需要向用户追问的条件；
9. 章节、格式和引用要求；
10. 禁止作出的合规、认证和核查结论；
11. 提交 ReportIR 前的检查清单。

运行时只加载公共 Skill 和用户选择的一个领域 Skill，不同时加载全部七个 Skill。

### `style-profile.json`

样式配置负责页面尺寸、方向、字体、颜色、封面、页眉页脚、表格、图表和签署区，不参与业务事实判断。建议方向为：R1 横版产品型、R2 纵版审计型、R3 ESG 编辑型、R4 表单型、R5 法规文书型、R6 气候行动型、R7 正式意见书型。

## 6. 核心数据结构

### EvidenceLedger

记录进入报告的信息来源，`source_type` 至少支持：

- `DOCUMENT`：当前源文档；
- `KNOWLEDGE_BASE`：标准、指南、排放因子或优秀案例；
- `USER_INPUT`：用户在创建表单或补充问答中提供；
- `CALCULATION`：受控公式的计算结果。

```json
{
  "evidence_id": "E-0012",
  "source_type": "DOCUMENT",
  "document_id": 123,
  "document_version": 2,
  "chunk_id": "chunk-18",
  "page": 6,
  "excerpt": "报告期为2025年1月1日至12月31日",
  "content_hash": "sha256..."
}
```

### FieldLedger

记录每个模板字段的值、单位、状态、置信度和证据。状态统一为：`FOUND`、`CALCULATED`、`USER_SUPPLIED`、`MISSING`、`CONFLICT`、`NOT_APPLICABLE`、`UNVERIFIED`。禁止使用数值零代替缺失数据。

### ReportIR

ReportIR 包含任务和模板元数据、FieldLedger、章节、内容块、计算、警告、限制和渲染配置。支持的内容块首期包括：标题、段落、列表、表格、指标卡、柱状图、环形图、时间轴、风险矩阵、提示框、来源注释、签署区和分页符。

所有具体事实必须映射到 EvidenceLedger；所有计算值必须记录公式 ID、输入字段、精度和计算版本。

## 7. 前端设计

### 创建报告表单

表单至少包含：

- 源文件：只读；
- 报告类型：R1-R7，必填；
- 报告年度或报告期：按模板显示；
- 语言：默认简体中文；
- 模型：只展示用户有权限且支持工具调用的模型；
- 补充要求：可选；
- 输出格式：在线报告、DOCX、PDF；
- 当前模板的适用场景、主要章节和最低材料说明。

前端不提交任意模板版本，后端选择当前注册版本并冻结。模板状态与评审信息仅作元数据展示，不限制创建报告。

### 报告工作台

建议提供：

```text
[报告预览] [待补充信息] [字段与证据] [生成记录]
```

展示报告类型、模板和文档版本、当前阶段、缺失/冲突/未核实字段数量、结构化问题、在线预览、下载入口和失败重试。首期先实现结构化补充表单，后续对话面板必须绑定 `run_id`，不能绕过字段状态和最终校验。

## 8. 外部 API

```http
GET  /api/v1/report-templates
POST /api/v1/documents/{document_id}/reports
GET  /api/v1/report-runs/{run_id}
GET  /api/v1/report-runs/{run_id}/questions
POST /api/v1/report-runs/{run_id}/answers
GET  /api/v1/report-runs/{run_id}/report
GET  /api/v1/report-runs/{run_id}/artifacts/docx
GET  /api/v1/report-runs/{run_id}/artifacts/pdf
```

创建请求示例：

```json
{
  "report_type": "R2",
  "llm_config_id": 7,
  "language": "zh-CN",
  "reporting_year": 2025,
  "user_instructions": "重点展示 Scope 3"
}
```

后端冻结 `template_id`、`template_version`、`document_version`、`model_config_id` 和输入哈希。报告必须按 `run_id` 隔离，不同类型和不同运行不能共享一个文档级 `latest.json` 指针。

## 9. 数据库与状态机

新增 `report_runs`，记录用户、数据集、文档和版本、报告类型、模板和版本、模式、模型配置、Pi Session、状态、阶段、输入哈希、Manifest、错误和时间字段。

新增 `report_questions`，记录 `run_id`、字段 ID、问题类型、问题、互斥选项、是否必填、状态、回答、回答人和回答时间。

状态机：

```text
PENDING
→ PREPARING
→ ANALYZING_SOURCE
→ EXTRACTING_FIELDS
→ RETRIEVING_REFERENCES
→ CALCULATING
→ NEEDS_INPUT
→ GENERATING_REPORT
→ VALIDATING_REPORT
→ RENDERING
→ SUCCEEDED
```

终态还包括 `FAILED`、`CANCELLED` 和 `STALE_DOCUMENT`。`NEEDS_INPUT` 是可恢复状态，用户回答后从持久化检查点继续。文档版本或解析对象变化时禁止发布旧报告。

## 10. Pi Agent Service

建议新增独立 Node 服务：

```text
apps/pi-service/
├── src/server.ts
├── src/agent-runner.ts
├── src/model-runtime.ts
├── src/tools/
├── src/resources/
├── src/security/
├── tests/
├── package.json
└── Dockerfile
```

Session 必须禁用通用 `read/write/edit/bash`，只加载公共 Skill、当前类型 Skill 和业务 Tool；使用短期 `run_token`；限制 Agent 轮数、工具调用、Token、并发和总时间；订阅 Agent/Tool 事件写入运行进度。

## 11. Tool 设计

| Tool | 职责 |
| --- | --- |
| `get_analysis_context` | 返回当前运行的文档、模板、版本和预算上下文 |
| `get_template_definition` | 只返回任务冻结的模板，禁止切换类型 |
| `read_document_chunks` | 按顺序分页读取主体分片、页码、表格和视觉描述 |
| `search_reference_knowledge` | 检索规范、指南、排放因子和案例并标记来源类型 |
| `calculate_report_metrics` | 只执行注册公式，返回单位、精度和依赖字段 |
| `save_analysis_checkpoint` | 幂等保存 EvidenceLedger、FieldLedger 和阶段结果 |
| `request_clarification` | 创建字段级问题，使任务进入 `NEEDS_INPUT` |
| `validate_report_ir` | 校验 Schema、字段、单位、公式、引用、章节和禁止声明 |
| `submit_report_ir` | 提交通过校验的候选 IR，不直接覆盖最终产物 |

Agent 无权直接发布报告、切换模板、执行任意代码或访问任意网络。

## 12. 模型治理

Pi 服务不保存用户 API Key。推荐通过 FastAPI 内部模型网关，根据 `run_id` 解析用户和 `model_config_id`，复用现有模型授权与 Provider 调用。创建任务时必须验证模型具备 `CHAT` 和 `TOOL_CALLING` 能力；模型停用、删除或失去授权时明确失败，不得静默切换模型。

## 13. 补充问答

首期采用结构化字段问题。用户回答保存为 `USER_INPUT` Evidence，并把字段状态更新为 `USER_SUPPLIED`。问题需要说明缺失影响，并优先提供互斥选项。

后续对话能力绑定 `run_id`，可用于解释缺失和冲突、补充字段、查看计算依据、要求重新分析章节和修改未发布草稿。对话不能把用户陈述改写为源文档事实，也不能绕过模板和校验。

## 14. 渲染与存储

Renderer 只接受通过校验的 ReportIR，并以共享内容块渲染器加七个 Style Profile 生成在线 HTML、DOCX 和 PDF。

每次运行建议保存：

```text
report-ir.json
evidence-ledger.json
field-ledger.json
validation-report.json
report.md
preview.html
report.docx
report.pdf
run-audit.json
manifest.json
```

对象前缀建议为：

```text
reports/{user_id}/{document_id}/v{document_version}/{run_id}/
```

Manifest 记录哈希、内容类型、模板版本、模型配置 ID、生成时间和渲染版本，不保存模型密钥或服务 Token。R7 签署区必须标记为草稿或待核查机构确认。

## 15. 安全和可观测性

安全要求：Pi 仅内部访问；FastAPI与Pi使用独立服务身份和短期限定 Token；Tool 全部进行 Schema 与租户校验；文档、知识库和用户输入均视为不可信数据；文档中的指令不能改变系统规则；报告不得声称已经完成正式审计、认证、核查或法律合规判断。

所有日志和指标绑定 `run_id`、`trace_id`，记录阶段耗时、Agent 轮数、Tool 次数、Token、分片/证据/字段数量、校验失败、修复次数、渲染耗时和产物大小。日志不得记录密钥、Token、密码或完整敏感正文。

## 16. 测试与验收

模板测试覆盖 Schema 加载、ID 唯一性、公式依赖、Skill/目录一致性、Style Profile 和版本状态。Tool 测试覆盖租户越权、文档版本变化、稳定分页、检查点幂等、公式重算、模板不可切换和过期 Token。Agent 测试每类至少准备完整、缺失、冲突、Prompt Injection、超大文档、模型截断、Tool 超时和补充后继续样例。

每份成功报告必须满足：

1. 报告类型与用户选择一致；
2. 每个必填字段有明确状态，不静默漏项；
3. 缺失数据不写成零；
4. 事实有文档、知识库、用户或计算来源；
5. 计算可以由程序重算；
6. 无证据内容不写成合规、认证或验证结论；
7. 用户补充信息可识别；
8. 文档版本变化时不发布旧报告；
9. DOCX/PDF 无乱码、截断、错位和签署误导；
10. Manifest 哈希和对象引用有效。

## 17. 实施阶段

### 阶段一：七类模板工程

完成 7 份 `definition.json`、7 份 `SKILL.md`、7 份 `style-profile.json`、公共 Schema 和各类验收样例。建议顺序：

```text
R2 → R1 → R3/R6 → R4 → R5 → R7
```

### 阶段二：报告平台底座

完成 ReportRun 数据库、RabbitMQ 任务、模板注册表、API、ReportIR 存储、Validator、前端选择表单和报告工作台。

### 阶段三：Pi Agent

完成独立 Pi 服务、内部模型网关、Tools、Skill 加载、运行预算、日志和 readiness，先以 R2、R1 验证真实端到端。

### 阶段四：补充问答和七类接入

完成 `NEEDS_INPUT`、问题和回答持久化、任务继续执行，以及 R3-R7 接入。

### 阶段五：正式产物

完成七类在线样式、DOCX、PDF、图表、视觉回归、历史版本和下载。

## 18. 开放项

1. 首期是否同时保留 `ASSESS` 与 `GENERATE`，还是只交付 `GENERATE`；
2. 首期是否直接输出 PDF，还是先交付在线预览和 DOCX；
3. 一个任务是否只允许一个源文档，后续是否支持多文档材料包；
4. 用户补充是否需要人工确认后才能写入最终报告；
5. R5 法规版本和更新时间策略；
6. R7 可生成的草稿范围和强制免责声明；
7. 知识检索使用当前数据集、专用规范数据集还是组合；
8. 是否区分草稿、待确认和最终发布状态。

## 19. 首个里程碑

完成 R1-R7 模板分析和版本化资产，确定统一 EvidenceLedger、FieldLedger 与 ReportIR，并以 R2、R1 为试点完成模板评审和固定样例验证。模板评审通过后再开始 Pi Agent、任务平台和渲染实现。

该里程碑不等于已经接入 Pi、不等于已经生成可用于审计或核查的正式报告，也不等于已经完成 DOCX/PDF 视觉验收。

## 20. 当前实现状态（2026-08-28）

已实现报告平台底座、Pi sidecar 和在线报告工作台，并对整体设计评审发现的高风险问题完成了以下加固：

1. 任务创建时冻结完整模板资产快照、非敏感模型配置和文档分片 Manifest，运行期不再依赖可变的当前模板。
2. Pi 恢复分析前必须读取结构化追问和用户答案；用户输入按内容哈希绑定为 `USER_INPUT` 证据。
3. Pi 必须按冻结顺序覆盖全部文档分片；提交时 Pi 和 FastAPI Worker 分别验证覆盖率、顺序、分片哈希和 Manifest 哈希。
4. ReportIR 通过 JSON Schema 和业务校验器双层检查，并根据权威分片、用户答案和模板来源规则校验证据，不信任 Pi 自报的校验结果。
5. 注册公式改为确定性 Decimal 计算，支持求和、除法、缩放除法、百分比和加权求和；所有计算字段在提交时重放核对。
6. 模型端点默认只允许 HTTPS 公网主机，拦截本机、内网、保留地址和未允许主机；生产 Pi 服务必须配置主机白名单。
7. 前端支持用户显式选择 R1-R7、结构化补充问答、历史任务恢复、取消、失败重试和在线结果查看。

当前边界：七类模板仍为 `DRAFT`，评审状态仅作元数据保留，不作为报告创建门禁；参考知识库 Tool 仍返回明确的不可用状态；DOCX/PDF 渲染和真实模型、数据库、RabbitMQ、MinIO 端到端验收尚未完成。
