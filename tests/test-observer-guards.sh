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
if grep -Eq '(grep|sed|awk)[^\n]*\.env' "${root_script}"; then
    echo "Observer must let Compose isolate the one allow-listed environment value." >&2
    exit 1
fi
if ! grep -Fq 'config --environment' "${root_script}"; then
    echo "Runtime fingerprint does not use the allow-listed Compose environment view." >&2
    exit 1
fi
if ! grep -Fq -- "--format '{{.Id}}'" "${root_script}" || \
    ! grep -Fq -- "--format '{{json .RepoDigests}}'" "${root_script}"; then
    echo "Runtime fingerprint must inspect image identity and digests separately." >&2
    exit 1
fi
if grep -Fq "{{.Id}}\\t{{json .RepoDigests}}" "${root_script}"; then
    echo "Runtime fingerprint must not rely on Go-template escape delimiters." >&2
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
redacted_again="$(printf '%s' "${redacted}" | python3 "${redactor}" /dev/stdin)"
if [[ "${redacted}" != "${redacted_again}" ]]; then
    echo "Redactor output is not stable after one invocation." >&2
    exit 1
fi
for forbidden in super-secret abc.def.ghi client-password webhook-token discord-secret cookie-secret sentry-secret '_token()'; do
    if grep -Fq "${forbidden}" <<<"${redacted}"; then
        echo "Redactor leaked fixture secret: ${forbidden}" >&2
        exit 1
    fi
done
if [[ "$(grep -o '<redacted' <<<"${redacted}" | wc -l)" -lt 5 ]]; then
    echo "Redactor did not replace all expected secret forms." >&2
    exit 1
fi
if ! grep -Fq 'ordinary_line=keep-this-visible' <<<"${redacted}"; then
    echo "Redactor removed an ordinary diagnostic field." >&2
    exit 1
fi
redacted_file="$(mktemp -t buh-redacted-test.XXXXXX)"
trap 'rm -f -- "${redacted_file}"' EXIT
python3 "${redactor}" "${redaction_input}" >"${redacted_file}"
python3 "${redactor}" --validate "${redacted_file}"
if python3 "${redactor}" --validate "${redaction_input}" >/dev/null 2>&1; then
    echo "Credential validator accepted unsafe input." >&2
    exit 1
fi

fake_bin="${repo_root}/tests/fakebin"

for minutes in 5 15 30 60 180 360; do
    output="$(PATH="${fake_bin}:${PATH}" SSH_ORIGINAL_COMMAND="diagnostics ${minutes}" "${entry}")"
    [[ "${output}" == "sudo_target=-n /usr/local/sbin/buh-github-observe-root stdin=${minutes}" ]]
done
output="$(PATH="${fake_bin}:${PATH}" SSH_ORIGINAL_COMMAND="fingerprint platform-v2" "${entry}")"
[[ "${output}" == "sudo_target=-n /usr/local/sbin/buh-github-observe-root stdin=fingerprint platform-v2" ]]

for denied in "" "bash" "diagnostics 120" "diagnostics 15; id" "diagnostics" \
    "fingerprint" "fingerprint platform-v2; id"; do
    if PATH="${fake_bin}:${PATH}" SSH_ORIGINAL_COMMAND="${denied}" "${entry}" >/dev/null 2>&1; then
        echo "Observer accepted forbidden command: ${denied}" >&2
        exit 1
    fi
done

echo "Observer guard tests passed."
