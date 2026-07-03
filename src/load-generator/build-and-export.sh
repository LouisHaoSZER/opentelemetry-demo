#!/usr/bin/env bash
# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
#
# 构建 load-generator 镜像并导出为 tar.gz, 用于离线 (无远程仓库) 分发到 TKE 节点。
#
# 用法:
#   IMAGE_TAG=2.0.4-secure-2 ./build-and-export.sh
#
# 环境变量:
#   IMAGE_REPO   镜像仓库前缀 (不含 tag), 默认 otel-demo-load-generator。
#                离线场景下必须与 values 里 imageOverride.repository 完全一致。
#   IMAGE_TAG    镜像 tag, 默认 dev-$(git rev-parse --short HEAD)。
#   PLATFORM     buildx 平台, 默认 linux/amd64。
#   OUTPUT_DIR   tar.gz 输出目录, 默认 ./_dist。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"

IMAGE_REPO="${IMAGE_REPO:-otel-demo-load-generator}"
IMAGE_TAG="${IMAGE_TAG:-dev-${GIT_SHA}}"
PLATFORM="${PLATFORM:-linux/amd64}"
OUTPUT_DIR="${OUTPUT_DIR:-${SCRIPT_DIR}/_dist}"

FULL_IMAGE="${IMAGE_REPO}:${IMAGE_TAG}"
TAR_NAME="otel-demo-load-generator_${IMAGE_TAG}.tar"
TAR_PATH="${OUTPUT_DIR}/${TAR_NAME}"
TGZ_PATH="${TAR_PATH}.gz"

mkdir -p "${OUTPUT_DIR}"

echo ">> building ${FULL_IMAGE} (platform=${PLATFORM})"
if docker buildx version >/dev/null 2>&1; then
    docker buildx build \
        --platform "${PLATFORM}" \
        --tag "${FULL_IMAGE}" \
        --output type=docker \
        .
else
    echo ">> docker buildx unavailable, falling back to docker build (PLATFORM ignored)"
    docker build --tag "${FULL_IMAGE}" .
fi

echo ">> docker save -> ${TAR_PATH}"
docker save "${FULL_IMAGE}" -o "${TAR_PATH}"

echo ">> gzip -> ${TGZ_PATH}"
gzip -f -9 "${TAR_PATH}"

if command -v sha256sum >/dev/null 2>&1; then
    ( cd "${OUTPUT_DIR}" && sha256sum "$(basename "${TGZ_PATH}")" > "${TGZ_PATH}.sha256" )
    echo ">> sha256 -> ${TGZ_PATH}.sha256"
fi

cat <<EOF

>> Done.
   image:  ${FULL_IMAGE}
   tar.gz: ${TGZ_PATH}

   Next steps (on each TKE node):
       gunzip -k ${TAR_NAME}.gz && sudo ctr -n k8s.io images import ${TAR_NAME}
       # 或管道:
       gunzip -c ${TAR_NAME}.gz | sudo ctr -n k8s.io images import -

       sudo ctr -n k8s.io images ls -q | grep '${FULL_IMAGE}'

   values 中 imageOverride 必须与该完整名一致:
       repository: ${IMAGE_REPO}
       tag:        ${IMAGE_TAG}
       pullPolicy: IfNotPresent
EOF
