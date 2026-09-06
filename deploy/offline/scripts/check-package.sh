#!/usr/bin/env sh
set -eu

package_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

for path in \
  docker-compose.yml \
  .env.example \
  README.md \
  docs/部署手册.md \
  docs/数据迁移手册.md \
  docs/接口文档.md \
  openapi.json \
  images/docker-images.tar.gz \
  data/mysql.sql.gz \
  data/mysql-table-counts.tsv \
  data/qdrant-collections.json \
  data/minio-data.tar.gz \
  data/qdrant-data.tar.gz \
  data/manticore-data.tar.gz \
  SHA256SUMS; do
  if [ ! -f "$package_dir/$path" ]; then
    echo "缺少文件: $path" >&2
    exit 1
  fi
done

(cd "$package_dir" && sha256sum -c SHA256SUMS)
docker compose --env-file "$package_dir/.env.example" -f "$package_dir/docker-compose.yml" config --quiet
echo "部署包结构、校验和与 Compose 配置有效"
