#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export BUH_COMPOSE_PROJECT="${BUH_COMPOSE_PROJECT:-buh-fast-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-$$}"
export BUH_TEST_IMAGE_TAG="${BUH_TEST_IMAGE_TAG:-${BUH_COMPOSE_PROJECT}}"

cd "${ROOT}"

ruff check apps platform tests ops
python -m compileall -q apps platform tests ops
while IFS= read -r -d '' script; do
    node --check "${script}"
done < <(find apps -type f -name '*.js' -print0)
python -m unittest discover -s tests/platform -p 'test_*.py'

export PYTHONPATH="${ROOT}/platform/testauth:${ROOT}"
export BUH_TEST_SETTINGS="testauth.settings.unit"
python platform/testauth/manage.py check --no-color
python platform/testauth/manage.py makemigrations --check --dry-run --no-color \
    buh_structure_ops buh_mining_analytics buh_moon_tax
python platform/testauth/manage.py test --noinput --no-color \
    buh_structure_ops.tests \
    buh_mining_analytics.tests \
    buh_moon_tax.tests \
    tests.contracts
