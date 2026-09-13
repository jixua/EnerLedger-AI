#!/usr/bin/env bash
set -Eeuo pipefail

deploy_root="${1:-/opt/enerledger-ai}"
state_dir="${deploy_root}/.deployment"
previous_sha_file="${state_dir}/previous_sha"

if [[ ! -f "$previous_sha_file" ]]; then
  echo "No verified previous release is recorded" >&2
  exit 2
fi

previous_sha="$(<"$previous_sha_file")"
if [[ ! "$previous_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Recorded previous SHA is invalid" >&2
  exit 2
fi

echo "Rollback changes application containers only; database migrations are not downgraded."
echo "Confirm migration compatibility before continuing."
read -r -p "Type the full previous SHA to continue: " confirmation
if [[ "$confirmation" != "$previous_sha" ]]; then
  echo "Rollback cancelled" >&2
  exit 3
fi

export GHCR_NAMESPACE="$(<"${state_dir}/ghcr_namespace")"
export RELEASE_SHA="$previous_sha"
export SKIP_PULL="${SKIP_PULL:-true}"
"${deploy_root}/bin/deploy.sh" "$deploy_root"
