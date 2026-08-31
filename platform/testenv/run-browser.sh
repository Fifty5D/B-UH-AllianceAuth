#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export BUH_COMPOSE_PROJECT="${BUH_COMPOSE_PROJECT:-buh-browser-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-$$}"
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
"${COMPOSE[@]}" run --rm -e BUH_TEST_SETTINGS=testauth.settings.browser test \
    python manage.py migrate --noinput --no-color
"${COMPOSE[@]}" run --rm -e BUH_TEST_SETTINGS=testauth.settings.browser test \
    python manage.py buh_seed_test_platform --reset --no-color
"${COMPOSE[@]}" up -d --wait web
"${COMPOSE[@]}" run --rm browser
