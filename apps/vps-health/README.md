# B-UH VPS health

The application source was recovered from the installed, MIT-licensed
`aa_buh_vps_health-0.2.0-py3-none-any.whl` (SHA-256
`b2f10c7b25d7134e83c32abb75fcf85198a4de829a59d4da81d199bebfce67dc`).
This successor keeps the application code and widens its AllianceAuth dependency
only after the AllianceAuth 5.4 compatibility tests pass.

## Filesystem telemetry

The standalone host collector reports the system filesystem (`/`) and Auth data
filesystem (`/srv/buh-data`) separately. It reads exact host mount entries and
filesystem statistics; it does not scan stored files. Docker overlay mounts and
the database/archive bind aliases are not additional capacity. A missing data
mount is unavailable, even when its underlying directory still exists on `/`.
Matching device identities are counted once.

`host.filesystems` is an additive list with stable IDs `system` and `data`, labels
`System disk` and `Auth data`, and these fields:

- `mountpoint`, `available`, and `status` (`mounted`, `not_mounted`, `duplicate`,
  or `unavailable`).
- `total_bytes`, `used_bytes`, `free_bytes`, `available_bytes`, and `percent`.
  `free_bytes` includes filesystem-reserved space; `available_bytes` is usable by
  the application. Percent remains used/total, matching the existing system-disk
  metric. Unavailable readings contain null numbers rather than invented zeroes.

The legacy `host.disk_*` values, stored scalar disk fields, existing element IDs,
and historical disk chart continue to mean the system disk. New snapshots also
store `filesystems` JSON, returned additively by the history endpoint. Old agents
and old snapshots have no additional-filesystem sample; their root history is
unchanged and missing data is not a healthy zero-percent reading.

Disk warnings evaluate each filesystem independently using the configured disk
threshold and sustained sample count. The existing `disk` alert key remains the
system disk; `disk:data` is Auth data pressure. Missing Auth data readings raise
an availability warning and cannot clear an existing pressure warning. A rolled
back or older agent that cannot report data telemetry also cannot manufacture a
recovery. Valid healthy readings can resolve those warnings.

## Compatible rollout

The dashboard application and standalone root collector are separate installed
components. The collector runs from `/usr/local/sbin/buh-vps-health-agent.py`;
updating the application wheel alone does not replace that executable. The new
dashboard tolerates an older collector and shows additional storage as
unavailable. A newer collector also retains all legacy fields for older
dashboards. Installing the reviewed application migration and the separately
reviewed collector update is necessary to show live Auth data capacity. Source
changes do not install or restart either production component.
