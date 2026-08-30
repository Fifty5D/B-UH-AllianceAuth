#!/usr/bin/env bash
set -Eeuo pipefail

VERSION="0.3.0"
MODE="${1:-status}"
APP_DIR="${2:-/opt/aa-docker}"
ADMIN_CHARACTER="${3:-Fifty5D}"
PAYMENT_CORPORATION="${4:-Bureau of Unified Harvesting}"
MANAGE_PY="/home/allianceauth/myauth/manage.py"
LOG_FILE="${APP_DIR}/buh-moon-tax-platform-v${VERSION}.log"
BACKUP_ROOT="${APP_DIR}/conf/buh-moon-tax-platform-backups"

STRUCTURES_WHEEL="aa_structures-4.0.3-py3-none-any.whl"
MOON_WHEEL="aa_moonmining-3.1.0.post1-py3-none-any.whl"
OPS_WHEEL="aa_buh_structure_ops-0.3.0-py3-none-any.whl"
ANALYTICS_WHEEL="aa_buh_mining_analytics-0.2.0-py3-none-any.whl"
TAX_WHEEL="aa_buh_moon_tax-0.3.0-py3-none-any.whl"

STRUCTURES_SHA256="ba8493ef10a450198fa5ba3d8995e1ea661b74dca20d770beaa91bf005f558e3"
MOON_SHA256="aa4ddbc7da64151cd6d988d971d59462360e35a5111d47a3bf56d577f454bc13"
OPS_SHA256="02418557b0e6f44bb8b954dae34894985c09c411b08e75236d5f118b980e5d16"
ANALYTICS_SHA256="5716cb36b6ab1d476734da057d730361b5e27873b1927822586c626d2794c5bb"
TAX_SHA256="ee8ec1bb84cdf969e30c9f0353008d4377b3f17cda1eadc4ee7454458fe98890"

AUTH_SERVICES=(
    allianceauth_gunicorn
    allianceauth_worker
    allianceauth_worker_services
    allianceauth_beat
)

cleanup() {
    case "$0" in
        /tmp/buh-moon-tax-platform-remote-v0.3.0.sh)
            rm -f -- "$0" \
                "/tmp/${STRUCTURES_WHEEL}" \
                "/tmp/${MOON_WHEEL}" \
                "/tmp/${OPS_WHEEL}" \
                "/tmp/${ANALYTICS_WHEEL}" \
                "/tmp/${TAX_WHEEL}"
            ;;
    esac
}
trap cleanup EXIT

if [[ "$(id -u)" -ne 0 ]]; then
    echo "This utility must run as root. Configure ssh b-uh to connect as root."
    exit 1
