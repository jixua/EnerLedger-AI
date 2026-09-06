#!/usr/bin/env sh
set -eu

package_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
compose_file="$package_dir/docker-compose.yml"
env_file="$package_dir/.env"

if [ "${1:-}" != "--confirm-empty-target" ]; then
  echo "该命令仅用于全新目标环境。确认目标没有同名业务卷后执行:" >&2
  echo "$0 --confirm-empty-target" >&2
  exit 1
fi
if [ ! -f "$env_file" ]; then
  echo "缺少 $env_file；先复制 .env.example 或解密 secrets.env.enc" >&2
  exit 1
fi
if grep -q 'CHANGE_ME' "$env_file"; then
  echo ".env 仍包含 CHANGE_ME，拒绝部署" >&2
  exit 1
fi

compose() {
  docker compose --env-file "$env_file" -f "$compose_file" "$@"
}

project_name=$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' "$env_file" | tail -n 1)
project_name=${project_name:-enerledger-offline}

for suffix in mysql-data minio-data qdrant-data manticore-data rabbitmq-data; do
  volume="${project_name}_${suffix}"
  if docker volume inspect "$volume" >/dev/null 2>&1; then
    echo "检测到已有卷 $volume，拒绝覆盖。请更换 COMPOSE_PROJECT_NAME 或人工处理。" >&2
    exit 1
  fi
done

"$package_dir/scripts/load-images.sh"

for suffix in mysql-data minio-data qdrant-data manticore-data rabbitmq-data; do
  docker volume create "${project_name}_${suffix}" >/dev/null
done

restore_volume() {
  volume=$1
  archive=$2
  if [ ! -f "$archive" ]; then
    echo "缺少数据归档: $archive" >&2
    exit 1
  fi
  docker run --rm \
    -v "$volume:/restore" \
    -v "$archive:/backup/archive.tar.gz:ro" \
    alpine:3.21 \
    sh -ec 'cd /restore && tar -xzf /backup/archive.tar.gz'
}

restore_volume "${project_name}_minio-data" "$package_dir/data/minio-data.tar.gz"
restore_volume "${project_name}_qdrant-data" "$package_dir/data/qdrant-data.tar.gz"
restore_volume "${project_name}_manticore-data" "$package_dir/data/manticore-data.tar.gz"

compose up -d mysql minio qdrant manticore rabbitmq

mysql_id=$(compose ps -q mysql)
attempt=0
until [ "$(docker inspect -f '{{.State.Health.Status}}' "$mysql_id" 2>/dev/null || true)" = healthy ]; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "MySQL 在 300 秒内未就绪" >&2
    exit 1
  fi
  sleep 5
done

gzip -dc "$package_dir/data/mysql.sql.gz" | compose exec -T mysql \
  sh -ec 'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD"'

compose up -d pi-agent api parse-worker frontend
if [ "${ENABLE_REPORTS:-false}" = true ]; then
  compose --profile reports up -d report-worker
fi

echo "数据恢复和服务启动完成；下一步执行 scripts/verify.sh"
