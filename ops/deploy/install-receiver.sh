#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1

usage() {
  echo "Usage: sudo install-receiver.sh CONFIG_JSON LEGACY_RECEIVER [DEPLOY_USER]" >&2
  exit 64
}

[[ "$#" -ge 2 && "$#" -le 3 ]] || usage
[[ "$(id -u)" -eq 0 ]] || { echo "Run this installer as root." >&2; exit 77; }

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "${script_dir}/../.." && pwd -P)"
reviewed_inventory="${BUH_REVIEWED_INVENTORY:-}"
install_root="${BUH_RECEIVER_INSTALL_ROOT:-/}"
canonical_config=/etc/buh-platform-v2/receiver.json
canonical_legacy=/usr/local/sbin/buh-moon-tax-platform-remote
[[ "${install_root}" == /* && -d "${install_root}" && ! -L "${install_root}" ]] || {
  echo "Receiver install root must be an existing absolute non-symlink directory." >&2
  exit 66
}
install_root="$(cd -- "${install_root}" && pwd -P)"

rooted() {
  [[ "$1" == /* && "$1" != *"/../"* ]] || return 66
  if [[ "${install_root}" == "/" ]]; then
    printf '%s\n' "$1"
  else
    printf '%s%s\n' "${install_root}" "$1"
  fi
}

library_dir="$(rooted /usr/local/lib/buh-platform-v2)"
config_dir="$(rooted /etc/buh-platform-v2)"
receiver_path="$(rooted /usr/local/sbin/buh-platform-v2-receiver)"
dispatcher_path="$(rooted /usr/local/sbin/buh-deploy-dispatch)"
observer_entry_path="$(rooted /usr/local/bin/buh-github-observe-entry)"
observer_root_path="$(rooted /usr/local/sbin/buh-github-observe-root)"
redactor_path="$(rooted /usr/local/libexec/buh-redact-diagnostics)"
sudoers_path="$(rooted /etc/sudoers.d/buh-platform-v2)"
compile_cache=""
[[ ! -L "$1" && ! -L "$2" ]] || {
  echo "Receiver inputs must not be symbolic links." >&2
  exit 66
}
config_source="$(readlink -f -- "$1")"
legacy_source="$(readlink -f -- "$2")"
config_path="${BUH_RECEIVER_CONFIG_PATH:-${config_source}}"
legacy_receiver="${BUH_LEGACY_RECEIVER_PATH:-${legacy_source}}"
deploy_user="${3:-buh-deployer}"

[[ -f "${config_source}" && ! -L "${config_source}" ]] || {
  echo "Receiver configuration must be a regular file." >&2
  exit 66
}
[[ -f "${legacy_source}" && -x "${legacy_source}" && ! -L "${legacy_source}" ]] || {
  echo "Pinned legacy receiver must be an executable regular file." >&2
  exit 66
}
[[ "${config_path}" == "${canonical_config}" && \
  "${legacy_receiver}" == "${canonical_legacy}" ]] || {
  echo "Receiver configuration and legacy receiver paths must be canonical." >&2
  exit 66
}
managed_targets=(
  /usr/local/lib/buh-platform-v2
  /etc/buh-platform-v2
  /usr/local/bin/buh-github-observe-entry
  /usr/local/sbin/buh-github-observe-root
  /usr/local/libexec/buh-redact-diagnostics
  /usr/local/sbin/buh-platform-v2-receiver
  /etc/sudoers.d/buh-platform-v2
  /usr/local/sbin/buh-deploy-dispatch
)
for managed_target in "${managed_targets[@]}"; do
  [[ "${legacy_receiver}" != "${managed_target}" ]] || {
    echo "Legacy receiver must not overlap a managed receiver target." >&2
    exit 66
  }
done
[[ "${deploy_user}" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || {
  echo "Deploy user name is unsafe." >&2
  exit 66
}
id "${deploy_user}" >/dev/null 2>&1 || {
  echo "Deploy user does not exist." >&2
  exit 67
}

if [[ -n "${reviewed_inventory}" ]]; then
  source_commit="${BUH_REVIEWED_COMMIT:-}"
  [[ "${reviewed_inventory}" == /* && -f "${reviewed_inventory}" && \
    ! -L "${reviewed_inventory}" && "${source_commit}" =~ ^[0-9a-f]{40}$ ]] || {
    echo "Reviewed receiver inventory is invalid." >&2
    exit 65
  }
  [[ "$(stat -c '%u:%a' -- "${reviewed_inventory}")" == "0:600" ]] || {
    echo "Reviewed receiver inventory is not private and root-owned." >&2
    exit 65
  }
  reviewed_entry() {
    awk -F '\t' -v expected="$1" '$2 == expected {print $1}' \
      "${reviewed_inventory}"
  }
else
  [[ "${config_source}" == "${canonical_config}" && \
    "${legacy_source}" == "${canonical_legacy}" ]] || {
    echo "Direct installation inputs must be the canonical live files." >&2
    exit 66
  }
  git_root="$(git -C "${repo_root}" rev-parse --show-toplevel)"
  git_root="$(cd -- "${git_root}" && pwd -P)"
  [[ "${git_root}" == "${repo_root}" ]] || {
    echo "Receiver must be installed from its exact Git checkout." >&2
    exit 65
  }
  git_reviewed() { git -C "${repo_root}" "$@"; }
  reviewed_entry() { git_reviewed ls-tree "${source_commit}" -- "$1"; }
  source_commit="$(git_reviewed rev-parse HEAD)"
  [[ "${source_commit}" =~ ^[0-9a-f]{40}$ ]] || {
    echo "Source checkout does not have a full Git commit identity." >&2
    exit 65
  }
  git_reviewed diff --quiet --
  git_reviewed diff --cached --quiet --
  [[ -z "$(git_reviewed status --porcelain=v1 --untracked-files=all --ignored=matching)" ]] || {
    echo "Receiver source checkout contains tracked, untracked, or ignored changes." >&2
    exit 65
  }
fi

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
  tree_entry="$(reviewed_entry "${relative}")"
  [[ "${tree_entry}" =~ ^100(644|755)[[:space:]]blob[[:space:]]([0-9a-f]{40})([[:space:]]|$) ]]
  expected_object="${BASH_REMATCH[2]}"
  [[ -f "${repo_root}/${relative}" && ! -L "${repo_root}/${relative}" ]]
  if [[ -n "${reviewed_inventory}" ]]; then
    actual_object="$(git hash-object --no-filters -- "${repo_root}/${relative}")"
  else
    actual_object="$(git_reviewed hash-object -- "${repo_root}/${relative}")"
  fi
  [[ "${actual_object}" == "${expected_object}" ]]
done

if [[ -n "${BUH_PINNED_CONFIG_SHA256:-}" ]]; then
  [[ "${BUH_PINNED_CONFIG_SHA256}" =~ ^[0-9a-f]{64}$ && \
    "$(sha256sum "${config_source}" | awk '{print $1}')" == \
      "${BUH_PINNED_CONFIG_SHA256}" ]] || {
    echo "Pinned receiver configuration changed." >&2
    exit 65
  }
fi
if [[ -n "${BUH_PINNED_LEGACY_SHA256:-}" ]]; then
  [[ "${BUH_PINNED_LEGACY_SHA256}" =~ ^[0-9a-f]{64}$ && \
    "$(sha256sum "${legacy_source}" | awk '{print $1}')" == \
      "${BUH_PINNED_LEGACY_SHA256}" ]] || {
    echo "Pinned legacy receiver changed." >&2
    exit 65
  }
fi

PYTHONPATH="${repo_root}" /usr/bin/python3 -P - "${config_source}" <<'PY'
import sys
from pathlib import Path
from ops.deploy.contracts import ReceiverConfig

ReceiverConfig.load(Path(sys.argv[1]))
PY

install -d -m 0755 "${library_dir}/ops/deploy"
install -d -m 0755 "${library_dir}/ops/release"
install -m 0644 "${repo_root}/ops/__init__.py" \
  "${library_dir}/ops/__init__.py"
install -m 0644 "${repo_root}/ops/deploy/__init__.py" \
  "${repo_root}/ops/deploy/contracts.py" \
  "${repo_root}/ops/deploy/docker_host.py" \
  "${repo_root}/ops/deploy/engine.py" \
  "${repo_root}/ops/deploy/receiver.py" \
  "${library_dir}/ops/deploy/"
install -m 0644 "${repo_root}/ops/release/__init__.py" \
  "${repo_root}/ops/release/buh_release.py" \
  "${library_dir}/ops/release/"

install -d -m 0700 "${config_dir}"
install -m 0600 "${config_source}" "${config_dir}/receiver.json"
install -d -m 0755 "$(dirname -- "${receiver_path}")" \
  "$(dirname -- "${observer_entry_path}")" "$(dirname -- "${redactor_path}")"
install -m 0755 "${script_dir}/buh-platform-v2-receiver" \
  "${receiver_path}"
install -m 0755 "${script_dir}/buh-deploy-dispatch" \
  "${dispatcher_path}"
install -m 0755 "${repo_root}/ops/buh-github-observe-entry" \
  "${observer_entry_path}"
install -m 0755 "${repo_root}/ops/buh-github-observe-root" \
  "${observer_root_path}"
install -m 0755 "${repo_root}/ops/buh-redact-diagnostics.py" \
  "${redactor_path}"

provenance_tmp="$(mktemp "${config_dir}/.install.XXXXXX")"
/usr/bin/python3 -P - "${repo_root}" "${config_source}" "${source_commit}" \
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
  "${config_dir}/INSTALL.json"
rm -f -- "${provenance_tmp}"

mapfile -t private_dirs < <(
  PYTHONPATH="${repo_root}" /usr/bin/python3 -P - "${config_source}" <<'PY'
import sys
from pathlib import Path
from ops.deploy.contracts import ReceiverConfig

config = ReceiverConfig.load(Path(sys.argv[1]))
print(config.state_dir)
print(config.backup_dir)
PY
)
[[ "${#private_dirs[@]}" -eq 2 ]]
for private_dir in "${private_dirs[@]}"; do
  install -d -o root -g root -m 0700 "$(rooted "${private_dir}")"
done

install -d -m 0755 "$(dirname -- "${sudoers_path}")"
sudoers_tmp="$(mktemp "$(dirname -- "${sudoers_path}")/.buh-platform-v2.XXXXXX")"
cleanup() {
  rm -f -- "${sudoers_tmp}"
  if [[ -n "${compile_cache}" ]]; then
    rm -rf -- "${compile_cache}"
  fi
}
trap cleanup EXIT
{
  printf '%s ALL=(root) NOPASSWD: %s\n' "${deploy_user}" "${legacy_receiver}"
  printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/buh-platform-v2-receiver preflight\n' "${deploy_user}"
  printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/buh-platform-v2-receiver deploy\n' "${deploy_user}"
} >"${sudoers_tmp}"
chmod 0440 "${sudoers_tmp}"
visudo -cf "${sudoers_tmp}" >/dev/null
install -o root -g root -m 0440 "${sudoers_tmp}" "${sudoers_path}"

compile_cache="$(mktemp -d /var/tmp/buh-receiver-compile.XXXXXXXX)"
chmod 0700 "${compile_cache}"
PYTHONPATH="${library_dir}" PYTHONPYCACHEPREFIX="${compile_cache}" \
  PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -P -m compileall -q \
  "${library_dir}/ops"
rm -rf -- "${compile_cache}"
compile_cache=""

echo "Platform v2 receiver installed but not enabled for SSH."
echo "Review docs/operations/release-and-deployment.md before changing the forced command."
