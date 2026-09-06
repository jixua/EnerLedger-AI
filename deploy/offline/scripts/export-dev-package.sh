#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=${REPO_ROOT:-$(CDPATH= cd -- "$script_dir/../../.." && pwd)}
source_compose_file=${SOURCE_COMPOSE_FILE:?请设置 SOURCE_COMPOSE_FILE}
source_env_file=${SOURCE_ENV_FILE:?请设置 SOURCE_ENV_FILE}
source_project_name=${SOURCE_PROJECT_NAME:?请设置 SOURCE_PROJECT_NAME}
passphrase_file=${PACKAGE_PASSPHRASE_FILE:?请设置 PACKAGE_PASSPHRASE_FILE}
package_api_image=${PACKAGE_API_IMAGE:?请设置 PACKAGE_API_IMAGE}
package_frontend_image=${PACKAGE_FRONTEND_IMAGE:?请设置 PACKAGE_FRONTEND_IMAGE}
package_pi_image=${PACKAGE_PI_IMAGE:?请设置 PACKAGE_PI_IMAGE}
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

data_source_api_image=$(docker inspect -f '{{.Config.Image}}' "$api_id")
data_source_frontend_image=$(docker inspect -f '{{.Config.Image}}' "$frontend_id")
data_source_pi_image=$(docker inspect -f '{{.Config.Image}}' "$pi_id")
for image in "$package_api_image" "$package_frontend_image" "$package_pi_image"; do
  docker image inspect "$image" >/dev/null
done
docker image tag "$package_api_image" enerledger/api:offline
docker image tag "$package_frontend_image" enerledger/frontend:offline
docker image tag "$package_pi_image" enerledger/pi-agent:offline

mysql_image=$(docker inspect -f '{{.Config.Image}}' "$mysql_id")
minio_image=$(docker inspect -f '{{.Config.Image}}' "$minio_id")
qdrant_image=$(docker inspect -f '{{.Config.Image}}' "$qdrant_id")
manticore_image=$(docker inspect -f '{{.Config.Image}}' "$manticore_id")
rabbitmq_image=$(docker inspect -f '{{.Config.Image}}' "$rabbitmq_id")
minio_mc_image=${MINIO_MC_IMAGE:-minio/mc:latest}
docker image inspect "$minio_mc_image" >/dev/null 2>&1 || docker pull "$minio_mc_image"
docker image inspect alpine:3.21 >/dev/null 2>&1 || docker pull alpine:3.21

docker image save \
  enerledger/api:offline \
  enerledger/frontend:offline \
  enerledger/pi-agent:offline \
  "$mysql_image" "$minio_image" "$minio_mc_image" "$qdrant_image" "$manticore_image" "$rabbitmq_image" \
  alpine:3.21 | gzip -1 >"$release_dir/images/docker-images.tar.gz"

reviewer_username=${REVIEWER_USERNAME:-reviewer}
reviewer_password=$(openssl rand -hex 16)
reviewer_hash=$(REVIEWER_BOOTSTRAP_PASSWORD="$reviewer_password" python3 -c 'import base64,hashlib,os,secrets; password=os.environ["REVIEWER_BOOTSTRAP_PASSWORD"].encode(); salt=secrets.token_bytes(16); derived=hashlib.scrypt(password,salt=salt,n=2**14,r=8,p=1,dklen=32); enc=lambda value: base64.urlsafe_b64encode(value).decode().rstrip("="); print(f"scrypt:16384:8:1:{enc(salt)}:{enc(derived)}")')
runtime_env=$(mktemp)
chmod 600 "$runtime_env"
grep -v -E '^(COMPOSE_PROJECT_NAME|API_IMAGE|FRONTEND_IMAGE|PI_AGENT_IMAGE|MYSQL_IMAGE|MINIO_IMAGE|MINIO_MC_IMAGE|QDRANT_IMAGE|MANTICORE_IMAGE|RABBITMQ_IMAGE|REVIEWER_USERNAME|REVIEWER_PASSWORD_HASH)=' "$source_env_file" >"$runtime_env"
{
  printf 'COMPOSE_PROJECT_NAME=enerledger-offline\n'
  printf 'API_IMAGE=enerledger/api:offline\n'
  printf 'FRONTEND_IMAGE=enerledger/frontend:offline\n'
  printf 'PI_AGENT_IMAGE=enerledger/pi-agent:offline\n'
  printf 'MYSQL_IMAGE=%s\n' "$mysql_image"
  printf 'MINIO_IMAGE=%s\n' "$minio_image"
  printf 'MINIO_MC_IMAGE=%s\n' "$minio_mc_image"
  printf 'QDRANT_IMAGE=%s\n' "$qdrant_image"
  printf 'MANTICORE_IMAGE=%s\n' "$manticore_image"
  printf 'RABBITMQ_IMAGE=%s\n' "$rabbitmq_image"
  printf 'REVIEWER_USERNAME=%s\n' "$reviewer_username"
  printf 'REVIEWER_PASSWORD_HASH=%s\n' "$reviewer_hash"
} >>"$runtime_env"

