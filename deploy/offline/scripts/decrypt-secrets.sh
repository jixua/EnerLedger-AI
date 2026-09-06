#!/usr/bin/env sh
set -eu

package_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
passphrase_file=${1:-}

if [ -z "$passphrase_file" ] || [ ! -f "$passphrase_file" ]; then
  echo "用法: $0 /安全路径/package-passphrase.txt" >&2
  exit 1
fi
if [ ! -f "$package_dir/secrets.env.enc" ]; then
  echo "部署包中没有 secrets.env.enc" >&2
  exit 1
fi
if [ -e "$package_dir/.env" ]; then
  echo "$package_dir/.env 已存在，拒绝覆盖" >&2
  exit 1
fi

openssl enc -d -aes-256-cbc -pbkdf2 \
  -in "$package_dir/secrets.env.enc" \
  -out "$package_dir/.env" \
  -pass "file:$passphrase_file"
chmod 600 "$package_dir/.env"
echo "已解密为 $package_dir/.env；请先核对端口和外部地址，再执行恢复"
