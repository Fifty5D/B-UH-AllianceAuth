#!/usr/bin/env bash
# Reviewed, one-time Platform v2 receiver upgrade with exact host rollback.
set -Eeuo pipefail
umask 077

die() { printf '%s\n' "$1" >&2; exit 64; }
[[ "$#" -eq 4 ]] || die "Usage: upgrade-receiver.sh COMMIT CONFIG LEGACY_RECEIVER PREFLIGHT_REQUEST"
[[ "$(id -u)" -eq 0 ]] || die "Run this helper as root."

expected_commit="$1"
config="$2"
legacy="$3"
request="$4"
config_path="${BUH_RECEIVER_CONFIG_PATH:-}"
legacy_receiver_path="${BUH_LEGACY_RECEIVER_PATH:-}"
canonical_config=/etc/buh-platform-v2/receiver.json
canonical_legacy=/usr/local/sbin/buh-moon-tax-platform-remote
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "${script_dir}/../.." && pwd -P)"
reviewed_inventory="${BUH_REVIEWED_INVENTORY:-}"
reviewed_inventory_sha256="${BUH_REVIEWED_INVENTORY_SHA256:-}"

[[ "${expected_commit}" =~ ^[0-9a-f]{40}$ ]] || \
  die "Expected commit must be 40 lowercase hex characters."
[[ -n "${reviewed_inventory}" && \
  "${BUH_REVIEWED_COMMIT:-}" == "${expected_commit}" ]] || \
  die "Run only from the root-private reviewed receiver bootstrap."