docker run --rm --env-file "$runtime_env" "$package_api_image" python -c \
  'import json; from app.main import app; print(json.dumps(app.openapi(), ensure_ascii=False, indent=2))' \
  >"$release_dir/openapi.json"
docker exec "$mysql_id" sh -ec '
  for table in $(mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -e "SHOW TABLES"); do
    count=$(mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -e "SELECT COUNT(*) FROM \`$table\`")
    printf "%s\t%s\n" "$table" "$count"
  done
' >"$release_dir/data/mysql-table-counts.tsv"
docker exec "$api_id" python -c \
  'import json,urllib.request; base="http://qdrant:6333"; listing=json.load(urllib.request.urlopen(base+"/collections",timeout=10)); names=[item["name"] for item in listing["result"]["collections"]]; print(json.dumps({"collections":{name:json.load(urllib.request.urlopen(base+"/collections/"+name,timeout=10))["result"] for name in names}},ensure_ascii=False,indent=2))' \
  >"$release_dir/data/qdrant-collections.json"
docker exec "$api_id" python -c \
  'import json,pymysql; c=pymysql.connect(host="manticore",port=9306,user="",password="",autocommit=True); q=c.cursor(); q.execute("SHOW TABLES"); tables=[r[0] for r in q.fetchall()]; counts={}; [(q.execute("SELECT COUNT(*) FROM `"+table+"`"),counts.__setitem__(table,q.fetchone()[0])) for table in tables]; print(json.dumps(counts,ensure_ascii=False,indent=2))' \
  >"$release_dir/data/manticore-table-counts.json"

openssl enc -aes-256-cbc -pbkdf2 -salt \
  -in "$runtime_env" \
  -out "$release_dir/secrets.env.enc" \
  -pass "file:$passphrase_file"
printf 'username=%s\npassword=%s\n' "$reviewer_username" "$reviewer_password" | \
  openssl enc -aes-256-cbc -pbkdf2 -salt \
    -out "$release_dir/initial-reviewer-password.txt.enc" \
    -pass "file:$passphrase_file"
reviewer_password=
reviewer_hash=

stopped_services=()
restart_source() {
  if [ "${#stopped_services[@]}" -gt 0 ]; then
    compose start "${stopped_services[@]}" >/dev/null || true
  fi
}
cleanup() {
  restart_source
  rm -f "${runtime_env:-}"
}
trap cleanup EXIT

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
    -v "$release_dir/data:/backup:Z" \
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
  printf 'data_source_git_sha=%s\n' "$(docker inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$api_id" 2>/dev/null || true)"
  printf 'package_git_sha=%s\n' "$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$package_api_image" 2>/dev/null || true)"
  printf 'alembic_revision=%s\n' "$(docker exec "$mysql_id" sh -ec 'mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -e "SELECT version_num FROM alembic_version ORDER BY version_num"' | paste -sd, -)"
  printf 'data_source_api_image=%s\n' "$data_source_api_image"
  printf 'data_source_frontend_image=%s\n' "$data_source_frontend_image"
  printf 'data_source_pi_image=%s\n' "$data_source_pi_image"
  printf 'package_api_image=%s\n' "$package_api_image"
  printf 'package_frontend_image=%s\n' "$package_frontend_image"
  printf 'package_pi_image=%s\n' "$package_pi_image"
  for id in "$api_id" "$frontend_id" "$pi_id" "$mysql_id" "$minio_id" "$qdrant_id" "$manticore_id" "$rabbitmq_id"; do
    docker inspect -f 'image={{.Config.Image}} image_id={{.Image}} container={{.Name}}' "$id"
  done
} >"$release_dir/MANIFEST.txt"

(cd "$release_dir" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum >SHA256SUMS)
restart_source
stopped_services=()
rm -f "$runtime_env"
runtime_env=
trap - EXIT

tar -C "$output_root" -czf "$release_dir.tar.gz" "$(basename "$release_dir")"
(cd "$output_root" && sha256sum "$(basename "$release_dir").tar.gz" >"$(basename "$release_dir").tar.gz.sha256")
echo "部署包目录: $release_dir"
echo "部署包归档: $release_dir.tar.gz"
echo "加密口令文件没有写入部署包，请通过独立安全渠道交付"
