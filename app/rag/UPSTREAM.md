# LinkRag 源码迁移基线

本目录是当前项目内部的 RAG 能力实现，不是运行时 SDK 或外部服务代理。

- 上游仓库：`ql-link/LinkRag`
- 上游分支：`dev`
- 固定提交：`861f24810c3482ec0d86768a24f952b1e08ae675`
- 迁移日期：2026-08-09
- 当前命名空间：`app.rag`

迁入后已将 `src.*` 导入统一调整为 `app.rag.*`，并将 PDF 默认后端改为
`opendataloader`、LTR 模式改为 `off`。后续业务修改必须发生在当前仓库，运行时不读取
原 LinkRag 工作区，也不安装 `tolink-rag` 包。

Word 自适应表格、OMML 公式和保存态分页能力参考并迁移自本地 LinkParse 仓库：

- 来源路径：`/Users/fang/Developer/Projects/toLink/LinkParse`
- 来源分支：`master`
- 固定提交：`d72089eac41df4a597a628a8b9ca1e49d4030212`
- 迁移日期：2026-08-12

迁入后已改为当前项目的 `BaseParser -> ParseTaskService -> quality gate -> splitter`
链路，并使用当前项目的 MinIO 图片持久化、结构化表格 metadata 和失败状态语义；运行时
不读取 LinkParse 工作区。

当前项目的适配层位于 `app/api` 与 `app/domain`：它接管 Dataset、原文件和 LLM 配置
管理，并直接调用本目录的解析/召回管线。当前不建立模型用量表，用量仅写结构化调试
日志；运行链路不依赖 LinkRag-Service、MQ 或 Java 业务消费者。
