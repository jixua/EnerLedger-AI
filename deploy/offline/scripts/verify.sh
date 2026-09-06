#!/usr/bin/env sh
set -eu

package_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
compose_file="$package_dir/docker-compose.yml"
env_file="$package_dir/.env"

if [ ! -f "$env_file" ]; then
  echo "缺少 $env_file" >&2
  exit 1
fi

compose() {
  docker compose --env-file "$env_file" -f "$compose_file" "$@"
}

compose ps
compose exec -T api alembic current
compose exec -T mysql sh -ec '
  for table in $(mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -e "SHOW TABLES"); do
    count=$(mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -e "SELECT COUNT(*) FROM \`$table\`")
    printf "%s\t%s\n" "$table" "$count"
  done
'

api_port=$(sed -n 's/^API_PORT=//p' "$env_file" | tail -n 1)
api_port=${api_port:-8000}
curl -fsS "http://127.0.0.1:${api_port}/health/live"
curl -fsS "http://127.0.0.1:${api_port}/openapi.json" >/dev/null
echo
echo "基础健康、Alembic 版本、业务表和 OpenAPI 检查通过"
