# PBXonix Agent

The agent that runs on an Asterisk, FreePBX or Issabel server and reports to
[PBXonix Cloud](https://pbxonix.com).


## Source and releases

This is the public source of the agent distributed by PBXonix. It runs on
Linux with Python 3.6 or newer and has no third-party runtime dependencies.

- [Download a versioned release](https://github.com/mikhilgermov-afk/pbxonix-agent/releases)
- [Report an issue](https://github.com/mikhilgermov-afk/pbxonix-agent/issues)
- [Release process](RELEASING.md) and [changelog](CHANGELOG.md)
- [Security reports](SECURITY.md)

Each release includes a wheel, source archive, installers and SHA256SUMS.
The cloud installer below remains the recommended way to connect a PBX.
Using PBXonix Cloud requires an account and enrollment token. Installing or
reading the agent source does not require purchasing a cloud subscription.

## Install

Create a PBX in the dashboard, then run the command it gives you, as root:

```bash
curl --speed-limit 20 --speed-time 60 -fsSL https://get.pbxonix.com/install.sh | sudo sh -s -- --token TOKEN
```

Safe to run again — it upgrades in place and leaves an existing enrollment
alone. Pass `--re-enroll` with a new token to replace one.

## What it does

Opens an **outbound** HTTPS connection to `api.pbxonix.com` and pushes
telemetry. The cloud never connects back. You open no port, forward no AMI,
expose no database and run no VPN.

Every 30 seconds it sends a heartbeat. Every 60 seconds it samples metrics into
a local SQLite buffer and flushes it. With AMI enabled, slow inventory reads
run every 300 seconds by default. A separate panel worker refreshes cached
operator states and queue summaries every 5 seconds. Intervals are configurable.
If the cloud is unreachable the buffered metrics replay when it returns.

See [QUALITY.md](QUALITY.md) for RTCP quality collection and
[REPORTS.md](REPORTS.md) for optional, locally sanitized report data.

## SIP trunk monitoring

Off by default, because it needs a credential. Three steps, once:

**1. Give Asterisk a read-only manager user.** Copy
the output of `pbxonix-agent ami-config` to `/etc/asterisk/manager_pbxonix.conf`,
replace `REPLACE_ME` with a secret you generate (`openssl rand -base64 32`), add
`#include manager_pbxonix.conf` to `manager.conf`, then
`asterisk -rx "manager reload"`.

The AMI user is loopback-only and holds no `originate`, `command`, `config` or
`all` permission. The agent does not place calls or rewrite dialplan. Local
system probes use fixed, read-only Asterisk CLI commands. The optional quality
clock helper permits only bounded codec lookups; see [QUALITY.md](QUALITY.md).

**2. Store the secret locally.**

```bash
sudo pbxonix-agent set-ami --username pbxonix
```

It prompts. The secret never goes through argv, so it does not land in your
shell history or in anyone else's `ps` output. It is written to
`/etc/pbxonix/credentials.json` (0640 root:pbxonix) and **is never sent to
PBXonix Cloud**.

**3. Turn it on** — set `ami_enabled = true` in `/etc/pbxonix/agent.conf`, then
`sudo systemctl restart pbxonix-agent`.

Check it worked:

```bash
sudo pbxonix-agent check
```

That prints every trunk the agent can see, its normalised state, and the raw
Asterisk status behind it.

### What is read

`PJSIPShowRegistrationsOutbound` and `SIPshowregistry` for outbound trunk
registrations, `PJSIPShowEndpoints` and `SIPpeers` for endpoint counts, and
`QueueStatus` for queues. Both channel drivers are attempted, so a PBX running
each for different trunks is handled.

Slow inventory collectors share one AMI session per cycle. Lightweight panel
sampling uses its own persistent session and peer-status events between
inventory reads, so the 5-second panel does not repeat expensive full scans.

Queues report calls waiting, how many agents are logged in, how many could
actually take the next call (device state 1 and not paused), and the longest
current wait.

Asterisk's status words are normalised to five states — `registered`,
`unregistered`, `rejected`, `unreachable`, `unknown` — and the original word is
carried through to the alert, because "Rejected" tells you far more than "down".

A trunk mid-handshake ("Request Sent", "Stopping") reports `unknown`, which
neither opens nor closes an alert. Treating it as recovered would resolve the
alert and re-open it a minute later, forever.

The inventory session logs in with `Events: off`. The persistent panel session
and optional quality worker subscribe only to their required event classes.

## Design constraints

**No third-party dependencies.** Not a preference — a requirement:

1. It installs on PBXs with no route to PyPI. `pip install --no-index` of one
   verified wheel only works if there is nothing to resolve.
2. It runs as a service next to live call traffic. Every dependency would be
   more code someone else wrote, running there.

Hence INI config via `configparser` rather than YAML, `urllib` rather than
`httpx`, and `/proc` rather than `psutil`.

**Python 3.6+.** Not 3.12, and not 3.9 either. 3.6 is the stock interpreter on
Issabel 4 / CentOS 7, and a monitoring tool for legacy phone systems that
refuses to run on legacy phone systems is not a product. CentOS 7 is EOL and its
SCL repos have moved to vault, so "install a newer Python first" is advice that
frequently cannot be followed.

The floor is paid for in the source: no dataclasses, no `from __future__ import
annotations`, no PEP 604 unions, no `subprocess.run(capture_output=)`, no
`add_subparsers(required=)`. `make test-oldest` runs the suite on 3.6. Note that
compiling on 3.6 proves very little — the last three of those parse cleanly and
fail only when the line runs — so the tests exercise them directly.

**Never runs as root.** The systemd unit runs as `pbxonix` with a locked-down
sandbox. `NOPASSWD: ALL` is never used; a collector that needs elevation gets a
sudoers entry naming the exact commands.

## Files

| Path | |
|---|---|
| `/etc/pbxonix/agent.conf` | Configuration, `0640 root:pbxonix` |
| `/etc/pbxonix/credentials.json` | Agent credential, `0640 root:pbxonix` |
| `/var/lib/pbxonix/buffer.sqlite3` | Local buffer, owned by `pbxonix` |
| `/opt/pbxonix-agent/venv/` | The agent itself |
| `/etc/systemd/system/pbxonix-agent.service` | Unit |

## Operating it

```bash
systemctl status pbxonix-agent
journalctl -u pbxonix-agent -f
pbxonix-agent check                     # everything the agent can see, incl. trunks
pbxonix-agent set-ami --username pbxonix   # store the AMI secret (prompts)
systemctl restart pbxonix-agent          # after editing agent.conf
```

`check` prints host facts, current system metrics, Asterisk state and whether
the agent is enrolled. It is the first thing to run when something looks wrong.

## Configuration

```ini
[agent]
api_base_url = https://api.pbxonix.com
heartbeat_interval_seconds = 30
metrics_interval_seconds = 60
log_level = INFO
verify_tls = true

[buffer]
path = /var/lib/pbxonix/buffer.sqlite3
max_rows = 50000
max_bytes = 67108864

[asterisk]
ami_enabled = false
ami_host = 127.0.0.1
ami_port = 5038

[recordings]
paths = /var/spool/asterisk/monitor
```

`api_base_url` must be `https://`. The agent refuses to start otherwise —
a bearer credential travels on every request.

The buffer is bounded by row count **and** file size, and refuses to write below
128 MB free regardless. A monitoring agent that fills the disk of the PBX it is
watching has caused the exact outage it was installed to prevent. When full, the
oldest samples go first: during a long outage recent telemetry is worth more
than the start of the gap.

## What is sent

System: CPU, load average, RAM, swap, disk, inode usage, uptime.
Asterisk: running or not, version, active channels and calls.
With AMI enabled: trunk names and registration state, endpoint counts, and
queue names with waiting-call and agent counts.
Recordings: how many files, how much space, and when the newest one was
written. This needs no AMI credential -- it is a filesystem scan.

## What is never sent

- Audio, and recording **filenames**. Asterisk names recordings after the call,
  so the name is personal data; only counts, sizes and timestamps are sent.
- SIP, AMI, database or SSH passwords. AMI credentials stay in
  `credentials.json` on your machine.
- Customer phone numbers, caller names or unfiltered call detail records.
  Report collection removes these fields locally before upload. The cloud
  receives aggregate statistics and sanitized event rows; see [REPORTS.md](REPORTS.md).
  Technical extension and queue identifiers are retained for monitoring.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check pbxonix_agent
```

The suite runs without a network and without a PBX — collectors are tested
against synthetic `/proc` content, on Linux without accessing a live phone system.

Test against the oldest supported interpreter (pytest 7 dropped 3.6, hence
the pin). This is the guard that catches a 3.7+ construct before a customer
does:

```bash
docker run --rm -v "$PWD:/src" -w /src python:3.6-slim \
  sh -c 'pip install -q "pytest<7" && python -m pytest -q'
```

## Uninstall

```bash
systemctl disable --now pbxonix-agent
rm -f /etc/systemd/system/pbxonix-agent.service
systemctl daemon-reload
rm -rf /opt/pbxonix-agent /etc/pbxonix /var/lib/pbxonix
userdel pbxonix && groupdel pbxonix
```

Then delete the PBX in the dashboard, which revokes the credential.

## Licence

Licensed under [Apache License 2.0](LICENSE). Preserve the license and
applicable [NOTICE](NOTICE) when redistributing. The hosted PBXonix Cloud
service is separate and is not included in this repository.


### Calendar-day queue statistics (agent 0.9.0)

The optional collector reads an Asterisk realtime MySQL/MariaDB queue log every
30 seconds in its own thread. It uses the PBX host timezone, including DST, and
recomputes the day from durable events after restarts. It does not add AMI calls.
The cloud receives aggregate counters and wait times only.

Configure a local SELECT-only account for the queue log columns `time`,
`queuename`, `event`, `data1`, `data3`, `data4`. Store it in
`/etc/pbxonix/queue-mysql.cnf` (root:pbxonix, mode 0640):

```ini
[client]
user=pbxonix_queue
password="YOUR_LOCAL_READ_ONLY_PASSWORD"
protocol=socket
```

In `/etc/pbxonix/agent.conf`:

```ini
[queue_daily]
enabled = true
defaults_file = /etc/pbxonix/queue-mysql.cnf
database = asteriskcdrdb
table = queuelog
time_basis = local
```

Match the database/table to `queue_log` in Asterisk extconfig.conf. The table must
have expanded `data1` through `data4` columns and sortable timestamp strings
(`YYYY-MM-DD HH:MM:SS[.microseconds]`). Set `time_basis = utc` when the logger uses
`queue_log_realtime_use_gmt = yes`; otherwise confirm the stored time basis on
the PBX. A leading `time` index is recommended for large logs. The local mysql
client must be at `/usr/bin/mysql`. Each read has a 5-second timeout.

The collector is disabled by default. Empty text logs are not used as evidence
of zero calls. Failed reads keep the last sample with its original age; at the
next midnight, old-day totals disappear. A successful empty database query is
zero activity. No customer-specific queue or extension exclusions are applied.

Counts are event-based within the current local day: ENTERQUEUE for entries,
CONNECT for answers, ABANDON / EXITWITHTIMEOUT / EXITEMPTY / EXITWITHKEY for exits
without answer. Repeated entries count separately; member RINGNOANSWER attempts
and COMPLETE events do not duplicate outcomes. Average wait uses CONNECT hold
time. Maximum wait uses answered/exited waits, not calls still waiting. Outcomes
from calls entering before midnight can belong to the following day.
