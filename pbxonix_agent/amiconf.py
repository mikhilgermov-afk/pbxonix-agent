"""The manager.conf snippet Asterisk needs before AMI will answer.

Carried in the package rather than only in the repository. The installer ships
a single wheel and nothing else, so a path like
``packaging/manager_pbxonix.conf.example`` does not exist on a customer's PBX --
and both the docs and the agent's own post-enrollment message used to point at
it. `pbxonix-agent ami-config` prints this instead.

A string rather than package data: the agent has no dependencies and must load
identically from a wheel and from a checkout, and ``importlib.resources`` is
3.7+ while the floor is 3.6. A test asserts the repository copy still matches
this, so the two cannot drift.
"""

# The literal string this file ships with, swapped for a real secret by
# `ami-config --generate`. Not a credential -- a marker that one is missing.
SECRET_PLACEHOLDER = "REPLACE_ME"  # noqa: S105

# The paragraph that tells the reader how to obtain a secret, and what to
# say instead once one is already filled in. Kept as constants so the file
# cannot end up explaining how to generate a secret it already contains.
HOW_TO_GENERATE = """\
; The secret below must match the one the agent stores locally. Getting both
; right in one step:
;
;   pbxonix-agent ami-config --generate
;
; It generates the secret, stores it in /etc/pbxonix/credentials.json
; (0640 root:pbxonix), and prints this file with it already filled in. The
; secret is never transmitted to PBXonix Cloud.
"""

ALREADY_FILLED = """\
; The secret below is the one the agent has stored in
; /etc/pbxonix/credentials.json (0640 root:pbxonix). Asterisk and the agent
; therefore agree by construction. It is never transmitted to PBXonix Cloud.
"""

MANAGER_CONF = """; Read-only AMI user for PBXonix.
;
; Install:
;   1. Put this file next to manager.conf as manager_pbxonix.conf
;   2. Add to /etc/asterisk/manager.conf:   #include manager_pbxonix.conf
;   3. asterisk -rx "manager reload"
;
; The secret below must match the one the agent stores locally. Getting both
; right in one step:
;
;   pbxonix-agent ami-config --generate
;
; It generates the secret, stores it in /etc/pbxonix/credentials.json
; (0640 root:pbxonix), and prints this file with it already filled in. The
; secret is never transmitted to PBXonix Cloud.
;
; -----------------------------------------------------------------------------
; A note on "read" and "write", because the names are misleading.
;
; In Asterisk, `read` controls which *unsolicited events* the session receives.
; `write` controls which *actions* the session may invoke -- it does NOT mean
; "may modify the system". Invoking PJSIPShowEndpoints therefore requires
; write=system, even though it only reads.
;
; The permissions that would actually let this user do harm are `originate`
; (place calls), `command` (run arbitrary CLI), `config` (rewrite configuration)
; and `all`. None of them are granted below, and PBXonix has no code path that
; would use them.
; -----------------------------------------------------------------------------

[pbxonix]
secret = REPLACE_ME

; Loopback only. The entire product exists so that nothing has to be exposed;
; an AMI reachable from the network would undo that in one line.
;
; deny first, then permit. Asterisk walks the whole list and the LAST matching
; rule wins, so a catch-all deny placed after the permit overrides it and
; refuses everything -- loopback included. Every sample config Asterisk ships
; is written in this order for exactly that reason.
deny = 0.0.0.0/0.0.0.0
permit = 127.0.0.1/255.255.255.255

; system    -> PJSIPShowEndpoints, PJSIPShowRegistrationsOutbound, CoreStatus
; reporting -> SIPshowregistry, CoreShowChannels, QueueStatus
read = system,reporting
write = system,reporting

; No eventfilter is needed: the agent logs in with "Events: off", so the session
; receives no unsolicited traffic at all -- only direct responses to the actions
; it issues.
"""
