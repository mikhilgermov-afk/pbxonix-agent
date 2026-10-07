# Collectors

Each collector returns a plain dict and never raises. A PBX where one probe is
unavailable must still report everything else — losing disk metrics because
Asterisk is unreachable would be a worse outage than the one being monitored.

## system

Reads `/proc` and `statvfs` directly. No `psutil`, because the agent ships with
no third-party dependencies.

| Field | Source | Note |
|---|---|---|
| `cpu_pct` | `/proc/stat` | A rate, so it needs two readings. The first sample returns `null` rather than a fabricated number. |
| `load_1/5/15` | `os.getloadavg()` | |
| `ram_pct` | `/proc/meminfo` | Derived from `MemAvailable`, not `MemFree`. Page cache is not memory pressure; alerting on `MemFree` would page someone every night during a backup. |
| `swap_*` | `/proc/meminfo` | |
| `disk_*` | `shutil.disk_usage` | |
| `inode_pct` | `os.statvfs` | Tracked separately: inode exhaustion looks exactly like a full disk to Asterisk while `df` still reports free space. |
| `uptime_seconds` | `/proc/uptime` | |

## asterisk

Phase 1 reads what can be had without credentials.

| Field | Source |
|---|---|
| `running` | `systemctl is-active asterisk`, falling back to a CLI probe |
| `version` | `asterisk -rx "core show version"` |
| `active_channels`, `active_calls` | `asterisk -rx "core show channels count"` |
| `uptime_seconds` | `asterisk -rx "core show uptime"` |

`running` is a **tri-state**. `null` means "could not determine" and is not the
same as `false`. Reporting `false` where the agent merely lacks permission
would raise a false `asterisk_stopped` alert on every install that has not been
given access.

When Asterisk is not running, the channel probes are skipped rather than
attempted — each would otherwise burn a five-second subprocess timeout every
cycle.

Subprocesses are always invoked with a fixed argv list, never a shell string, so
there is nothing for a hostname or version banner to inject into.

## Phase 2

`ami.py`, `pjsip.py`, `queues.py`, `recordings.py`. The cloud-side tables,
enums and alert kinds already exist — see `docs/ROADMAP.md` in `pbxonix-server`.

AMI connects to `127.0.0.1:5038` only, with a read-only manager user. See
`packaging/manager_pbxonix.conf.example`.
