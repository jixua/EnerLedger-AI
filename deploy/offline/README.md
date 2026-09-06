# EnerLedger-AI 离线 Docker 部署包

本目录是部署包模板。执行 `scripts/export-dev-package.sh` 后会生成包含当前 Dev 镜像、业务数据、接口定义和校验和的独立归档。实际数据、镜像和加密环境文件不进入 Git。

## 交付物

- `docker-compose.yml`：离线部署编排，包含 MySQL、MinIO/初始化器、Qdrant、Manticore、RabbitMQ、API、Parse Worker、Pi Agent、前端，可选 Report Worker。
- `images/docker-images.tar.gz`：当前 Dev 使用的全部镜像，包含固定版本中间件和 `alpine:3.21` 恢复工具镜像。
- `data/mysql.sql.gz`：MySQL schema、Alembic 版本和业务数据全量逻辑备份。
- `data/minio-data.tar.gz`：原文件及私有解析产物。
- `data/qdrant-data.tar.gz`：Dense/Sparse 向量索引。
- `data/manticore-data.tar.gz`：BM25 索引。
- `openapi.json`：从实际 Dev API 镜像导出的完整 OpenAPI 规范。
- `secrets.env.enc`：源环境配置的 AES-256 加密副本；解密口令不放入部署包。
- `initial-reviewer-password.txt.enc`：新审核员账号的初始密码，和环境文件使用同一离线口令加密。
- `MANIFEST.txt`、`SHA256SUMS`：源环境版本、镜像 ID、Alembic revision 和文件完整性记录。

## 最短部署流程

```bash
tar -xzf enerledger-offline-<release-id>.tar.gz
cd enerledger-offline-<release-id>
sha256sum -c SHA256SUMS
scripts/decrypt-secrets.sh /安全路径/package-passphrase.txt
scripts/restore-data.sh --confirm-empty-target
scripts/verify.sh
```

部署前必须阅读 [部署手册](docs/部署手册.md) 和 [数据迁移手册](docs/数据迁移手册.md)。恢复脚本只接受全新项目名和空数据卷，不覆盖已有数据。
