#!/bin/sh
#
# PBXonix agent installer -- bootstrap.
#
#   curl -fsSL https://get.pbxonix.com/install.sh | sudo sh -s -- --token TOKEN
#
# This file is deliberately tiny, and that is its entire job.
#
# A pipe into a shell cannot resume. The shell executes bytes as they arrive, so
# a connection that stalls part-way leaves a half-read script and no way to
# recover -- restarting fetches from zero and hits the same wall at the same
# offset. Measured on a customer's PBX, three times: that path stops carrying
# data at roughly 20 KB. The real installer is twice that, and could never
# arrive in one piece over such a link.
#
# So the one-liner fetches only this, which is small enough to land in a single
# burst, and this downloads the installer to a *file* -- where curl can resume
# from wherever the previous attempt died.
#
# Wrapped in a function called on the last line, so that a truncated copy of
# even this much runs nothing at all rather than half of something.
main() {
set -eu

GET="${PBXONIX_GET:-https://get.pbxonix.com}"
URL="$GET/agent.sh"
ATTEMPTS=8

command -v curl >/dev/null 2>&1 || {
  printf 'fail curl is required and was not found\n' >&2
  exit 1
}

TARGET="$(mktemp)"
trap 'rm -f "$TARGET"' EXIT

got() { wc -c < "$TARGET" 2>/dev/null || echo 0; }

printf '==> Fetching the installer\n'

# Only curl 7.29 options: that is what CentOS 7 ships, and an unknown flag would
# break the download this exists to make reliable.
OPTS="--proto =https --tlsv1.2 --connect-timeout 15 --max-time 300 --speed-limit 1024 --speed-time 20"

# One attempt asking for compression, before the resume loop. The installer is
# 43 KB of shell and gzips to 15 KB, and on the link this whole bootstrap exists
# for -- one that stops forwarding after a fixed ~20 KB per connection -- that
# is the difference between arriving whole on the first try and needing three.
#
# It is deliberately outside the resume loop. Range requests address the encoded
# stream while `-C -` counts decoded bytes, so resuming a compressed transfer
# can silently reassemble nonsense. If this attempt fails the file is discarded
# and everything below refetches it as plain bytes, where resume is exact.
NEED_PLAIN=1
# shellcheck disable=SC2086
if curl -fsSL $OPTS --compressed -o "$TARGET" "$URL"; then
  NEED_PLAIN=0
else
  # Discard whatever arrived. The loop below resumes by plain byte offset, and
  # a partial compressed body is not a prefix of the file it would resume.
  rm -f "$TARGET"
fi

n=0
while [ "$NEED_PLAIN" -eq 1 ] && [ "$n" -lt "$ATTEMPTS" ]; do
  n=$((n + 1))
  before="$(got)"

  # -C - is meaningful only once something is on disk; on a fresh file curl
  # would ask the server to resume from nowhere.
  if [ "$before" -gt 0 ]; then
    # shellcheck disable=SC2086
    curl -fsSL $OPTS -C - -o "$TARGET" "$URL" && break
  else
    # shellcheck disable=SC2086
    curl -fsSL $OPTS -o "$TARGET" "$URL" && break
  fi

  [ "$n" -lt "$ATTEMPTS" ] || break
  after="$(got)"
  if [ "$after" -gt "$before" ]; then
    printf '    stalled at %s bytes, resuming (attempt %s of %s)\n' "$after" "$((n + 1))" "$ATTEMPTS"
  else
    printf '    no progress, retrying (attempt %s of %s)\n' "$((n + 1))" "$ATTEMPTS"
  fi
  sleep 3
done

# Completeness, not integrity: the transfer is TLS from our own origin, and what
# actually goes wrong here is truncation. The installer's last line is its own
# entry point, so finding it proves the file arrived whole -- and it needs no
# checksum to be regenerated and kept in step on every edit.
if ! tail -n 1 "$TARGET" | grep -q '^main "\$@"'; then
  printf 'fail could not download the installer\n' >&2
  printf '     url:      %s\n' "$URL" >&2
  printf '     attempts: %s\n' "$ATTEMPTS" >&2
  printf '     got:      %s bytes, incomplete\n' "$(got)" >&2
  printf '\n' >&2
  printf '     The connection between this machine and the CDN stops carrying\n' >&2
  printf '     data part-way through. A transfer that starts fine and dies at\n' >&2
  printf '     the same offset every time is usually an MTU problem on the path\n' >&2
  printf '     rather than a refusal. Tell PBXonix support the byte count.\n' >&2
  exit 1
fi

# Arguments straight through, so --token, --configure-ami and the rest behave as
# though this indirection were not here. The installer reads its prompts from
# /dev/tty rather than stdin, which is what makes that safe under a pipe.
sh "$TARGET" "$@"
}

# Nothing above this line has run yet. If this download was itself cut short,
# the shell never reaches here and nothing happens -- which is the point.
main "$@"
