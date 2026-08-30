#!/usr/bin/env bash
set -Eeuo pipefail

readonly APP_DIR="/opt/aa-docker"
readonly SETTINGS_FILE="${APP_DIR}/conf/local.py"
readonly PYTHON_HELPER="${1:-}"
readonly MANAGE_PY="/home/allianceauth/myauth/manage.py"
readonly SERVICES=(
    allianceauth_gunicorn
    allianceauth_worker
    allianceauth_worker_services
    allianceauth_beat
)

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run the token rotation through the configured root SSH target." >&2
    exit 1
fi
if [[ -z "${PYTHON_HELPER}" || ! -f "${PYTHON_HELPER}" ]]; then
    echo "The token rotation helper is missing." >&2
    exit 64
fi
if [[ ! -f "${SETTINGS_FILE}" || ! -f "${APP_DIR}/docker-compose.yml" || ! -f "${APP_DIR}/.env" ]]; then
    echo "Alliance Auth Docker was not found at ${APP_DIR}." >&2
    exit 69
fi

umask 077
token_file="$(mktemp -t buh-discord-token.XXXXXX)"
backup_dir="${APP_DIR}/conf/buh-token-rotation-backups"
timestamp="$(date -u '+%Y%m%d-%H%M%S')"
backup_file="${backup_dir}/local.py.${timestamp}"
changed=0

cleanup() {
    rm -f -- "${token_file}"
}
trap cleanup EXIT

IFS= read -r new_token
printf '%s' "${new_token}" >"${token_file}"
unset new_token

install -d -o root -g root -m 0700 "${backup_dir}"
cp -a -- "${SETTINGS_FILE}" "${backup_file}"

cd "${APP_DIR}"
compose() {
    docker compose --env-file=.env "$@"
}

rollback() {
    if ((changed)); then
        echo "Restoring the previous local.py configuration..." >&2
        cp -a -- "${backup_file}" "${SETTINGS_FILE}"
        compose up -d --force-recreate "${SERVICES[@]}" >/dev/null 2>&1 || true
    fi
}
trap 'rollback' ERR

setting_name="$(python3 "${PYTHON_HELPER}" "${SETTINGS_FILE}" "${token_file}")"
changed=1
rm -f -- "${token_file}"

echo "Updated ${setting_name}; running the Django system check..."
compose run --rm --no-deps --entrypoint python3 allianceauth_gunicorn \
    "${MANAGE_PY}" check

echo "Recreating Alliance Auth services with the new Discord token..."
compose up -d --force-recreate "${SERVICES[@]}"

deadline=$((SECONDS + 180))
while ((SECONDS < deadline)); do
    all_ready=1
    for service in "${SERVICES[@]}"; do
        mapfile -t ids < <(compose ps -q "${service}" | sed '/^$/d')
        if ((${#ids[@]} == 0)); then
            all_ready=0
            break
        fi
        for container_id in "${ids[@]}"; do
            state="$(docker inspect --format '{{.State.Status}}' "${container_id}")"
            health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${container_id}")"
            if [[ "${state}" != "running" || "${health}" == "unhealthy" || "${health}" == "starting" ]]; then
                all_ready=0
                break 2
            fi
        done
    done
    if ((all_ready)); then
        changed=0
        trap - ERR
        echo "Discord token rotated and all Auth services are running."
        echo "Backup retained at ${backup_file}"
        compose ps "${SERVICES[@]}"
        exit 0
    fi
    sleep 3
done

echo "Auth services did not become ready within 180 seconds." >&2
false
