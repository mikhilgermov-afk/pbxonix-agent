# Call quality (agent 0.11.1)

An independent worker subscribes to Asterisk AMI `reporting` events. It accepts
`RTCPSent` (media received by the PBX) and `RTCPReceived` (media sent by the PBX)
for SIP/PJSIP channels. It never originates calls or changes dialplan, direct
media, RTP settings or AMI permissions. Existing local `system,reporting`
permissions are sufficient.

Every minute it sends numerical aggregates to `/v1/agent/quality`: report
counts, sums and maxima for fractional loss, interarrival jitter and RTCP RTT,
grouped by known inventory endpoint and media direction. Unknown endpoint
names are discarded on the agent and checked again against cloud inventory.
There are no call IDs, customer numbers, caller names, IP addresses, audio,
packet captures or raw SIP messages in the outgoing or retry payload.

Fractional loss uses the RTCP eight-bit fraction divided by 256. RTT seconds
are converted to milliseconds only when a sender report has been acknowledged.
Jitter uses RTP clock units, not milliseconds. A bounded, read-only local
`core show channel` lookup resolves NativeFormats. Known unambiguous clocks are
converted to milliseconds; unknown codecs or multi-report events leave jitter
absent. G.722 uses the 8 kHz RTP clock despite its 16 kHz audio rate. No MOS is
estimated. Audio may be poor for reasons these three metrics cannot reveal.

The module shows observation-weighted averages, peaks, sample counts, collector
freshness and 1-hour / 24-hour / 7-day history. Observations are neither unique
calls nor packet counts. Threshold indicators use loss >=2%, jitter >=30 ms,
and RTT >=300 ms as diagnostic guides. These indicators do not send additional
notifications. Existing PBX/trunk alerts retain their behavior.

Collection is bounded to 500 aggregate groups and 30 codec lookups per minute.
Only five sanitized batches are retained in memory for failed delivery; excess
observations are counted as dropped. Restarting the agent drops the current
in-memory minute. Historical gaps are not filled with zeros. Cloud retention
follows the existing metrics retention setting (30 days by default).

If the collector is connected but no measurements arrive, check whether there
are SIP/PJSIP audio calls, RTCP is available, and media passes through Asterisk.
Direct media, missing RTCP or absent audio cannot be treated as zero packet loss.
The agent does not change call routing to force measurement coverage.

Protocol references:
- https://docs.asterisk.org/Asterisk_20_Documentation/API_Documentation/AMI_Events/RTCPReceived/
- https://github.com/asterisk/asterisk/blob/20/main/rtp_engine.c
- https://github.com/asterisk/asterisk/blob/20/res/res_rtp_asterisk.c
- https://www.rfc-editor.org/rfc/rfc3551#section-4.5.2

On systemd installations a root-owned, socket-activated helper runs as the Asterisk CLI socket owner. It accepts only validated SIP/PJSIP channel names and returns a numeric RTP clock. Its socket is accessible only to the agent user. The main agent keeps NoNewPrivileges=yes, gains no sudo permission, and cannot send arbitrary CLI commands through this helper.
