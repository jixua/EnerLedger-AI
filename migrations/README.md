# Alembic 迁移

本目录只管理当前项目的四张 MySQL 业务表：

- `llm_config`
- `dataset`
- `document`
- `document_chunk`

Alembic 自己创建的 `alembic_version` 是版本记录表，不属于业务表。MinIO、Qdrant 和 Manticore 的数据结构也不由 Alembic 管理。

## 当前基线

迁移链保留一个根版本，并在其上追加兼容的增量迁移：

```text
0001_minimal_rag
  -> 0002_document_parse_queue
  -> 0003_chunk_structure_metadata (head)
```

`0001_minimal_rag` 直接创建四张业务表，不依赖历史 LinkRag schema；后续迁移仍只修改这四张表。`migrations/db.sql` 是当前 head 的可读 SQL 快照；正常部署应以 `alembic upgrade head` 为准，不要同时手工执行 SQL 文件。

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
uv run alembic upgrade head

uv run alembic current
uv run alembic heads
uv run alembic history

# 后续 schema 变更
uv run alembic revision --autogenerate -m "describe change"
```

降级到根版本之前会依次撤销增量字段；继续执行根版本的 `downgrade()` 会删除四张业务表，同样属于破坏性操作。
