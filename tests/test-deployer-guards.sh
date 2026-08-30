#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
entry="${repo_root}/ops/buh-github-deploy-entry"
root_helper="${repo_root}/ops/buh-github-deploy-root"
bootstrap="${repo_root}/ops/bootstrap-deployer.sh"
workflow="${repo_root}/.github/workflows/deploy-moon-tax.yml"
setup="${repo_root}/setup/Setup-BUH-GitHubDeployer.ps1"

bash -n "${entry}" "${root_helper}" "${bootstrap}"

test_root="$(mktemp -d)"
trap 'rm -rf -- "${test_root}"' EXIT
mkdir -p "${test_root}/bin"
cat >"${test_root}/bin/sudo" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >"${BUH_DEPLOY_TEST_ROOT}/sudo-args"
cat >"${BUH_DEPLOY_TEST_ROOT}/stdin"
SH
chmod 0700 "${test_root}/bin/sudo"

if PATH="${test_root}/bin:${PATH}" BUH_DEPLOY_TEST_ROOT="${test_root}" \
    SSH_ORIGINAL_COMMAND="diagnostics 30" bash "${entry}" </dev/null 2>/dev/null; then
    echo "The deploy entry accepted an unsupported command." >&2
    exit 1
fi
test ! -e "${test_root}/sudo-args"

printf 'checked release bytes' | PATH="${test_root}/bin:${PATH}" \
    BUH_DEPLOY_TEST_ROOT="${test_root}" SSH_ORIGINAL_COMMAND="deploy moon-tax" \
    bash "${entry}"
grep -Fxq -- '-n /usr/local/sbin/buh-github-deploy-root' "${test_root}/sudo-args"
grep -Fxq 'checked release bytes' "${test_root}/stdin"

grep -Fq 'restrict,command="/usr/local/bin/buh-github-deploy-entry"' "${bootstrap}"
grep -Fq 'NOPASSWD: /usr/local/sbin/buh-github-deploy-root' "${bootstrap}"
grep -Fq 'MAX_ARCHIVE_BYTES=67108864' "${root_helper}"
grep -Fq 'flock -n' "${root_helper}"
grep -Fq 'sha256sum --check --strict SHA256SUMS' "${root_helper}"
grep -Fq -- '--no-same-owner --no-same-permissions' "${root_helper}"
grep -Fq 'workflow_dispatch:' "${workflow}"
grep -Fq "vars.BUH_VPS_HOST || secrets.BUH_VPS_HOST || '74.208.147.52'" "${workflow}"
grep -Fq 'variable set BUH_VPS_HOST' "${setup}"
grep -Fq 'variable set BUH_VPS_PORT' "${setup}"
if grep -Eq '^[[:space:]]+(push|pull_request|release):' "${workflow}"; then
    echo "Production deployment must remain an explicit release-promotion action." >&2
    exit 1
fi

echo "Guarded deployer tests passed."