[[ "${reviewed_inventory}" == /* && -f "${reviewed_inventory}" && \
  ! -L "${reviewed_inventory}" ]] || die "Reviewed tree inventory is invalid."
[[ "$(stat -c '%u:%a' -- "${reviewed_inventory}")" == "0:600" ]] || \
  die "Reviewed tree inventory is not private and root-owned."
[[ "${reviewed_inventory_sha256}" =~ ^[0-9a-f]{64}$ ]] || \
  die "Reviewed tree inventory identity is missing."
[[ "${config_path}" == "${canonical_config}" && \
  "${legacy_receiver_path}" == "${canonical_legacy}" ]] || \
  die "Receiver configuration and legacy receiver paths must be canonical."

verify_reviewed_tree() {
  [[ "$(sha256sum "${reviewed_inventory}" | awk '{print $1}')" == \
    "${reviewed_inventory_sha256}" ]] || \
    die "Reviewed tree inventory changed."
  [[ "$(stat -c '%u' -- "${repo_root}")" == 0 && ! -L "${repo_root}" ]] || \
    die "Exported receiver source is not root-owned."
  [[ -z "$(find "${repo_root}" -perm /022 -print -quit)" ]] || \
    die "Reviewed receiver source is writable outside root."
  [[ -z "$(find "${repo_root}" -type l -print -quit)" ]] || \
    die "Exported receiver source contains a symbolic link."
  diff -q \
    <(awk -F '\t' '{print $2}' "${reviewed_inventory}" | LC_ALL=C sort) \
    <(cd -- "${repo_root}" && find . -type f -printf '%P\n' | LC_ALL=C sort) \
    >/dev/null || \
    die "Exported receiver source does not exactly match the reviewed tree."
  while IFS=$'\t' read -r tree_entry relative; do
    [[ "${tree_entry}" =~ ^100(644|755)[[:space:]]blob[[:space:]]([0-9a-f]{40})$ ]] || \
      die "A reviewed source inventory entry is invalid."
    expected_mode="${BASH_REMATCH[1]}"
    expected_object="${BASH_REMATCH[2]}"
    [[ "${relative}" =~ ^[A-Za-z0-9._/-]+$ && \
      -f "${repo_root}/${relative}" && ! -L "${repo_root}/${relative}" ]] || \
      die "A reviewed source is missing or unsafe."
    [[ "$(stat -c '%a' -- "${repo_root}/${relative}")" == "${expected_mode}" && \
      "$(git hash-object --no-filters -- "${repo_root}/${relative}")" == \
        "${expected_object}" ]] || \
      die "A reviewed source differs from its exact inventory."
  done <"${reviewed_inventory}"
}

verify_reviewed_tree

for path in "${config}" "${legacy}" "${request}"; do
  [[ "${path}" == /* && "${path}" != *"/../"* && -f "${path}" && ! -L "${path}" ]] || \
    die "Every input must be an absolute regular non-symlink file."
  [[ "$(stat -c '%u' -- "${path}")" == 0 ]] || \
    die "Every input must be root-owned."
done
[[ -x "${legacy}" ]] || die "Legacy receiver must remain executable."
[[ -f "${config_path}" && ! -L "${config_path}" && \
  "$(stat -c '%u:%a' -- "${config_path}")" == "0:600" ]] || \
  die "Canonical receiver configuration is unsafe."
[[ "${legacy_receiver_path}" == /* && \
  "${legacy_receiver_path}" != *"/../"* && \
  -f "${legacy_receiver_path}" && ! -L "${legacy_receiver_path}" && \
  -x "${legacy_receiver_path}" && \
  "$(stat -c '%u' -- "${legacy_receiver_path}")" == 0 && \
  "$((8#$(stat -c '%a' -- "${legacy_receiver_path}") & 8#022))" == 0 ]] || \
  die "Legacy receiver logical path is unsafe."
[[ "$((8#$(stat -c '%a' -- "${config}") & 8#077))" == 0 && \
  "$((8#$(stat -c '%a' -- "${request}") & 8#077))" == 0 && \
  "$((8#$(stat -c '%a' -- "${legacy}") & 8#022))" == 0 ]] || \
  die "Receiver input permissions are unsafe."

for input_hash in BUH_PINNED_CONFIG_SHA256 BUH_PINNED_LEGACY_SHA256 \
  BUH_PINNED_REQUEST_SHA256; do
  [[ "${!input_hash:-}" =~ ^[0-9a-f]{64}$ ]] || \
    die "Pinned receiver input identity is missing."
done
[[ "$(sha256sum "${config}" | awk '{print $1}')" == \
  "${BUH_PINNED_CONFIG_SHA256}" && \
  "$(sha256sum "${legacy}" | awk '{print $1}')" == \
  "${BUH_PINNED_LEGACY_SHA256}" && \
  "$(sha256sum "${request}" | awk '{print $1}')" == \
  "${BUH_PINNED_REQUEST_SHA256}" && \
  "$(sha256sum "${config_path}" | awk '{print $1}')" == \
  "${BUH_PINNED_CONFIG_SHA256}" && \
  "$(sha256sum "${legacy_receiver_path}" | awk '{print $1}')" == \
  "${BUH_PINNED_LEGACY_SHA256}" ]] || die "Pinned receiver input changed."

# Run the complete host-independent deployment suite before any installed path is
# changed. A sanitized environment and -P keep the caller's working directory and
# Python environment outside the root trust boundary.
python_environment=(
  env -i
  HOME=/root
  LANG=C.UTF-8
  PATH=/usr/sbin:/usr/bin:/sbin:/bin
  PYTHONPATH="${repo_root}"
  PYTHONDONTWRITEBYTECODE=1
  GIT_CONFIG_NOSYSTEM=1
  GIT_CONFIG_GLOBAL=/dev/null
)
python_environment+=(
  BUH_REVIEWED_INVENTORY="${reviewed_inventory}"
  BUH_REVIEWED_INVENTORY_SHA256="${reviewed_inventory_sha256}"
  BUH_REVIEWED_COMMIT="${expected_commit}"
  BUH_RECEIVER_CONFIG_PATH="${config_path}"
  BUH_PINNED_CONFIG_SHA256="${BUH_PINNED_CONFIG_SHA256}"
  BUH_PINNED_LEGACY_SHA256="${BUH_PINNED_LEGACY_SHA256}"
  BUH_PINNED_REQUEST_SHA256="${BUH_PINNED_REQUEST_SHA256}"
  BUH_LEGACY_RECEIVER_PATH="${legacy_receiver_path}"
)
"${python_environment[@]}" python3 -P -m unittest discover \
  -s "${repo_root}/tests/deploy" -t "${repo_root}" -p 'test_*.py'

[[ "$(sha256sum "${config}" | awk '{print $1}')" == \
  "${BUH_PINNED_CONFIG_SHA256}" && \
  "$(sha256sum "${legacy}" | awk '{print $1}')" == \
  "${BUH_PINNED_LEGACY_SHA256}" && \
  "$(sha256sum "${request}" | awk '{print $1}')" == \
  "${BUH_PINNED_REQUEST_SHA256}" && \
  "$(sha256sum "${config_path}" | awk '{print $1}')" == \
  "${BUH_PINNED_CONFIG_SHA256}" && \
  "$(sha256sum "${legacy_receiver_path}" | awk '{print $1}')" == \
  "${BUH_PINNED_LEGACY_SHA256}" ]] || die "Pinned receiver input changed during tests."
verify_reviewed_tree

[[ "$(sha256sum "${config_path}" | awk '{print $1}')" == \
  "${BUH_PINNED_CONFIG_SHA256}" && \
  "$(sha256sum "${legacy_receiver_path}" | awk '{print $1}')" == \
  "${BUH_PINNED_LEGACY_SHA256}" ]] || \
  die "Canonical receiver input changed before activation."

"${python_environment[@]}" python3 -P -m ops.deploy.receiver_upgrade upgrade \
  "${expected_commit}" "${repo_root}" "${config}" "${legacy}" "${request}"
