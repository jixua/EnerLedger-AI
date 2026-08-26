# Alembic 迁移

本目录只管理当前项目的五张 MySQL 业务表：

- `llm_config`
- `dataset`
- `document_folder`
- `document`
- `document_chunk`

Alembic 自己创建的 `alembic_version` 是版本记录表，不属于业务表。MinIO、Qdrant 和 Manticore 的数据结构也不由 Alembic 管理。

## 当前基线

迁移链保留一个根版本，并在其上追加兼容的增量迁移：

```text
0001_minimal_rag
  -> 0002_document_parse_queue
  -> 0003_chunk_structure_metadata
  -> 0004_document_parse_quality
  -> 0005_dataset_vision_config
  -> 0006_document_dispatch_outbox
  -> 0007_crawler_document_review (head)
```

`0001_minimal_rag` 直接创建最初四张业务表，不依赖历史 LinkRag schema；`0007_document_folders` 增加仅用于文档分类的 `document_folder` 表和可空 `document.folder_id`。`migrations/db.sql` 是当前 head 的可读 SQL 快照；正常部署应以 `alembic upgrade heads` 为准，以兼容 `dev` 中多个从 `master` 派生的候选迁移，不要同时手工执行 SQL 文件。

`0004_document_parse_quality` 只在 `document` 表增加可空的质量状态与 JSON 摘要字段。升级时将已有 PDF 标记为 `LEGACY_UNCHECKED`，已有非 PDF 标记为 `NOT_APPLICABLE`；不会改变文档解析、队列或召回就绪状态。

`0005_dataset_vision_config` 在 `dataset` 表增加可空的 `vision_config_id`，用于绑定 PDF 页级 OCR/视觉兜底模型；不新增配置表，也不修改已有数据集的绑定。

`0007_crawler_document_review` 在 `document` 表保存第三方采集来源与人工审核状态。外部上传文件在审核通过前保持 `PENDING_REVIEW`，不会进入 RabbitMQ 解析队列。

## 与旧 39 版迁移链不兼容

`0001_minimal_rag` 是全新基线，没有把旧 39 版迁移的最后 revision 设为 `down_revision`，也没有承担旧表数据转换。

因此：

- 只能直接应用到空数据库；
- 不能在旧 39 版开发库上直接执行 `alembic upgrade head`；
- 不能用 `alembic stamp 0001_minimal_rag` 冒充完成迁移；
- 需要保留旧数据时，必须另行设计 ETL、ID 映射和 Qdrant/Manticore 重新索引。

对于确认可丢弃的本地 Compose 开发数据，可从仓库根目录执行：

```bash
# 破坏性操作：删除本项目全部 Compose 数据卷。
docker compose down -v
docker compose up --build -d
```

不要在生产环境或仍需保留的数据上执行上述删除命令。

## 常用命令

```bash
# 仅对空库或已经处于本基线的数据库执行
uv run alembic upgrade heads

uv run alembic current
uv run alembic heads
uv run alembic history

# 后续 schema 变更
uv run alembic revision --autogenerate -m "describe change"
```

降级到根版本之前会依次撤销增量字段和文件夹表；继续执行根版本的 `downgrade()` 会删除最初四张业务表，同样属于破坏性操作。
