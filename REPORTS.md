# Call and queue reports (agent 0.10.2)

Reports are optional. Agent 0.10.2 removes customer phone numbers, CallerID names,
URLs, free-form dialplan fields and unknown queue-agent names before any HTTPS
request. Only timestamps, durations, outcomes, numeric queue statistics, technical
call IDs and endpoints from the PBX inventory are sent. Unknown endpoints are
omitted. Phone numbers are not replaced with customer hashes or masked digits.
Raw records are read only on the PBX and are not written to the agent buffer.

Cloud ingestion applies the same allowlist to legacy agents. Reports and Excel
have no From/To phone-number columns. Internal extension, queue and trunk IDs
remain for operational statistics. Display names manually entered in PBXonix are
account configuration. Recordings and call routing remain on the PBX.

Reporting waits for a successful AMI inventory before publishing. Unknown named
queue agents cannot be attributed to an extension and do not create named rows.

## Configure a source

1. Check the CDR and queue-log schemas and their timestamp conventions. CDR
   timestamps and queue timestamps can use different time zones on one PBX.
2. Create a dedicated local database account with SELECT on the necessary
   columns only. Store it in `/etc/pbxonix/reports-mysql.cnf`:

   ```ini
   [client]
   user=pbxonix_reports
   password=YOUR_RANDOM_PASSWORD
   host=localhost
   ```

   Set ownership `root:pbxonix` and permissions `0640`. Do not reuse the
   administrator database password. The service user needs access to the
   local socket. No remote database port is needed.
3. Add a `[reports]` section to `/etc/pbxonix/agent.conf`, using the example
   below. Restart only `pbxonix-agent`.
4. Open Reports → Data sources and coverage. Wait until both sources show
   their received-through dates at the current time before evaluating totals.

```ini
[reports]
enabled = true
defaults_file = /etc/pbxonix/reports-mysql.cnf
cdr_mode = mysql
cdr_database = asteriskcdrdb
cdr_table = cdr
cdr_time_basis = local
queue_database = asteriskcdrdb
queue_table = queuelog
queue_time_basis = local
history_days = 30
```

CDR columns: `src`, `dst`, `dcontext`, `channel`, `dstchannel`, `lastapp`,
`start` (or `calldate`), `answer`, `end`, `duration`, `billsec`, `disposition`,
`uniqueid`, `linkedid`, `sequence`. Only existing columns are selected.
Required: start/calldate, duration, billsec, uniqueid. End, answer and linkedid
improve accuracy. Without linkedid, uniqueid groups call paths.

Queue columns: `id` (optional), `time`, `callid`, `queuename`, `agent`, `event`,
`data1` through `data5`. Supported timestamps are SQL-style local/UTC dates,
with optional fractional seconds; epoch or pipe-delimited queue files are
not supported by this source. The database time column should be indexed.

On a large CDR table, use an indexed end/start column or Master.csv. Do not
run unindexed scans over a production CDR archive or add a large index during
business hours merely to enable reports.

## Master.csv / legacy Issabel

```ini
cdr_mode = csv
cdr_csv = /var/log/asterisk/cdr-csv/Master.csv
cdr_time_basis = utc
cdr_database = asteriskcdrdb
cel_table = cel
```

Verify UTC against an actual uniqueid timestamp; it is not universal. The
service user needs read access to the file, its directory and rotated files.
The parser expects standard Asterisk CSV columns, including uniqueid (17th
column), with one completed record per physical line. It seeks to the recent
part of the file rather than scanning the archive. Keep rotated files
uncompressed until the agent has caught up. Existing compressed rotations
are not backfilled. Out-of-order historical imports require a fresh replay.

Optional `cel_table` resolves missing CDR linkedid through indexed CEL
uniqueid lookups. Grant SELECT only on `uniqueid,linkedid`; verify that
uniqueid is indexed. Leave this option empty if no compatible CEL exists.

## Operation and limitations

- Initial history: 30 days, configurable from 1 to 180, from local midnight.
- Up to 500 CDR records or 1,000 queue events per source batch. HTTPS
  requests are split into roughly 12 KB slices for legacy networks; coverage
  advances only after all slices are acknowledged.
- After catch-up, polls run every minute. SQL sources replay recent queue
  events every five minutes and the preceding CDR day hourly for delayed
  writes. Replayed source keys do not increase totals.
- Checkpoints live in `/var/lib/pbxonix/reports-state.json`. Back them up
  before replacing sources. Stop the agent before resetting a checkpoint.
- Report queries allow 93 calendar days, at most 300,000 source rows per
  source query; Excel allows 50,000 rows per worksheet. Narrow the period or
  PBX selection when a limit is reached.
- CDR answer can be an IVR answer. Queue answers come from queue events.
  Operator online time is not reconstructed from the current status.
- Asterisk routing varies. Direction and trunk matching use actual channel
  endpoints and the PBX inventory, never telephone-number length. Unknown
  endpoints remain unknown rather than being guessed.
- Queue reports count visits by entry date. Transfers/re-entries can produce
  several visits in one conversation. Queue SLA uses all entries as its
  denominator. An incomplete source is marked visibly in UI and Excel.

To stop collecting report history, set `enabled = false` and restart the
agent. Existing monitoring configuration and already collected reports remain.
