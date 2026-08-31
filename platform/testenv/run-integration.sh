#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export BUH_COMPOSE_PROJECT="${BUH_COMPOSE_PROJECT:-buh-integration-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-$$}"
export BUH_TEST_IMAGE_TAG="${BUH_TEST_IMAGE_TAG:-${BUH_COMPOSE_PROJECT}}"
COMPOSE=(
    docker compose
    --project-name "${BUH_COMPOSE_PROJECT}"
    -f "${ROOT}/platform/testenv/compose.yml"
)

cleanup() {
    "${COMPOSE[@]}" down --volumes --remove-orphans
}
trap cleanup EXIT

"${COMPOSE[@]}" up -d --wait db redis fake-esi
"${COMPOSE[@]}" run --rm test python manage.py check --no-color
"${COMPOSE[@]}" run --rm test python manage.py migrate --noinput --no-color
"${COMPOSE[@]}" run --rm test python manage.py migrate --noinput --no-color
"${COMPOSE[@]}" run --rm test python manage.py showmigrations --plan
"${COMPOSE[@]}" run --rm test python manage.py test --noinput --no-color \
    tests.integration
"${COMPOSE[@]}" up -d --wait worker
"${COMPOSE[@]}" run --rm test python manage.py buh_test_celery --no-color
