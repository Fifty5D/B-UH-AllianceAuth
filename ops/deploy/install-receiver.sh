#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

usage() {
  echo "Usage: sudo install-receiver.sh CONFIG_JSON LEGACY_RECEIVER [DEPLOY_USER]" >&2
  exit 64
}

[[ "$#" -ge 2 && "$#" -le 3 ]] || usage
[[ "$(id -u)" -eq 0 ]] || { echo "Run this installer as root." >&2; exit 77; }

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "${script_dir}/../.." && pwd -P)"
config_source="$(readlink -f -- "$1")"
legacy_receiver="$(readlink -f -- "$2")"
deploy_user="${3:-buh-deployer}"

[[ -f "${config_source}" && ! -L "${config_source}" ]] || {
  echo "Receiver configuration must be a regular file." >&2
  exit 66
}
[[ -f "${legacy_receiver}" && -x "${legacy_receiver}" && ! -L "${legacy_receiver}" ]] || {
  echo "Legacy receiver must be an executable regular file." >&2
  exit 66
}
[[ "${legacy_receiver}" =~ ^/[A-Za-z0-9._/-]+$ && "${legacy_receiver}" != *"/../"* ]] || {
  echo "Legacy receiver path contains unsupported characters." >&2
  exit 66
}
[[ "${deploy_user}" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || {
  echo "Deploy user name is unsafe." >&2
  exit 66
}
id "${deploy_user}" >/dev/null 2>&1 || {
  echo "Deploy user does not exist." >&2
  exit 67
}

[[ "$(git -C "${repo_root}" rev-parse --show-toplevel)" == "${repo_root}" ]] || {
  echo "Receiver must be installed from a Git checkout." >&2
  exit 65
}
source_commit="$(git -C "${repo_root}" rev-parse HEAD)"
[[ "${source_commit}" =~ ^[0-9a-f]{40}$ ]] || {
  echo "Source checkout does not have a full Git commit identity." >&2
  exit 65
}
git -C "${repo_root}" diff --quiet --
git -C "${repo_root}" diff --cached --quiet --

install_sources=(
  ops/__init__.py
  ops/buh-github-observe-entry
  ops/buh-github-observe-root
  ops/buh-redact-diagnostics.py
  ops/deploy/__init__.py
  ops/deploy/contracts.py
  ops/deploy/docker_host.py
  ops/deploy/engine.py
  ops/deploy/receiver.py
  ops/deploy/buh-deploy-dispatch
  ops/deploy/buh-platform-v2-receiver
  ops/release/__init__.py
  ops/release/buh_release.py
)
for relative in "${install_sources[@]}"; do
  git -C "${repo_root}" ls-files --error-unmatch -- "${relative}" >/dev/null
  [[ -f "${repo_root}/${relative}" && ! -L "${repo_root}/${relative}" ]]
done

PYTHONPATH="${repo_root}" python3 - "${config_source}" <<'PY'
import sys
from pathlib import Path
from ops.deploy.contracts import ReceiverConfig

ReceiverConfig.load(Path(sys.argv[1]))
PY

install -d -m 0755 /usr/local/lib/buh-platform-v2/ops/deploy
install -d -m 0755 /usr/local/lib/buh-platform-v2/ops/release
install -m 0644 "${repo_root}/ops/__init__.py" \
  /usr/local/lib/buh-platform-v2/ops/__init__.py
install -m 0644 "${repo_root}/ops/deploy/__init__.py" \
  "${repo_root}/ops/deploy/contracts.py" \
  "${repo_root}/ops/deploy/docker_host.py" \
  "${repo_root}/ops/deploy/engine.py" \
  "${repo_root}/ops/deploy/receiver.py" \
  /usr/local/lib/buh-platform-v2/ops/deploy/
install -m 0644 "${repo_root}/ops/release/__init__.py" \
  "${repo_root}/ops/release/buh_release.py" \
  /usr/local/lib/buh-platform-v2/ops/release/

install -d -m 0700 /etc/buh-platform-v2
install -m 0600 "${config_source}" /etc/buh-platform-v2/receiver.json
install -m 0755 "${script_dir}/buh-platform-v2-receiver" \
  /usr/local/sbin/buh-platform-v2-receiver
install -m 0755 "${script_dir}/buh-deploy-dispatch" \
  /usr/local/sbin/buh-deploy-dispatch
install -d -m 0755 /usr/local/libexec
install -m 0755 "${repo_root}/ops/buh-github-observe-entry" \
  /usr/local/bin/buh-github-observe-entry
install -m 0755 "${repo_root}/ops/buh-github-observe-root" \
  /usr/local/sbin/buh-github-observe-root
install -m 0755 "${repo_root}/ops/buh-redact-diagnostics.py" \
  /usr/local/libexec/buh-redact-diagnostics

provenance_tmp="$(mktemp /etc/buh-platform-v2/.install.XXXXXX)"
python3 - "${repo_root}" "${config_source}" "${source_commit}" \
  "${install_sources[@]}" >"${provenance_tmp}" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
config = Path(sys.argv[2])
commit = sys.argv[3]
files = {}
for relative in sys.argv[4:]:
    files[relative] = hashlib.sha256((root / relative).read_bytes()).hexdigest()
value = {
    "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
    "files": files,
    "schema_version": 1,
    "source_commit": commit,
}
print(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
PY
install -o root -g root -m 0600 "${provenance_tmp}" \
  /etc/buh-platform-v2/INSTALL.json
rm -f -- "${provenance_tmp}"

mapfile -t private_dirs < <(
  PYTHONPATH="${repo_root}" python3 - "${config_source}" <<'PY'
import sys
from pathlib import Path
from ops.deploy.contracts import ReceiverConfig

config = ReceiverConfig.load(Path(sys.argv[1]))
print(config.state_dir)
print(config.backup_dir)
PY
)
[[ "${#private_dirs[@]}" -eq 2 ]]
install -d -o root -g root -m 0700 "${private_dirs[@]}"

sudoers_tmp="$(mktemp /etc/sudoers.d/.buh-platform-v2.XXXXXX)"
cleanup() { rm -f -- "${sudoers_tmp}"; }
trap cleanup EXIT
{
  printf '%s ALL=(root) NOPASSWD: %s\n' "${deploy_user}" "${legacy_receiver}"
  printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/buh-platform-v2-receiver preflight\n' "${deploy_user}"
  printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/buh-platform-v2-receiver deploy\n' "${deploy_user}"
} >"${sudoers_tmp}"
chmod 0440 "${sudoers_tmp}"
visudo -cf "${sudoers_tmp}" >/dev/null
install -o root -g root -m 0440 "${sudoers_tmp}" /etc/sudoers.d/buh-platform-v2

PYTHONPATH=/usr/local/lib/buh-platform-v2 python3 -P -m compileall -q \
  /usr/local/lib/buh-platform-v2/ops

echo "Platform v2 receiver installed but not enabled for SSH."
echo "Review docs/operations/release-and-deployment.md before changing the forced command."
