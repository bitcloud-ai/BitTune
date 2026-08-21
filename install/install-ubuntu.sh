#!/usr/bin/env bash
# Installs the base Bittune Agent on an apt-based x86_64 host.
# GPU, Docker, vLLM, Hugging Face, and EvalScope are explicit provider
# prerequisites and are deliberately not changed by this installer.
# Usage: sudo ./install-ubuntu.sh /path/to/bittune-<version>.tgz <linux-user>
set -euo pipefail

readonly NODE_VERSION="v22.22.2"
readonly NODE_SHA256="88fd1ce767091fd8d4a99fdb2356e98c819f93f3b1f8663853a2dee9b438068a"
readonly INSTALL_ROOT="${BITTUNE_INSTALL_ROOT:-/opt/bittune}"
readonly NODE_ROOT="${INSTALL_ROOT}/node"
readonly AGENT_ROOT="${INSTALL_ROOT}/agent"
readonly BACKUP_ROOT="${INSTALL_ROOT}/backups"

die() { echo "Bittune installer: $*" >&2; exit 1; }
log() { echo "[bittune-install] $*"; }
require_root() { [[ ${EUID} -eq 0 ]] || die "请使用 sudo 运行安装器。"; }
require_command() { command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"; }
backup_path() { printf '%s/%s-%s' "${BACKUP_ROOT}" "$1" "$(date -u +%Y%m%dT%H%M%SZ)"; }

PACKAGE_PATH=${1:-}
TARGET_USER=${2:-${SUDO_USER:-}}
[[ -n ${PACKAGE_PATH} && -r ${PACKAGE_PATH} ]] || die "第一个参数必须是可读的 Bittune .tgz 包。"
[[ -n ${TARGET_USER} ]] || die "第二个参数必须是运行 Bittune 的普通 Linux 用户。"
id "${TARGET_USER}" >/dev/null 2>&1 || die "用户不存在：${TARGET_USER}"
require_root
require_command apt-get
require_command curl
require_command sha256sum
require_command tar
[[ $(uname -m) == "x86_64" ]] || die "当前安装包仅包含 linux-x64 Node.js 运行时，需要 x86_64 主机。"

log "安装基础系统依赖…"
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl

mkdir -p "${INSTALL_ROOT}" "${BACKUP_ROOT}"
temporary=$(mktemp -d)
stage_agent=$(mktemp -d "${INSTALL_ROOT}/.agent-stage.XXXXXX")
trap 'rm -rf "${temporary}" "${stage_agent}"' EXIT

if [[ ! -x ${NODE_ROOT}/bin/node ]] || [[ $(${NODE_ROOT}/bin/node --version) != "${NODE_VERSION}" ]]; then
  log "安装固定 Node.js ${NODE_VERSION}…"
  archive="${temporary}/node.tar.xz"
  curl --fail --location --proto '=https' --tlsv1.2 "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-x64.tar.xz" --output "${archive}"
  echo "${NODE_SHA256}  ${archive}" | sha256sum --check --status || die "Node.js 下载 SHA-256 校验失败。"
  tar -xJf "${archive}" -C "${temporary}"
  candidate="${temporary}/node-${NODE_VERSION}-linux-x64"
  [[ -x ${candidate}/bin/node ]] || die "Node.js 解压结果不完整。"
  if [[ -e ${NODE_ROOT} ]]; then mv "${NODE_ROOT}" "$(backup_path node)"; fi
  mv "${candidate}" "${NODE_ROOT}"
fi

log "安装 Bittune Runtime…"
tar -xzf "${PACKAGE_PATH}" --strip-components=1 -C "${stage_agent}"
[[ -r ${stage_agent}/package.json ]] || die "Bittune 包缺少 package.json。"
[[ -r ${stage_agent}/dist/bittune.js ]] || die "Bittune 包缺少 dist/bittune.js。"
PATH="${NODE_ROOT}/bin:${PATH}" "${NODE_ROOT}/bin/npm" install --omit=dev --ignore-scripts --prefix "${stage_agent}"
if [[ -e ${AGENT_ROOT} ]]; then mv "${AGENT_ROOT}" "$(backup_path agent)"; fi
mv "${stage_agent}" "${AGENT_ROOT}"
trap - EXIT
rm -rf "${temporary}"

chown -R "${TARGET_USER}:${TARGET_USER}" "${INSTALL_ROOT}"

cat > /usr/local/bin/bittune <<EOF
#!/usr/bin/env bash
set -euo pipefail
export PATH="${INSTALL_ROOT}/node/bin:${INSTALL_ROOT}/agent/node_modules/.bin:\${PATH}"
exec "${INSTALL_ROOT}/node/bin/node" "${INSTALL_ROOT}/agent/dist/bittune.js" "\$@"
EOF
chmod 0755 /usr/local/bin/bittune

log "基础安装完成。没有安装或修改 Docker、NVIDIA Toolkit、GPU Driver、Runtime 镜像或模型。"
echo "请以用户 ${TARGET_USER} 执行："
echo "  export BITTUNE_AGENT_LLM_API_KEY='你的 Agent 模型密钥'"
echo "  bittune configure --base-url https://endpoint/v1 --model-id your-tool-capable-model"
echo "  bittune doctor"
echo "  bittune"