fi
if [[ ! "${APP_DIR}" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    echo "Unsupported Alliance Auth directory: ${APP_DIR}"
    exit 1
fi
if [[ ! "${ADMIN_CHARACTER}" =~ ^[A-Za-z0-9._\ -]+$ ]]; then
    echo "The administrator character contains unsupported characters."
    exit 1
fi
if [[ -z "${PAYMENT_CORPORATION}" || ${#PAYMENT_CORPORATION} -gt 255 \
    || "${PAYMENT_CORPORATION}" == *$'\n'* \
    || "${PAYMENT_CORPORATION}" == *$'\r'* ]]; then
    echo "The payment corporation contains unsupported characters."
    exit 1
fi
if [[ ! -d "${APP_DIR}" || ! -f "${APP_DIR}/docker-compose.yml" ]]; then
    echo "Alliance Auth Docker was not found at ${APP_DIR}."
    exit 1
fi

cd "${APP_DIR}"

compose() {
    docker compose --env-file=.env "$@"
}

require_live_auth() {
    local container_id status
    container_id="$(compose ps -q allianceauth_gunicorn | head -n 1)"
    if [[ -z "${container_id}" ]]; then
        echo "The Alliance Auth Gunicorn container does not exist."
        return 1
    fi
    status="$(docker inspect --format '{{.State.Status}}' "${container_id}")"
    if [[ "${status}" != "running" ]]; then
        echo "Alliance Auth Gunicorn is ${status}, not running."
        return 1
    fi
}

run_manage_image() {
    compose run --rm --no-deps --entrypoint python3 allianceauth_gunicorn \
        "${MANAGE_PY}" "$@"
}

run_manage_live() {
    require_live_auth
    compose exec -T allianceauth_gunicorn python3 "${MANAGE_PY}" "$@"
}

check_auth_containers() {
    local service container_id status restart_count found
    for service in "${AUTH_SERVICES[@]}"; do
        found=0
        while IFS= read -r container_id; do
            [[ -z "${container_id}" ]] && continue
            found=1
            status="$(docker inspect --format '{{.State.Status}}' "${container_id}")"
            restart_count="$(docker inspect --format '{{.RestartCount}}' "${container_id}")"
            if [[ "${status}" != "running" || "${restart_count}" -ne 0 ]]; then
                return 1
            fi
        done < <(compose ps -q "${service}")
        if [[ "${found}" -ne 1 ]]; then
            return 1
        fi
    done
}

verify_uploaded_wheels() {
    local wheel hash
    while read -r wheel hash; do
        if [[ ! -f "/tmp/${wheel}" ]]; then
            echo "Uploaded wheel not found: /tmp/${wheel}"
            return 1
        fi
        printf '%s  %s\n' "${hash}" "/tmp/${wheel}" | sha256sum --check --strict
    done <<EOF
${STRUCTURES_WHEEL} ${STRUCTURES_SHA256}
${MOON_WHEEL} ${MOON_SHA256}
${OPS_WHEEL} ${OPS_SHA256}
${ANALYTICS_WHEEL} ${ANALYTICS_SHA256}
${TAX_WHEEL} ${TAX_SHA256}
EOF
}

write_dockerfile_block() {
    python3 - "${APP_DIR}/custom.dockerfile" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
begin = "# BEGIN B-UH MOON TAX PLATFORM"
end = "# END B-UH MOON TAX PLATFORM"
if text.count(begin) != text.count(end) or text.count(begin) > 1:
    raise SystemExit("The Moon Tax Platform Dockerfile markers are malformed.")
block = """# BEGIN B-UH MOON TAX PLATFORM
COPY conf/aa_structures-4.0.3-py3-none-any.whl /tmp/
COPY conf/aa_moonmining-3.1.0.post1-py3-none-any.whl /tmp/
COPY conf/aa_buh_structure_ops-0.3.0-py3-none-any.whl /tmp/
COPY conf/aa_buh_mining_analytics-0.2.0-py3-none-any.whl /tmp/
COPY conf/aa_buh_moon_tax-0.3.0-py3-none-any.whl /tmp/
RUN python3 -m pip install --no-cache-dir --upgrade \\
    /tmp/aa_structures-4.0.3-py3-none-any.whl \\
    /tmp/aa_moonmining-3.1.0.post1-py3-none-any.whl \\
    /tmp/aa_buh_structure_ops-0.3.0-py3-none-any.whl \\
    /tmp/aa_buh_mining_analytics-0.2.0-py3-none-any.whl \\
    /tmp/aa_buh_moon_tax-0.3.0-py3-none-any.whl
# END B-UH MOON TAX PLATFORM
"""
pattern = re.compile(
    r"(?ms)^# BEGIN B-UH MOON TAX PLATFORM\n.*?^# END B-UH MOON TAX PLATFORM\n?"
)
if pattern.search(text):
    text = pattern.sub(block, text)
else:
    text = text.rstrip() + "\n\n" + block
path.write_text(text, encoding="utf-8")
PY
}

write_local_settings_block() {
    python3 - "${APP_DIR}/conf/local.py" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
begin = "# BEGIN B-UH MOON TAX PLATFORM"
end = "# END B-UH MOON TAX PLATFORM"
if text.count(begin) != text.count(end) or text.count(begin) > 1:
    raise SystemExit("The Moon Tax Platform local.py markers are malformed.")
block = '''# BEGIN B-UH MOON TAX PLATFORM
# Shared B-UH platform: Structure Operations, Mining Analytics, and Moon Tax.
for _buh_platform_app in (
    "eveuniverse",
    "structures",
    "moonmining",
    "memberaudit",
    "buh_structure_ops",
    "buh_mining_analytics",
    "buh_moon_tax",
):
    if _buh_platform_app not in INSTALLED_APPS:
        INSTALLED_APPS += [_buh_platform_app]

LOGIN_TOKEN_SCOPES = sorted(
    set(globals().get("LOGIN_TOKEN_SCOPES", ()))
    | {
        "esi-wallet.read_character_wallet.v1",
        "esi-contracts.read_character_contracts.v1",
    }
)

# Operational safeguards. The audit interval itself remains editable in Auth admin.
BUH_MOON_TAX_SOURCE_SETTLE_SECONDS = 180
BUH_MOON_TAX_MAX_REFRESH_CHARACTERS = 5000
BUH_MOON_TAX_STALE_AUDIT_HOURS = 2
BUH_MOON_TAX_PRICE_CACHE_HOURS = 24

if "CELERYBEAT_SCHEDULE" not in globals():
    CELERYBEAT_SCHEDULE = {}

_buh_tax_task = "buh_moon_tax.tasks.run_scheduled_audit"
for _buh_schedule_name, _buh_schedule in list(CELERYBEAT_SCHEDULE.items()):
    if isinstance(_buh_schedule, dict) and _buh_schedule.get("task") == _buh_tax_task:
        del CELERYBEAT_SCHEDULE[_buh_schedule_name]

# Hourly guard: the editable database setting decides whether an audit is due.
CELERYBEAT_SCHEDULE["buh-moon-tax-hourly-guard"] = {
    "task": _buh_tax_task,
    "schedule": 3600,
}
del _buh_platform_app, _buh_tax_task
# END B-UH MOON TAX PLATFORM
'''
pattern = re.compile(
    r"(?ms)^# BEGIN B-UH MOON TAX PLATFORM\n.*?^# END B-UH MOON TAX PLATFORM\n?"
)
if pattern.search(text):
    text = pattern.sub(block, text)
else:
    text = text.rstrip() + "\n\n" + block
path.write_text(text, encoding="utf-8")
PY
}

verify_image_versions() {
    local service output expected
    expected="5.2.0|9.6.0|5.0.4|4.0.3|3.1.0.post1|0.3.0|0.2.0|0.3.0"
    for service in "${AUTH_SERVICES[@]}"; do
        output="$({
            compose run --rm --no-deps --entrypoint python3 "${service}" -c \
                "from importlib.metadata import version; print('|'.join(version(p) for p in ('allianceauth','django-esi','aa-memberaudit','aa-structures','aa-moonmining','aa-buh-structure-ops','aa-buh-mining-analytics','aa-buh-moon-tax')))"
        } | tail -n 1 | tr -d '\r')"
        if [[ "${output}" != "${expected}" ]]; then
            echo "${service} contains ${output:-no versions}; expected ${expected}."
            return 1
        fi
        echo "${service}: ${output}"
    done
}

queue_discord_role_refresh() {
    run_manage_live shell -c \
        "from allianceauth.services.modules.discord.tasks import update_all_groups; result=update_all_groups.delay(); print('Queued Discord role refresh:', result.id)"
}

queue_structure_refresh() {
    run_manage_live shell -c \
        "from buh_structure_ops.tasks import queue_source_refreshes; result=queue_source_refreshes.delay(); print('Queued structure source refresh:', result.id)"
}

queue_tax_audit() {
    run_manage_live buh_moon_tax_audit --no-color
}

run_shared_setup_image() {
    run_manage_image buh_structure_ops_setup \
        --admin-character "${ADMIN_CHARACTER}" --no-color
    run_manage_image buh_mining_setup \
        --admin-character "${ADMIN_CHARACTER}" --site-wide --no-color
    run_manage_image buh_moon_tax_setup \
        --payment-character "${ADMIN_CHARACTER}" \
        --payment-corporation "${PAYMENT_CORPORATION}" --no-color
}

run_shared_setup_live() {
    run_manage_live buh_structure_ops_setup \
        --admin-character "${ADMIN_CHARACTER}" --no-color
    run_manage_live buh_mining_setup \
        --admin-character "${ADMIN_CHARACTER}" --site-wide --no-color
    run_manage_live buh_moon_tax_setup \
        --payment-character "${ADMIN_CHARACTER}" \
        --payment-corporation "${PAYMENT_CORPORATION}" --no-color
}

show_status() {
    echo "B-UH Moon Tax Platform status"
    echo "Generated: $(date --iso-8601=seconds)"
    echo
    compose ps -a
    echo
    run_manage_live shell -c \
        "from importlib.metadata import version; print('Versions:', ' | '.join(f'{p}={version(p)}' for p in ('allianceauth','aa-memberaudit','aa-structures','aa-moonmining','aa-buh-structure-ops','aa-buh-mining-analytics','aa-buh-moon-tax')))"
    echo
    run_manage_live buh_moon_tax_status --no-color
    echo
    run_manage_live buh_structure_ops_status --no-color
    echo
    run_manage_live shell -c \
        "from django.conf import settings; wanted={'buh_moon_tax.tasks.run_scheduled_audit','structures.tasks.update_all_structures','structures.tasks.fetch_all_notifications','moonmining.tasks.run_regular_updates','buh_structure_ops.tasks.capture_and_evaluate'}; print('Effective relevant schedules:'); [print(f\"  {name}: {item.get('task')} every {item.get('schedule')}\") for name,item in settings.CELERYBEAT_SCHEDULE.items() if isinstance(item,dict) and item.get('task') in wanted]"
    echo
    df -h "${APP_DIR}"
}

show_logs() {
    echo "B-UH Moon Tax Platform recent logs"
    echo "Generated: $(date --iso-8601=seconds)"
    echo
    compose ps -a
    echo
    compose logs --since=45m --tail=500 --no-color \
        allianceauth_gunicorn allianceauth_worker allianceauth_worker_services allianceauth_beat
}

discover_recipients() {
    run_manage_live buh_moon_tax_setup \
        --payment-character "${ADMIN_CHARACTER}" \
        --payment-corporation "${PAYMENT_CORPORATION}" --no-color
    run_manage_live buh_moon_tax_status --no-color
}

repair_access() {
    run_manage_live buh_structure_ops_setup \
        --admin-character "${ADMIN_CHARACTER}" --no-color
    run_manage_live buh_mining_setup \
        --admin-character "${ADMIN_CHARACTER}" --site-wide --no-color
    run_manage_live buh_moon_tax_setup \
        --payment-character "${ADMIN_CHARACTER}" \
        --payment-corporation "${PAYMENT_CORPORATION}" \
        --repair-default-access --no-color
    if ! queue_discord_role_refresh; then
        echo "WARNING: Discord role refresh could not be queued; Auth access was still repaired."
    fi
}

install_or_update() {
    local timestamp backup_dir live_swapped healthy
    timestamp="$(date +%Y%m%d-%H%M%S)-$$"
    backup_dir="${BACKUP_ROOT}/${timestamp}"
    live_swapped=0

    verify_uploaded_wheels
    mkdir -p "${backup_dir}"
    cp "${APP_DIR}/custom.dockerfile" "${backup_dir}/custom.dockerfile"
    cp "${APP_DIR}/conf/local.py" "${backup_dir}/local.py"

    exec > >(tee -a "${LOG_FILE}") 2>&1

    rollback() {
        local exit_code=$?
        trap - ERR
        set +e
        echo
        echo "Installation failed. Restoring the previous Auth configuration..."
        cp "${backup_dir}/custom.dockerfile" "${APP_DIR}/custom.dockerfile"
        cp "${backup_dir}/local.py" "${APP_DIR}/conf/local.py"
        if [[ "${live_swapped}" -eq 1 ]]; then
            compose build --progress=plain
            compose up -d --no-deps --force-recreate "${AUTH_SERVICES[@]}"
            sleep 8
            compose restart nginx
            compose ps -a
        else
            echo "The live Auth containers were not replaced."
        fi
        echo "Backup retained at: ${backup_dir}"
        echo "Log retained at: ${LOG_FILE}"
        exit "${exit_code}"
    }
    trap rollback ERR

    echo "B-UH Moon Tax Platform installer/updater v${VERSION}"
    echo "Backup: ${backup_dir}"
    cp "/tmp/${STRUCTURES_WHEEL}" "${APP_DIR}/conf/${STRUCTURES_WHEEL}"
    cp "/tmp/${MOON_WHEEL}" "${APP_DIR}/conf/${MOON_WHEEL}"
    cp "/tmp/${OPS_WHEEL}" "${APP_DIR}/conf/${OPS_WHEEL}"
    cp "/tmp/${ANALYTICS_WHEEL}" "${APP_DIR}/conf/${ANALYTICS_WHEEL}"
    cp "/tmp/${TAX_WHEEL}" "${APP_DIR}/conf/${TAX_WHEEL}"

    echo "Updating the custom Docker image recipe and local.py..."
    write_dockerfile_block
    write_local_settings_block

    echo "Validating Docker Compose..."
    compose config >/dev/null

    echo "Building all Auth images while the live site remains online..."
    compose build --progress=plain

    echo "Verifying exact versions in every new Auth image..."
    verify_image_versions

    echo "Running Django checks, migrations, static collection, and setup..."
    run_manage_image check --no-color
    run_manage_image migrate --noinput --no-color
    run_manage_image collectstatic --noinput --no-color
    run_shared_setup_image
    run_manage_image buh_moon_tax_status --no-color

    echo "Replacing only the Alliance Auth application containers..."
    live_swapped=1
    compose up -d --no-deps --force-recreate "${AUTH_SERVICES[@]}"

    healthy=0
    for _attempt in {1..24}; do
        sleep 5
        if check_auth_containers; then
            healthy=1
            break
        fi
    done
    if [[ "${healthy}" -ne 1 ]]; then
        echo "The new Auth containers did not become stable within 120 seconds."
        compose ps -a
        false
    fi

    run_manage_live check --no-color
    if compose logs --since=5m --no-color "${AUTH_SERVICES[@]}" \
        | grep -Eqi 'ModuleNotFoundError|Worker failed to boot|ImproperlyConfigured|ImportError:|SyntaxError:'; then
        echo "A fatal startup pattern was found in the new Auth logs."
        compose logs --since=5m --tail=220 --no-color "${AUTH_SERVICES[@]}"
        false
    fi

    echo "Restarting nginx after Gunicorn recreation..."
    compose restart nginx
    sleep 3
    if ! queue_discord_role_refresh; then
        echo "WARNING: Discord role refresh could not be queued."
    fi
    if ! queue_structure_refresh; then
        echo "WARNING: Structure source refresh could not be queued."
    fi
    if ! queue_tax_audit; then
        echo "WARNING: The first Moon Tax audit was not queued; use launcher option 3."
    fi
    compose ps -a

    trap - ERR
    echo
    echo "B-UH Moon Tax Platform v${VERSION} installed successfully."
    echo "Every known Athanor has an editable tax rate and payment-recipient checklist."
    echo "Recipient discovery includes Auth-linked characters and represented corporations."
    echo "Your customized role names and permission matrices are preserved by updates."
    echo "Row Accept, drag-or-click decisions, and guarded batch actions share one tested accounting path."
    echo "Directors can preview exact read-only member scope and manage per-Athanor policy from Moon Tax."
    echo "Bills support reversible waivers, corrections, and voids with permanent reasons and history."
    echo "Unchanged exemption history is fingerprinted instead of being fully rescanned every audit."
    echo "Auth-account exemptions now cover every linked character and are rechecked each audit."
    echo "Post-2022 compressed ore now uses the correct 1:1 item-count valuation."
    echo "Automatic tax enforcement remains disabled until a director enables it."
    echo "Backup: ${backup_dir}"
    echo "Log: ${LOG_FILE}"
}

case "${MODE}" in
    install)
        install_or_update
        ;;
    status)
        show_status
        ;;
    audit)
        queue_tax_audit
        ;;
    discover)
        discover_recipients
        ;;
    access)
        repair_access
        ;;
    logs)
        show_logs
        ;;
    *)
        echo "Unknown mode: ${MODE}"
        exit 2
        ;;
esac
