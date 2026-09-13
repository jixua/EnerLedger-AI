#!/usr/bin/env bash
set -Eeuo pipefail

env_file="${1:-/opt/enerledger-ai/.env}"

if [[ ! -f "$env_file" ]]; then
  echo "Environment file not found: ${env_file}" >&2
  exit 2
fi

read_value() {
  local key="$1"
  local line
  line="$(grep -E "^${key}=" "$env_file" | tail -n 1 || true)"
  if [[ -z "$line" ]]; then
    return 1
  fi
  line="${line#*=}"
  if [[ "$line" == \"*\" && "$line" == *\" ]]; then
    line="${line:1:${#line}-2}"
  elif [[ "$line" == \'*\' && "$line" == *\' ]]; then
    line="${line:1:${#line}-2}"
  fi
  printf '%s' "$line"
}

required_keys=(
  GHCR_NAMESPACE
  CORS_ALLOW_ORIGINS
  MYSQL_ROOT_PASSWORD
  DB_USER
  DB_PASSWORD
  DB_NAME
  DATABASE_URL
  API_KEY_ENCRYPTION_SECRET
  ADMIN_USERNAME
  ADMIN_PASSWORD_HASH
  JWT_SECRET
  MINIO_ACCESS_KEY
  MINIO_SECRET_KEY
  MINIO_PUBLIC_ENDPOINT
  RABBITMQ_USER
  RABBITMQ_PASSWORD
  RABBITMQ_VHOST
  RABBITMQ_URL
  PI_SERVICE_TOKEN
  ENERLEDGER_INTERNAL_AGENT_TOKEN
)

for key in "${required_keys[@]}"; do
  value="$(read_value "$key" || true)"
  if [[ -z "$value" ]]; then
    echo "Missing or empty production variable: ${key}" >&2
    exit 1
  fi
  if [[ "$value" == *replace-with* || "$value" == *example.com* || "$value" == *change-me* ]]; then
    echo "Production variable still contains a placeholder: ${key}" >&2
    exit 1
  fi
done

encryption_secret="$(read_value API_KEY_ENCRYPTION_SECRET)"
if [[ ! "$encryption_secret" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "API_KEY_ENCRYPTION_SECRET must contain exactly 64 hexadecimal characters" >&2
  exit 1
fi

admin_hash="$(read_value ADMIN_PASSWORD_HASH)"
if [[ ! "$admin_hash" =~ ^scrypt:16384:8:1:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+$ ]]; then
  echo "ADMIN_PASSWORD_HASH is not a supported project scrypt hash" >&2
  exit 1
fi

pi_token="$(read_value PI_SERVICE_TOKEN)"
internal_token="$(read_value ENERLEDGER_INTERNAL_AGENT_TOKEN)"
if (( ${#pi_token} < 32 || ${#internal_token} < 32 )); then
  echo "Pi Agent service tokens must contain at least 32 characters" >&2
  exit 1
fi
if [[ "$pi_token" == "$internal_token" ]]; then
  echo "Pi Agent service tokens must be different" >&2
  exit 1
fi

if [[ "$(read_value GHCR_NAMESPACE)" != ghcr.io/* ]]; then
  echo "GHCR_NAMESPACE must start with ghcr.io/" >&2
  exit 1
fi

echo "Production environment contract passed"
