#!/usr/bin/env bash
set -Eeuo pipefail

# Production installation requires separate approval. The host publishes only
# redacted data to the private diagnostics repository via a repo-scoped key.
if [[ "$(id -u)" -ne 0 || "$#" -ne 5 ]]; then
    echo 'Usage (as root): bootstrap-diagnostics.sh COLLECTOR PUBLISHER REDACTOR PRIVATE_DEPLOY_KEY GITHUB_KNOWN_HOSTS' >&2
    exit 64
fi
collector="$1"; publisher="$2"; redactor="$3"; keyfile="$4"; hostsfile="$5"
for source in "${collector}" "${publisher}" "${redactor}" "${keyfile}" "${hostsfile}"; do
    test -f "${source}" || { echo 'A required input file is missing.' >&2; exit 65; }
done
install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "${collector}" /usr/local/libexec/buh-host-diagnostics
install -o root -g root -m 0755 "${publisher}" /usr/local/libexec/buh-diagnostics-publish
install -o root -g root -m 0755 "${redactor}" /usr/local/libexec/buh-redact-diagnostics
install -d -o root -g root -m 0700 /etc/buh-diagnostics /var/lib/buh-diagnostics /var/lib/buh-diagnostics-publish
install -o root -g root -m 0600 "${keyfile}" /etc/buh-diagnostics/deploy-key
install -o root -g root -m 0600 "${hostsfile}" /etc/buh-diagnostics/known_hosts
cat >/etc/systemd/system/buh-diagnostics.service <<'EOF'
[Unit]
Description=Collect private B-UH diagnostics independently of Auth
Wants=docker.service
After=docker.service

[Service]
Type=oneshot
User=root
ExecStart=/usr/bin/python3 -B /usr/local/libexec/buh-host-diagnostics collect
TimeoutStartSec=12min
EOF
cat >/etc/systemd/system/buh-diagnostics.timer <<'EOF'
[Unit]
Description=Collect B-UH diagnostics every five minutes

[Timer]
OnCalendar=*-*-* *:00/5:00
AccuracySec=30s
Persistent=true
Unit=buh-diagnostics.service

[Install]
WantedBy=timers.target
EOF
cat >/etc/systemd/system/buh-diagnostics-publish.service <<'EOF'
[Unit]
Description=Publish redacted B-UH diagnostics to the private evidence branch
Wants=network-online.target
After=network-online.target buh-diagnostics.service

[Service]
Type=oneshot
User=root
ExecStart=/usr/bin/python3 -B /usr/local/libexec/buh-diagnostics-publish
TimeoutStartSec=12min
EOF
cat >/etc/systemd/system/buh-diagnostics-publish.timer <<'EOF'
[Unit]
Description=Publish private B-UH diagnostics every five minutes

[Timer]
OnCalendar=*-*-* *:02/5:00
AccuracySec=30s
Persistent=true
Unit=buh-diagnostics-publish.service

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl start buh-diagnostics.service
systemctl start buh-diagnostics-publish.service
systemctl enable --now buh-diagnostics.timer buh-diagnostics-publish.timer
echo 'Diagnostics collector and private publisher installed; verify live connector retrieval.'
