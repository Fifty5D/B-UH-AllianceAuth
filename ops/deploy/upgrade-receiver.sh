#!/usr/bin/env bash
# Reviewed, one-time Platform v2 receiver upgrade with automatic host rollback.
set -Eeuo pipefail
umask 077

die() { printf '%s\n' "$1" >&2; exit 64; }
[[ "$#" -eq 4 ]] || die "Usage: upgrade-receiver.sh COMMIT CONFIG LEGACY_RECEIVER PREFLIGHT_REQUEST"
[[ "$(id -u)" -eq 0 ]] || die "Run this helper as root."

expected_commit="$1"
config="$2"
legacy="$3"
request="$4"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "${script_dir}/../.." && pwd -P)"
[[ "${expected_commit}" =~ ^[0-9a-f]{40}$ ]] || die "Expected commit must be 40 lowercase hex characters."
[[ "$(git -C "${repo_root}" rev-parse HEAD)" == "${expected_commit}" ]] || die "Checkout does not match reviewed commit."
git -C "${repo_root}" diff --quiet -- || die "Checkout has uncommitted changes."
git -C "${repo_root}" diff --cached --quiet -- || die "Checkout index has uncommitted changes."
git -C "${repo_root}" ls-files --error-unmatch -- ops/deploy/upgrade-receiver.sh >/dev/null
for path in "${config}" "${legacy}" "${request}"; do
  [[ "${path}" == /* && -f "${path}" && ! -L "${path}" ]] || die "Every input must be an absolute regular non-symlink file."
done
[[ -x "${legacy}" ]] || die "Legacy receiver must remain executable."

PYTHONPATH="${repo_root}" python3 -m unittest discover -s tests/deploy -p 'test_*.py'

backup="$(mktemp -d /var/backups/buh-receiver-upgrade.XXXXXXXX)"
chmod 0700 "${backup}"
installed=(
  /usr/local/lib/buh-platform-v2
  /usr/local/sbin/buh-platform-v2-receiver
  /usr/local/sbin/buh-deploy-dispatch
  /usr/local/bin/buh-github-observe-entry
  /usr/local/sbin/buh-github-observe-root
  /usr/local/libexec/buh-redact-diagnostics
  /etc/buh-platform-v2
  /etc/sudoers.d/buh-platform-v2
)
printf '%s\0' "${installed[@]}" >"${backup}/paths"
tar --create --null --files-from="${backup}/paths" --absolute-names \
  --ignore-failed-read --file="${backup}/receiver.tar"

restore() {
  status=$?
  trap - ERR
  if [[ "${status}" -ne 0 ]]; then
    # Remove only v2 targets. Legacy receiver and SSH identity files are untouched.
    rm -rf -- /usr/local/lib/buh-platform-v2 /etc/buh-platform-v2
    rm -f -- /usr/local/sbin/buh-platform-v2-receiver \
      /usr/local/sbin/buh-deploy-dispatch /usr/local/bin/buh-github-observe-entry \
      /usr/local/sbin/buh-github-observe-root /usr/local/libexec/buh-redact-diagnostics \
      /etc/sudoers.d/buh-platform-v2
    tar --extract --absolute-names --file="${backup}/receiver.tar"
    printf 'Receiver upgrade failed; the previous receiver was restored. Backup: %s\n' "${backup}" >&2
  fi
  exit "${status}"
}
trap restore ERR

"${script_dir}/install-receiver.sh" "${config}" "${legacy}"
/usr/local/sbin/buh-platform-v2-receiver preflight <"${request}" >/dev/null
trap - ERR
printf 'Receiver upgraded and no-change preflight passed at commit %s. Backup: %s\n' \
  "${expected_commit}" "${backup}"
