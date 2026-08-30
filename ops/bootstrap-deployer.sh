#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly DEPLOY_USER="buh-deployer"
readonly DEPLOY_HOME="/var/lib/buh-deployer"
readonly ENTRY_SOURCE="${1:-}"
readonly ROOT_SOURCE="${2:-}"
readonly PUBLIC_KEY_FILE="${3:-}"
readonly AUTH_DIR="${4:-/opt/aa-docker}"
readonly ADMIN_CHARACTER="${5:-Fifty5D}"
readonly PAYMENT_CORPORATION="${6:-Bureau of Unified Harvesting}"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run this bootstrap as root." >&2
    exit 1
fi
for required_file in "${ENTRY_SOURCE}" "${ROOT_SOURCE}" "${PUBLIC_KEY_FILE}"; do
    if [[ -z "${required_file}" || ! -f "${required_file}" ]]; then
        echo "Usage: bootstrap-deployer.sh ENTRY ROOT PUBLIC_KEY [AUTH_DIR ADMIN CORP]" >&2
        exit 64
    fi
done
if [[ ! "${AUTH_DIR}" =~ ^/[A-Za-z0-9._/-]+$ \
    || ! "${ADMIN_CHARACTER}" =~ ^[A-Za-z0-9._\ -]+$ \
    || -z "${PAYMENT_CORPORATION}" || ${#PAYMENT_CORPORATION} -gt 255 \
    || "${PAYMENT_CORPORATION}" == *$'\n'* ]]; then
    echo "The deployment configuration contains unsupported characters." >&2
    exit 65
fi

public_key="$(tr -d '\r\n' <"${PUBLIC_KEY_FILE}")"
if [[ ! "${public_key}" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+([[:space:]].*)?$ ]]; then
    echo "The deploy public key must be a valid ssh-ed25519 public key." >&2
    exit 65
fi

if ! id "${DEPLOY_USER}" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "${DEPLOY_HOME}" --shell /bin/bash \
        "${DEPLOY_USER}"
fi
passwd -l "${DEPLOY_USER}" >/dev/null 2>&1 || true

install -o root -g root -m 0755 "${ENTRY_SOURCE}" /usr/local/bin/buh-github-deploy-entry
install -o root -g root -m 0755 "${ROOT_SOURCE}" /usr/local/sbin/buh-github-deploy-root
install -d -o root -g root -m 0755 "${DEPLOY_HOME}"

config_temp="$(mktemp)"
printf 'AUTH_DIR=%s\nADMIN_CHARACTER=%s\nPAYMENT_CORPORATION=%s\n' \
    "${AUTH_DIR}" "${ADMIN_CHARACTER}" "${PAYMENT_CORPORATION}" >"${config_temp}"
install -o root -g root -m 0600 "${config_temp}" /etc/buh-github-deployer.conf
rm -f -- "${config_temp}"

install -d -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" -m 0700 "${DEPLOY_HOME}/.ssh"
authorized_keys="${DEPLOY_HOME}/.ssh/authorized_keys"
printf 'restrict,command="/usr/local/bin/buh-github-deploy-entry" %s\n' \
    "${public_key}" >"${authorized_keys}"
chown "${DEPLOY_USER}:${DEPLOY_USER}" "${authorized_keys}"
chmod 0600 "${authorized_keys}"

sudoers_file="/etc/sudoers.d/buh-github-deployer"
printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/buh-github-deploy-root\n' \
    "${DEPLOY_USER}" >"${sudoers_file}"
chmod 0440 "${sudoers_file}"
visudo -cf "${sudoers_file}" >/dev/null

echo "Guarded GitHub deployer installed for ${DEPLOY_USER}."
echo "The key is forced to one checked Moon Tax deployment command and cannot open a shell."
