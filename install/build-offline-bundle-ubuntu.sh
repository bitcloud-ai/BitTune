#!/usr/bin/env bash
# Builds an offline Bittune base Agent bundle without GPU/Runtime providers.
# Usage: ./build-offline-bundle-ubuntu.sh /path/to/bittune-<version>.tgz /output/dir
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "${SCRIPT_DIR}/offline-manifest.env"
PACKAGE_PATH=${1:-}
OUTPUT_DIR=${2:-}
[[ -r ${PACKAGE_PATH} ]] || { echo "第一个参数必须是 Bittune .tgz。" >&2; exit 1; }
[[ -n ${OUTPUT_DIR} ]] || { echo "第二个参数必须是输出目录。" >&2; exit 1; }
[[ $(uname -m) == "x86_64" ]] || { echo "仅支持 x86_64 构建离线包。" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "缺少 curl。" >&2; exit 1; }
command -v tar >/dev/null 2>&1 || { echo "缺少 tar。" >&2; exit 1; }

readonly BUNDLE_NAME="bittune-offline-${BITTUNE_VERSION}-amd64"
mkdir -p "${OUTPUT_DIR}"
readonly STAGE=$(mktemp -d "${OUTPUT_DIR}/.bittune-stage.XXXXXX")
trap 'rm -rf "${STAGE}"' EXIT
readonly ROOT="${STAGE}/${BUNDLE_NAME}"
mkdir -p "${ROOT}/agent"

NODE_ARCHIVE="${ROOT}/node-${NODE_VERSION}-linux-x64.tar.xz"
curl --fail --location --proto '=https' --tlsv1.2 "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-x64.tar.xz" --output "${NODE_ARCHIVE}"
echo "${NODE_SHA256}  ${NODE_ARCHIVE}" | sha256sum --check --status
tar -xJf "${NODE_ARCHIVE}" -C "${ROOT}"
tar -xzf "${PACKAGE_PATH}" --strip-components=1 -C "${ROOT}/agent"
[[ -r ${ROOT}/agent/dist/bittune.js ]] || { echo "Bittune 包缺少 dist/bittune.js。" >&2; exit 1; }
PATH="${ROOT}/node-${NODE_VERSION}-linux-x64/bin:${PATH}" "${ROOT}/node-${NODE_VERSION}-linux-x64/bin/npm" install --omit=dev --ignore-scripts --prefix "${ROOT}/agent"
cp "${SCRIPT_DIR}/offline-install-ubuntu.sh" "${ROOT}/offline-install-ubuntu.sh"
cp "${SCRIPT_DIR}/offline-manifest.env" "${ROOT}/offline-manifest.env"

cat > "${ROOT}/BUILD-METADATA" <<EOF
architecture=amd64
created_at=$(date --iso-8601=seconds)
runtime_selection=runtime-free
model_snapshot=NOT_INCLUDED
EOF
(cd "${ROOT}" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
tar -C "${STAGE}" -czf "${OUTPUT_DIR}/${BUNDLE_NAME}.tar.gz" "${BUNDLE_NAME}"
echo "${OUTPUT_DIR}/${BUNDLE_NAME}.tar.gz"
