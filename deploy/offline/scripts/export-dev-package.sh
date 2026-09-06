#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(git rev-parse --show-toplevel)
source_compose_file=${SOURCE_COMPOSE_FILE:?请设置 SOURCE_COMPOSE_FILE}
source_env_file=${SOURCE_ENV_FILE:?请设置 SOURCE_ENV_FILE}
source_project_name=${SOURCE_PROJECT_NAME:?请设置 SOURCE_PROJECT_NAME}
passphrase_file=${PACKAGE_PASSPHRASE_FILE:?请设置 PACKAGE_PASSPHRASE_FILE}
output_root=${OUTPUT_ROOT:-$repo_root/deploy/offline/artifacts}
release_id=${RELEASE_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
release_dir="$output_root/enerledger-offline-$release_id"

if [ ! -f "$source_compose_file" ] || [ ! -f "$source_env_file" ]; then
  echo "源 Compose 或 .env 不存在" >&2
  exit 1
fi
if [ ! -f "$passphrase_file" ]; then
  echo "部署包加密口令文件不存在: $passphrase_file" >&2
  exit 1
fi
if [ -e "$release_dir" ]; then
  echo "输出目录已存在: $release_dir" >&2
  exit 1
fi

compose() {
  docker compose \
    --project-name "$source_project_name" \
    --env-file "$source_env_file" \
    -f "$source_compose_file" \
    "$@"
}

container_id() {
  local service=$1
  local id
  id=$(compose ps -q "$service")
  if [ -z "$id" ]; then
    echo "找不到运行中的服务: $service" >&2
    return 1
  fi
  printf '%s\n' "$id"
}

volume_for_mount() {
  local service=$1
  local destination=$2
  local id
  id=$(container_id "$service")
  docker inspect -f "{{range .Mounts}}{{if eq .Destination \"$destination\"}}{{.Name}}{{end}}{{end}}" "$id"
}

mkdir -p "$release_dir/images" "$release_dir/data" "$release_dir/docs" "$release_dir/scripts"
cp "$repo_root/deploy/offline/docker-compose.yml" "$release_dir/docker-compose.yml"
cp "$repo_root/deploy/offline/.env.example" "$release_dir/.env.example"
cp "$repo_root/deploy/offline/README.md" "$release_dir/README.md"
cp "$repo_root/deploy/offline/docs/部署手册.md" "$release_dir/docs/部署手册.md"
cp "$repo_root/deploy/offline/docs/数据迁移手册.md" "$release_dir/docs/数据迁移手册.md"
cp "$repo_root/deploy/offline/docs/接口文档.md" "$release_dir/docs/接口文档.md"
cp "$repo_root/deploy/offline/scripts/load-images.sh" "$release_dir/scripts/load-images.sh"
cp "$repo_root/deploy/offline/scripts/decrypt-secrets.sh" "$release_dir/scripts/decrypt-secrets.sh"
cp "$repo_root/deploy/offline/scripts/restore-data.sh" "$release_dir/scripts/restore-data.sh"
cp "$repo_root/deploy/offline/scripts/verify.sh" "$release_dir/scripts/verify.sh"
cp "$repo_root/deploy/offline/scripts/check-package.sh" "$release_dir/scripts/check-package.sh"
chmod 750 "$release_dir/scripts/"*.sh

api_id=$(container_id api)
frontend_id=$(container_id frontend)
pi_id=$(container_id pi-agent)
mysql_id=$(container_id mysql)
minio_id=$(container_id minio)
qdrant_id=$(container_id qdrant)
manticore_id=$(container_id manticore)
rabbitmq_id=$(container_id rabbitmq)
minio_volume=$(volume_for_mount minio /data)
qdrant_volume=$(volume_for_mount qdrant /qdrant/storage)
manticore_volume=$(volume_for_mount manticore /var/lib/manticore)

api_source_image=$(docker inspect -f '{{.Config.Image}}' "$api_id")
frontend_source_image=$(docker inspect -f '{{.Config.Image}}' "$frontend_id")
pi_source_image=$(docker inspect -f '{{.Config.Image}}' "$pi_id")
docker image tag "$api_source_image" enerledger/api:offline
docker image tag "$frontend_source_image" enerledger/frontend:offline
docker image tag "$pi_source_image" enerledger/pi-agent:offline

mysql_image=$(docker inspect -f '{{.Config.Image}}' "$mysql_id")
minio_image=$(docker inspect -f '{{.Config.Image}}' "$minio_id")
qdrant_image=$(docker inspect -f '{{.Config.Image}}' "$qdrant_id")
manticore_image=$(docker inspect -f '{{.Config.Image}}' "$manticore_id")
rabbitmq_image=$(docker inspect -f '{{.Config.Image}}' "$rabbitmq_id")
docker image inspect alpine:3.21 >/dev/null 2>&1 || docker pull alpine:3.21

docker image save \
  enerledger/api:offline \
  enerledger/frontend:offline \
  enerledger/pi-agent:offline \
  "$mysql_image" "$minio_image" "$qdrant_image" "$manticore_image" "$rabbitmq_image" \
  alpine:3.21 | gzip -1 >"$release_dir/images/docker-images.tar.gz"

docker exec "$api_id" python -c \
  'import json; from app.main import app; print(json.dumps(app.openapi(), ensure_ascii=False, indent=2))' \
  >"$release_dir/openapi.json"
docker exec "$mysql_id" sh -ec '
  for table in $(mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -e "SHOW TABLES"); do
    count=$(mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -e "SELECT COUNT(*) FROM \`$table\`")
    printf "%s\t%s\n" "$table" "$count"
  done
' >"$release_dir/data/mysql-table-counts.tsv"
docker exec "$api_id" python -c \
  'import urllib.request; print(urllib.request.urlopen("http://qdrant:6333/collections", timeout=10).read().decode())' \
  >"$release_dir/data/qdrant-collections.json"

openssl enc -aes-256-cbc -pbkdf2 -salt \
  -in "$source_env_file" \
  -out "$release_dir/secrets.env.enc" \
  -pass "file:$passphrase_file"

stopped_services=()
restart_source() {
  if [ "${#stopped_services[@]}" -gt 0 ]; then
    compose start "${stopped_services[@]}" >/dev/null || true
  fi
}
trap restart_source EXIT

for service in frontend api parse-worker report-worker pi-agent; do
  if compose ps -q "$service" 2>/dev/null | grep -q .; then
    compose stop -t 360 "$service"
    stopped_services+=("$service")
  fi
done

db_name=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$mysql_id" | sed -n 's/^MYSQL_DATABASE=//p' | tail -n 1)
if [ -z "$db_name" ]; then
  echo "无法确定 MYSQL_DATABASE" >&2
  exit 1
fi
docker exec "$mysql_id" sh -ec \
  'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --quick --routines --triggers --events --hex-blob --set-gtid-purged=OFF --databases "$MYSQL_DATABASE"' \
  | gzip -9 >"$release_dir/data/mysql.sql.gz"

for service in minio qdrant manticore; do
  compose stop -t 120 "$service"
  stopped_services+=("$service")
done

archive_volume() {
  local volume=$1
  local output=$2
  if [ -z "$volume" ]; then
    echo "无法确定数据卷: $output" >&2
    exit 1
  fi
  docker run --rm \
    -v "$volume:/source:ro" \
    -v "$release_dir/data:/backup" \
    alpine:3.21 \
    sh -ec "cd /source && tar -czf /backup/$output ."
}

archive_volume "$minio_volume" minio-data.tar.gz
archive_volume "$qdrant_volume" qdrant-data.tar.gz
archive_volume "$manticore_volume" manticore-data.tar.gz

{
  printf 'release_id=%s\n' "$release_id"
  printf 'exported_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'source_project=%s\n' "$source_project_name"
  printf 'source_git_sha=%s\n' "$(docker inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$api_id" 2>/dev/null || true)"
  printf 'alembic_revision=%s\n' "$(docker exec "$mysql_id" sh -ec 'mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -e "SELECT version_num FROM alembic_version ORDER BY version_num"' | paste -sd, -)"
  printf 'api_source_image=%s\n' "$api_source_image"
  printf 'frontend_source_image=%s\n' "$frontend_source_image"
  printf 'pi_source_image=%s\n' "$pi_source_image"
  for id in "$api_id" "$frontend_id" "$pi_id" "$mysql_id" "$minio_id" "$qdrant_id" "$manticore_id" "$rabbitmq_id"; do
    docker inspect -f 'image={{.Config.Image}} image_id={{.Image}} container={{.Name}}' "$id"
  done
} >"$release_dir/MANIFEST.txt"

(cd "$release_dir" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum >SHA256SUMS)
restart_source
stopped_services=()
trap - EXIT

tar -C "$output_root" -czf "$release_dir.tar.gz" "$(basename "$release_dir")"
sha256sum "$release_dir.tar.gz" >"$release_dir.tar.gz.sha256"
echo "部署包目录: $release_dir"
echo "部署包归档: $release_dir.tar.gz"
echo "加密口令文件没有写入部署包，请通过独立安全渠道交付"
