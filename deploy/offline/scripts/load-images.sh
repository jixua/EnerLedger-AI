#!/usr/bin/env sh
set -eu

package_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
archive="$package_dir/images/docker-images.tar.gz"

if [ ! -f "$archive" ]; then
  echo "缺少镜像归档: $archive" >&2
  exit 1
fi

gzip -dc "$archive" | docker image load
echo "Docker 镜像已导入"
