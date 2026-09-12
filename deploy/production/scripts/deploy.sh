#!/usr/bin/env bash
set -Eeuo pipefail

deploy_root="${1:-/opt/enerledger-ai}"
compose_file="${deploy_root}/docker-compose.yml"
env_file="${deploy_root}/.env"
state_dir="${deploy_root}/.deployment"

: "${GHCR_NAMESPACE:?GHCR_NAMESPACE is required}"
: "${RELEASE_SHA:?RELEASE_SHA is required}"

if [[ ! "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "RELEASE_SHA must be a full 40-character Git SHA" >&2
  exit 2
fi
if [[ ! -f "$compose_file" || ! -f "$env_file" ]]; then
  echo "Missing production compose or .env under ${deploy_root}" >&2
  exit 2
fi
if [[ "$(stat -c '%a' "$env_file")" != "600" ]]; then
  echo "${env_file} must have mode 600" >&2
  exit 2
fi

"${deploy_root}/bin/validate-env.sh" "$env_file"

mkdir -p "$state_dir"
lock_file="${state_dir}/deploy.lock"
exec 9>"$lock_file"
flock -n 9 || {
  echo "Another deployment is already running" >&2
  exit 3
}

current_sha=""
if [[ -f "${state_dir}/current_sha" ]]; then
  current_sha="$(<"${state_dir}/current_sha")"
fi

export GHCR_NAMESPACE RELEASE_SHA
compose=(docker compose --env-file "$env_file" -f "$compose_file")

"${compose[@]}" config --quiet
"${compose[@]}" pull api parse-worker pi-agent web
if grep -Eq '^COMPOSE_PROFILES=reports([[:space:]]*)$' "$env_file"; then
  "${compose[@]}" --profile reports pull report-worker
fi
"${compose[@]}" up -d --no-build --wait --wait-timeout 300
"${deploy_root}/bin/verify.sh" "$deploy_root"

if [[ -n "$current_sha" && "$current_sha" != "$RELEASE_SHA" ]]; then
  printf '%s\n' "$current_sha" > "${state_dir}/previous_sha"
fi
printf '%s\n' "$RELEASE_SHA" > "${state_dir}/current_sha"
printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${state_dir}/deployed_at"
printf '%s\n' "$GHCR_NAMESPACE" > "${state_dir}/ghcr_namespace"

echo "Deployment verified: ${RELEASE_SHA}"
