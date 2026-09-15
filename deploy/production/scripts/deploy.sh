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
compose_with_profiles=("${compose[@]}")
if grep -Eq '^COMPOSE_PROFILES=reports([[:space:]]*)$' "$env_file"; then
  compose_with_profiles+=(--profile reports)
fi

wait_for_services() {
  local deadline=$((SECONDS + 300))
  local services=(mysql minio qdrant manticore rabbitmq pi-agent api parse-worker web)
  local service container_id state health exit_code all_ready
  if grep -Eq '^COMPOSE_PROFILES=reports([[:space:]]*)$' "$env_file"; then
    services+=(report-worker)
  fi

  while ((SECONDS < deadline)); do
    all_ready=true

    container_id="$("${compose[@]}" ps -a -q minio-init)"
    if [[ -z "$container_id" ]]; then
      all_ready=false
    else
      IFS='|' read -r state health exit_code < <(
        docker inspect --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{.State.ExitCode}}' "$container_id"
      )
      if [[ "$state" == "exited" && "$exit_code" != "0" ]]; then
        echo "minio-init failed with exit code ${exit_code}" >&2
        return 1
      fi
      if [[ "$state" != "exited" || "$exit_code" != "0" ]]; then
        all_ready=false
      fi
    fi

    for service in "${services[@]}"; do
      container_id="$("${compose[@]}" ps -a -q "$service")"
      if [[ -z "$container_id" ]]; then
        all_ready=false
        continue
      fi
      IFS='|' read -r state health exit_code < <(
        docker inspect --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{.State.ExitCode}}' "$container_id"
      )
      if [[ "$state" == "exited" || "$state" == "dead" ]]; then
        echo "${service} stopped unexpectedly with exit code ${exit_code}" >&2
        return 1
      fi
      if [[ "$state" != "running" || ( -n "$health" && "$health" != "healthy" ) ]]; then
        all_ready=false
      fi
    done

    if [[ "$all_ready" == "true" ]]; then
      return 0
    fi
    sleep 5
  done

  "${compose[@]}" ps -a >&2
  echo "Services did not become ready within 300 seconds" >&2
  return 1
}

"${compose_with_profiles[@]}" config --quiet

skip_pull="${SKIP_PULL:-false}"
if [[ "$skip_pull" != "true" && "$skip_pull" != "false" ]]; then
  echo "SKIP_PULL must be true or false" >&2
  exit 2
fi

if [[ "$skip_pull" == "true" ]]; then
  mapfile -t required_images < <(
    "${compose_with_profiles[@]}" config --images | sort -u
  )
  for image in "${required_images[@]}"; do
    if ! docker image inspect "$image" >/dev/null 2>&1; then
      echo "Required preloaded image is missing: ${image}" >&2
      exit 1
    fi
  done
  echo "All production images are preloaded; registry pull skipped."
else
  "${compose[@]}" pull api parse-worker pi-agent web
  if grep -Eq '^COMPOSE_PROFILES=reports([[:space:]]*)$' "$env_file"; then
    "${compose[@]}" --profile reports pull report-worker
  fi
fi

application_services=(api parse-worker pi-agent web)
if grep -Eq '^COMPOSE_PROFILES=reports([[:space:]]*)$' "$env_file"; then
  application_services+=(report-worker)
fi

# Application containers must be recreated for every immutable RELEASE_SHA.
# Without --force-recreate, Compose can keep an older API/Web container alive
# while only starting newly added worker containers, producing a mixed release.
"${compose_with_profiles[@]}" up -d \
  --force-recreate \
  --no-build \
  --pull never \
  "${application_services[@]}"
wait_for_services
"${deploy_root}/bin/verify.sh" "$deploy_root"

if [[ -n "$current_sha" && "$current_sha" != "$RELEASE_SHA" ]]; then
  printf '%s\n' "$current_sha" > "${state_dir}/previous_sha"
fi
printf '%s\n' "$RELEASE_SHA" > "${state_dir}/current_sha"
printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${state_dir}/deployed_at"
printf '%s\n' "$GHCR_NAMESPACE" > "${state_dir}/ghcr_namespace"

echo "Deployment verified: ${RELEASE_SHA}"
