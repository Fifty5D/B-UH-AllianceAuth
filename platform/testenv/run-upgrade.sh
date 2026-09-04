#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export BUH_COMPOSE_PROJECT="${BUH_COMPOSE_PROJECT:-buh-upgrade-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-$$}"
export BUH_TEST_IMAGE_TAG="${BUH_TEST_IMAGE_TAG:-${BUH_COMPOSE_PROJECT}}"
COMPOSE=(
    docker compose
    --project-name "${BUH_COMPOSE_PROJECT}"
    -f "${ROOT}/platform/testenv/compose.yml"
)
TEMP_DIR="$(mktemp -d)"

cleanup() {
    if [[ "${BUH_KEEP_COMPOSE:-0}" != "1" ]]; then
        "${COMPOSE[@]}" down --volumes --remove-orphans
    fi
    rm -rf -- "${TEMP_DIR}"
}
trap cleanup EXIT

"${COMPOSE[@]}" up -d --wait db redis fake-esi

# Recreate the exact deployed application schema and evidence from immutable
# v0.3.3 wheels before current source is allowed to touch the database.
"${COMPOSE[@]}" run --rm baseline \
    python manage.py migrate --noinput --no-color
"${COMPOSE[@]}" run --rm -T baseline \
    python manage.py shell \
    <"${ROOT}/platform/baselines/seed-v0.3.3.py"
"${COMPOSE[@]}" run --rm baseline \
    python manage.py showmigrations --plan --no-color

# This is the pre-migration backup. It contains synthetic CI data only.
"${COMPOSE[@]}" exec -T db sh -ceu \
    'export MYSQL_PWD="$MARIADB_ROOT_PASSWORD"; exec mariadb-dump --user=root --single-transaction --quick --routines --triggers --events --hex-blob "$MARIADB_DATABASE"' \
    >"${TEMP_DIR}/legacy-v1.sql"
test -s "${TEMP_DIR}/legacy-v1.sql"
sha256sum "${TEMP_DIR}/legacy-v1.sql"

# Exercise the normal upgrade path in place.
"${COMPOSE[@]}" run --rm test \
    python manage.py migrate --noinput --no-color
"${COMPOSE[@]}" run --rm test \
    python manage.py migrate --noinput --no-color
"${COMPOSE[@]}" run --rm -T test \
    python manage.py shell \
    <"${ROOT}/tests/upgrade/verify_v0_3_3.py"

# Prove that the pre-migration backup restores independently, then upgrade the
# restored copy as the rollback/forward-fix rehearsal.
"${COMPOSE[@]}" exec -T db sh -ceu \
    'export MYSQL_PWD="$MARIADB_ROOT_PASSWORD"; mariadb --user=root --execute="DROP DATABASE IF EXISTS buh_restore; CREATE DATABASE buh_restore CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"'
"${COMPOSE[@]}" exec -T db sh -ceu \
    'export MYSQL_PWD="$MARIADB_ROOT_PASSWORD"; exec mariadb --user=root buh_restore' \
    <"${TEMP_DIR}/legacy-v1.sql"
"${COMPOSE[@]}" run --rm -e BUH_DB_NAME=buh_restore test \
    python manage.py migrate --noinput --no-color
"${COMPOSE[@]}" run --rm -e BUH_DB_NAME=buh_restore test \
    python manage.py check --no-color
"${COMPOSE[@]}" run --rm -T -e BUH_DB_NAME=buh_restore test \
    python manage.py shell \
    <"${ROOT}/tests/upgrade/verify_v0_3_3.py"

echo "Legacy-v1 upgrade and pre-migration backup restore drill passed."
