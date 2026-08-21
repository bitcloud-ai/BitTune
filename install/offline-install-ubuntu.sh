#!/usr/bin/env bash
# Installs an offline Bittune base Agent bundle. The bundle contains neither
# model snapshots nor Docker/vLLM/NVIDIA provider software.
# Usage: sudo ./offline-install-ubuntu.sh <linux-user>
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
MANIFEST_PATH="${SCRIPT_DIR}/offline-manifest.env"
manifest_value() { awk -F= -v key="$1" '$1 == key { print substr($0, index($0, "=") + 1); exit }' "${MANIFEST_PATH}"; }
BITTUNE_VERSION=$(manifest_value BITTUNE_VERSION)
NODE_VERSION=$(manifest_value NODE_VERSION)
[[ -n ${BITTUNE_VERSION} && -n ${NODE_VERSION} ]] || { echo "离线包清单不完整。" >&2; exit 1; }
TARGET_USER=${1:-${SUDO_USER:-}}
[[ ${EUID} -eq 0 ]] || { echo "请使用 sudo 运行离线安装器。" >&2; exit 1; }
[[ -n ${TARGET_USER} ]] && id "${TARGET_USER}" >/dev/null 2>&1 || { echo "必须提供存在的普通 Linux 用户。" >&2; exit 1; }
[[ $(uname -m) == "x86_64" ]] || { echo "离线包仅包含 linux-x64 Node.js 运行时。" >&2; exit 1; }
(cd "${SCRIPT_DIR}" && sha256sum --check SHA256SUMS)

INSTALL_ROOT=${BITTUNE_INSTALL_ROOT:-/opt/bittune}
BACKUP_ROOT="${INSTALL_ROOT}/backups"
mkdir -p "${INSTALL_ROOT}" "${BACKUP_ROOT}"
backup_path() { printf '%s/%s-%s' "${BACKUP_ROOT}" "$1" "$(date -u +%Y%m%dT%H%M%SZ)"; }
[[ -x ${SCRIPT_DIR}/node-${NODE_VERSION}-linux-x64/bin/node ]] || { echo "离线包缺少 Node.js。" >&2; exit 1; }
[[ -r ${SCRIPT_DIR}/agent/dist/bittune.js ]] || { echo "离线包缺少 Bittune Runtime。" >&2; exit 1; }
[[ -d ${SCRIPT_DIR}/agent/node_modules ]] || { echo "离线包缺少已安装的 Node.js 运行依赖。" >&2; exit 1; }

stage=$(mktemp -d "${INSTALL_ROOT}/.offline-stage.XXXXXX")
trap 'rm -rf "${stage}"' EXIT
cp -a "${SCRIPT_DIR}/node-${NODE_VERSION}-linux-x64" "${stage}/node"
cp -a "${SCRIPT_DIR}/agent" "${stage}/agent"
if [[ -e ${INSTALL_ROOT}/node ]]; then mv "${INSTALL_ROOT}/node" "$(backup_path node)"; fi
if [[ -e ${INSTALL_ROOT}/agent ]]; then mv "${INSTALL_ROOT}/agent" "$(backup_path agent)"; fi
mv "${stage}/node" "${INSTALL_ROOT}/node"
mv "${stage}/agent" "${INSTALL_ROOT}/agent"
trap - EXIT
rm -rf "${stage}"
chown -R "${TARGET_USER}:${TARGET_USER}" "${INSTALL_ROOT}"

cat > /usr/local/bin/bittune <<EOF
#!/usr/bin/env bash
set -euo pipefail
export PATH="${INSTALL_ROOT}/node/bin:${INSTALL_ROOT}/agent/node_modules/.bin:\${PATH}"
exec "${INSTALL_ROOT}/node/bin/node" "${INSTALL_ROOT}/agent/dist/bittune.js" "\$@"
EOF
chmod 0755 /usr/local/bin/bittune
echo "离线基础安装完成：未安装 Docker、NVIDIA Toolkit、Runtime 镜像或模型。请让 ${TARGET_USER} 配置 Agent Endpoint；需要推理 Provider 时再按需准备其前置条件。"
