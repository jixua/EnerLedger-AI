#!/usr/bin/env bash
set -Eeuo pipefail

deploy_root="${1:-/opt/enerledger-ai}"
compose_file="${deploy_root}/docker-compose.yml"
env_file="${deploy_root}/.env"
state_dir="${deploy_root}/.deployment"

if [[ ! -f "$compose_file" || ! -f "$env_file" ]]; then
  echo "Missing production compose or .env under ${deploy_root}" >&2
  exit 2
fi

read_value() {
  local key="$1"
  local line
  line="$(grep -E "^${key}=" "$env_file" | tail -n 1 || true)"
  line="${line#*=}"
  if [[ "$line" == \"*\" && "$line" == *\" ]]; then
    line="${line:1:${#line}-2}"
  elif [[ "$line" == \'*\' && "$line" == *\' ]]; then
    line="${line:1:${#line}-2}"
  fi
  printf '%s' "$line"
}

requested_namespace="${GHCR_NAMESPACE:-}"
requested_sha="${RELEASE_SHA:-}"

GHCR_NAMESPACE="$(read_value GHCR_NAMESPACE)"
RELEASE_SHA="$(read_value RELEASE_SHA)"
API_BIND_ADDRESS="$(read_value API_BIND_ADDRESS)"
API_PORT="$(read_value API_PORT)"
WEB_BIND_ADDRESS="$(read_value WEB_BIND_ADDRESS)"
WEB_PORT="$(read_value WEB_PORT)"

if [[ -n "$requested_namespace" ]]; then
  GHCR_NAMESPACE="$requested_namespace"
elif [[ -f "${state_dir}/ghcr_namespace" ]]; then
  GHCR_NAMESPACE="$(<"${state_dir}/ghcr_namespace")"
fi
if [[ -n "$requested_sha" ]]; then
  RELEASE_SHA="$requested_sha"
elif [[ -f "${state_dir}/current_sha" ]]; then
  RELEASE_SHA="$(<"${state_dir}/current_sha")"
fi

: "${GHCR_NAMESPACE:?GHCR_NAMESPACE is required}"
: "${RELEASE_SHA:?RELEASE_SHA is required}"

compose=(docker compose --env-file "$env_file" -f "$compose_file")
"${compose[@]}" ps

for service in api pi-agent web; do
  image_json="$("${compose[@]}" images --format json "$service" | head -n 1)"
  if [[ "$image_json" != *"${RELEASE_SHA}"* ]]; then
    echo "${service} image does not match RELEASE_SHA=${RELEASE_SHA}" >&2
    exit 1
  fi
done

curl --fail --silent --show-error \
  "http://${API_BIND_ADDRESS:-127.0.0.1}:${API_PORT:-18000}/health/live" >/dev/null
curl --fail --silent --show-error \
  "http://${WEB_BIND_ADDRESS:-127.0.0.1}:${WEB_PORT:-18080}/health/live" >/dev/null

"${compose[@]}" exec -T api alembic current
echo "Runtime verification passed for ${RELEASE_SHA}"
