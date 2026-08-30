#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
entry="${repo_root}/ops/buh-github-observe-entry"
root_script="${repo_root}/ops/buh-github-observe-root"
bootstrap="${repo_root}/ops/bootstrap-observer.sh"
redactor="${repo_root}/ops/buh-redact-diagnostics.py"

for script in "${entry}" "${root_script}" "${bootstrap}"; do
    bash -n "${script}"
done
python3 -m py_compile "${redactor}"

if grep -Eq '(^|[^A-Za-z])source[[:space:]]+.*\.env|cat[[:space:]]+.*\.env' "${root_script}"; then
    echo "Observer must never print or source .env." >&2
    exit 1
fi

if ! grep -Fq 'restrict,command="/usr/local/bin/buh-github-observe-entry"' "${bootstrap}"; then
    echo "Observer key is not forced and restricted." >&2
    exit 1
fi

if ! grep -Fq 'NOPASSWD: /usr/local/sbin/buh-github-observe-root' "${bootstrap}"; then
    echo "Observer sudo rule is missing or unexpectedly broad." >&2
    exit 1
fi

redaction_input="${repo_root}/tests/fixtures/redaction-input.txt"
redacted="$(python3 "${redactor}" "${redaction_input}")"
for forbidden in super-secret abc.def.ghi client-password webhook-token; do
    if grep -Fq "${forbidden}" <<<"${redacted}"; then
        echo "Redactor leaked fixture secret: ${forbidden}" >&2
        exit 1
    fi
done
if [[ "$(grep -o '<redacted' <<<"${redacted}" | wc -l)" -lt 5 ]]; then
    echo "Redactor did not replace all expected secret forms." >&2
    exit 1
fi

fake_bin="${repo_root}/tests/fakebin"

for minutes in 5 15 30 60; do
    output="$(PATH="${fake_bin}:${PATH}" SSH_ORIGINAL_COMMAND="diagnostics ${minutes}" "${entry}")"
    [[ "${output}" == "sudo_target=-n /usr/local/sbin/buh-github-observe-root stdin=${minutes}" ]]
done

for denied in "" "bash" "diagnostics 120" "diagnostics 15; id" "diagnostics"; do
    if PATH="${fake_bin}:${PATH}" SSH_ORIGINAL_COMMAND="${denied}" "${entry}" >/dev/null 2>&1; then
        echo "Observer accepted forbidden command: ${denied}" >&2
        exit 1
    fi
done

echo "Observer guard tests passed."
