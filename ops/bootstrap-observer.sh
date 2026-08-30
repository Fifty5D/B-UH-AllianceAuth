#!/usr/bin/env bash
set -Eeuo pipefail

readonly OBSERVER_USER="buh-observer"
readonly OBSERVER_HOME="/var/lib/buh-observer"
readonly ENTRY_SOURCE="${1:-}"
readonly ROOT_SOURCE="${2:-}"
readonly REDACTOR_SOURCE="${3:-}"
readonly PUBLIC_KEY_FILE="${4:-}"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run this bootstrap as root." >&2
    exit 1
fi

for required_file in "${ENTRY_SOURCE}" "${ROOT_SOURCE}" "${REDACTOR_SOURCE}" "${PUBLIC_KEY_FILE}"; do
    if [[ -z "${required_file}" || ! -f "${required_file}" ]]; then
        echo "Usage: bootstrap-observer.sh ENTRY_SCRIPT ROOT_SCRIPT REDACTOR PUBLIC_KEY_FILE" >&2
        exit 64
    fi
done

public_key="$(tr -d '\r\n' <"${PUBLIC_KEY_FILE}")"
if [[ ! "${public_key}" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+([[:space:]].*)?$ ]]; then
    echo "The observer public key must be a valid ssh-ed25519 public key." >&2
    exit 65
fi

if ! id "${OBSERVER_USER}" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "${OBSERVER_HOME}" --shell /bin/bash "${OBSERVER_USER}"
fi
passwd -l "${OBSERVER_USER}" >/dev/null 2>&1 || true

install -o root -g root -m 0755 "${ENTRY_SOURCE}" /usr/local/bin/buh-github-observe-entry
install -o root -g root -m 0755 "${ROOT_SOURCE}" /usr/local/sbin/buh-github-observe-root
install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "${REDACTOR_SOURCE}" /usr/local/libexec/buh-redact-diagnostics

install -d -o "${OBSERVER_USER}" -g "${OBSERVER_USER}" -m 0700 "${OBSERVER_HOME}/.ssh"
authorized_keys="${OBSERVER_HOME}/.ssh/authorized_keys"
printf 'restrict,command="/usr/local/bin/buh-github-observe-entry" %s\n' "${public_key}" >"${authorized_keys}"
chown "${OBSERVER_USER}:${OBSERVER_USER}" "${authorized_keys}"
chmod 0600 "${authorized_keys}"

sudoers_file="/etc/sudoers.d/buh-github-observer"
printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/buh-github-observe-root\n' "${OBSERVER_USER}" >"${sudoers_file}"
chmod 0440 "${sudoers_file}"
visudo -cf "${sudoers_file}" >/dev/null

echo "Observer bridge installed for ${OBSERVER_USER}."
echo "The key is forced to a read-only diagnostics command and cannot open a shell."
