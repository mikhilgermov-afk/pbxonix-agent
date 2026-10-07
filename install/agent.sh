#!/bin/sh
#
# PBXonix agent installer.
#
# Not the file the one-liner fetches. install.sh is a small bootstrap that
# downloads this one to disk first, because a pipe into a shell cannot resume a
# stalled transfer and this file is too big to rely on arriving whole over a
# link that stalls. Running this directly works too:
#
#   sh agent.sh --token TOKEN
#
# Safe to run again: re-running upgrades the agent in place and leaves an
# existing enrolment alone unless a new token is supplied.
#
# Everything is fetched from get.pbxonix.com and checked against a SHA-256
# published alongside it. No third-party download, no unpinned pip install.
#
# POSIX sh, not bash. Alpine and several appliance images ship no bash at all,
# so a bash installer cannot even start there -- and telling somebody to install
# a shell before they can install the agent is not one command. The only thing
# this cost was `set -o pipefail`, which dash does not implement; the two places
# it mattered check their own output instead.
#
# Streamed into a shell, this file is read as it arrives, so a connection that
# stalls part-way would otherwise leave the shell executing half an installer
# and then waiting forever for the rest. Measured on a real install: 21338 bytes
# of script, the last line to run at byte 20156, that machine's transfers
# stalling at 20508.
#
# So the whole installer lives in a function that is called on the last line.
# The shell must parse through to the closing brace before it can call it -- a
# truncated download therefore runs nothing at all and fails loudly, instead of
# configuring half a system in silence.
main() {
set -eu

PBXONIX_GET="${PBXONIX_GET:-https://get.pbxonix.com}"
PBXONIX_API="${PBXONIX_API:-https://api.pbxonix.com}"
AGENT_VERSION="${PBXONIX_AGENT_VERSION:-0.11.1}"

INSTALL_DIR=/opt/pbxonix-agent
CONFIG_DIR=/etc/pbxonix
STATE_DIR=/var/lib/pbxonix
SERVICE_USER=pbxonix
SERVICE_NAME=pbxonix-agent
ASTERISK_ETC=/etc/asterisk
MIN_PYTHON_MINOR=6

TOKEN=""
FORCE_ENROLL=0
# The installer may fetch a missing Python from the distribution. Set to 0 with
# --no-install-deps on a machine where package installs are controlled
# elsewhere; the install then stops with the exact command to run instead.
INSTALL_DEPS=1
# empty = ask the operator, 1 = yes without asking, 0 = never.
CONFIGURE_AMI=""

# --------------------------------------------------------------------- output --
if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  C_OK=$(tput setaf 2); C_WARN=$(tput setaf 3); C_ERR=$(tput setaf 1); C_DIM=$(tput setaf 8); C_OFF=$(tput sgr0)
else
  C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_OFF=""
fi
step() { printf '%s==>%s %s\n' "$C_DIM" "$C_OFF" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '%swarn%s %s\n' "$C_WARN" "$C_OFF" "$*" >&2; }
die()  { printf '%sfail%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage: install.sh --token TOKEN [options]

  --token TOKEN       Enrollment token from the PBXonix dashboard (Add PBX).
  --re-enroll         Replace an existing enrolment with a new token.
  --configure-ami     Set up the read-only AMI user without asking. Use for
                      unattended installs; interactively the installer asks.
  --no-configure-ami  Never touch Asterisk configuration.
  --no-install-deps   Never invoke the package manager. If Python or its venv
                      support is missing, stop and print the command instead.
  --version X.Y.Z     Install a specific agent version.
  --help              Show this message.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --token)      TOKEN="${2:-}"; shift 2 ;;
    --token=*)    TOKEN="${1#*=}"; shift ;;
    --re-enroll)  FORCE_ENROLL=1; shift ;;
    --configure-ami)    CONFIGURE_AMI=1; shift ;;
    --no-configure-ami) CONFIGURE_AMI=0; shift ;;
    --no-install-deps)  INSTALL_DEPS=0; shift ;;
    --version)    AGENT_VERSION="${2:-}"; shift 2 ;;
    --version=*)  AGENT_VERSION="${1#*=}"; shift ;;
    --help|-h)    usage; exit 0 ;;
    *)            die "unknown option: $1 (try --help)" ;;
  esac
done

# ---------------------------------------------------------------- preflight --
[ "$(id -u)" -eq 0 ] || die "must run as root: pipe into 'sudo sh' rather than 'sh'"
command -v curl >/dev/null 2>&1 || die "curl is required and was not found"

DISTRO_ID="unknown"; DISTRO_VERSION="unknown"; DISTRO_PRETTY="unknown"
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  DISTRO_ID="${ID:-unknown}"
  DISTRO_VERSION="${VERSION_ID:-unknown}"
  DISTRO_PRETTY="${PRETTY_NAME:-$DISTRO_ID $DISTRO_VERSION}"
elif [ -r /etc/redhat-release ]; then
  # CentOS 6 and the Elastix images built on it predate os-release.
  DISTRO_PRETTY="$(cat /etc/redhat-release)"
  DISTRO_ID=rhel
fi

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64)   ARCH=amd64 ;;
  aarch64|arm64)  ARCH=arm64 ;;
  armv7l)         ARCH=armv7 ;;
  *)              warn "unrecognised architecture '$ARCH'; the agent is pure Python so this is usually fine" ;;
esac

# Which package manager this box has, if any. Used only to install a missing
# Python; nothing else here touches packages.
PKG=""
if command -v apt-get >/dev/null 2>&1;  then PKG=apt
elif command -v dnf >/dev/null 2>&1;    then PKG=dnf
elif command -v yum >/dev/null 2>&1;    then PKG=yum
elif command -v zypper >/dev/null 2>&1; then PKG=zypper
elif command -v pacman >/dev/null 2>&1; then PKG=pacman
elif command -v apk >/dev/null 2>&1;    then PKG=apk
fi

# Which init system will actually be supervising the agent.
#
# `command -v systemctl` is not the test. Plenty of images carry the binary
# without systemd running as PID 1 -- every Docker container built from a
# systemd distribution, for one -- and writing a unit file there produces an
# installer that reports success and leaves nothing running. /run/systemd/system
# exists only when systemd is genuinely managing the machine.
INIT=""
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
  INIT=systemd
elif command -v rc-update >/dev/null 2>&1 && [ -x /sbin/openrc-run ]; then
  INIT=openrc
elif [ -d /etc/init.d ]; then
  INIT=sysv
else
  # Nothing to install a service into: a stripped container image, usually.
  # Not a reason to refuse -- the agent runs fine, and whatever runs the
  # container is what supervises it.
  INIT=none
fi

step "Detected ${DISTRO_PRETTY} (${ARCH}, ${INIT}${PKG:+, $PKG})"

# Run a package install, visibly. Never interactive: this script is usually
# piped from curl, so stdin is not a terminal and a prompt would hang forever.
# Bounded, so a mirror that stopped answering fails instead of stalling the
# install. Only ever asked to add one named package.
install_package() {
  name=$1
  [ "$INSTALL_DEPS" -eq 1 ] || return 1
  [ -n "$PKG" ] || return 1
  step "  installing ${name} with ${PKG}"
  case "$PKG" in
    apt)
      DEBIAN_FRONTEND=noninteractive timeout 300 apt-get update -qq >/dev/null 2>&1 || true
      DEBIAN_FRONTEND=noninteractive timeout 600 apt-get install -y -qq "$name" >/dev/null 2>&1
      ;;
    dnf)    timeout 600 dnf install -y -q "$name" >/dev/null 2>&1 ;;
    yum)    timeout 600 yum install -y -q "$name" >/dev/null 2>&1 ;;
    zypper) timeout 600 zypper --non-interactive --quiet install "$name" >/dev/null 2>&1 ;;
    # --needed so an already-present package is not pointlessly reinstalled;
    # --noconfirm because there is no terminal to confirm at.
    pacman) timeout 600 pacman -Sy --noconfirm --needed "$name" >/dev/null 2>&1 ;;
    apk)    timeout 600 apk add --quiet --no-cache "$name" >/dev/null 2>&1 ;;
  esac
}

# ------------------------------------------------------------------- python --
# The agent is pure Python with no dependencies and runs from its own venv, so
# the floor is the stock interpreter on the oldest box we target: 3.6, which is
# what Issabel 4 / CentOS 7 ships. Newest first so a box with something modern
# installed alongside uses it.
#
# SCL paths are searched explicitly. Someone who followed older advice and ran
# `yum install rh-python38` gets an interpreter that is invisible to `command -v`
# unless the SCL environment is enabled, which it is not inside a curl | sh.
PYTHON=""
FOUND_PYTHON=""      # something that ran but was too old, for the error message

find_python() {
  PYTHON=""
  FOUND_PYTHON=""
  _search_python
}

_search_python() {
for candidate in \
  python3.13 python3.12 python3.11 python3.10 python3.9 \
  python3.8 python3.7 python3.6 python3 \
  /opt/rh/rh-python38/root/usr/bin/python3 \
  /opt/rh/rh-python36/root/usr/bin/python3
do
  case "$candidate" in
    /*) [ -x "$candidate" ] || continue; resolved="$candidate" ;;
    *)  command -v "$candidate" >/dev/null 2>&1 || continue; resolved="$(command -v "$candidate")" ;;
  esac
  version="$("$resolved" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" || continue
  [ -n "$version" ] || continue
  major="${version%%.*}"
  minor="${version##*.}"
  FOUND_PYTHON="${FOUND_PYTHON}${FOUND_PYTHON:+, }${resolved} (${version})"
  if [ "$major" -eq 3 ] && [ "$minor" -ge "$MIN_PYTHON_MINOR" ]; then
    PYTHON="$resolved"
    break
  fi
done
}

find_python

# Nothing suitable on the box: fetch one before giving up. The package name is
# not the same everywhere -- on Arch `python` *is* python 3, and `python3` does
# not exist as a package at all.
if [ -z "$PYTHON" ] && [ -z "$FOUND_PYTHON" ]; then
  case "$PKG" in
    pacman) install_package python && find_python ;;
    *)      install_package python3 && find_python ;;
  esac
fi

if [ -z "$PYTHON" ]; then
  if [ -n "$FOUND_PYTHON" ]; then
    die "no python 3.${MIN_PYTHON_MINOR}+ found. Interpreters seen: ${FOUND_PYTHON}"
  fi
  # By far the most common case on Issabel: python3 was simply never installed.
  # CentOS 7 is EOL and its mirrors are gone, so yum may need pointing at vault
  # before it can install anything at all.
  die "python3 not found and could not be installed automatically.

     Issabel / CentOS 7:  yum install -y python3
     If yum cannot reach a mirror, CentOS 7 is EOL and has moved to vault:
       sed -i 's|^mirrorlist=|#mirrorlist=|; s|^#baseurl=http://mirror.centos.org|baseurl=http://vault.centos.org|' /etc/yum.repos.d/CentOS-*.repo
     Debian / Ubuntu:     apt-get install -y python3 python3-venv
     Alpine:              apk add python3
     Arch:                pacman -S python
     openSUSE:            zypper install python3"
fi
ok "Using $PYTHON ($("$PYTHON" -V 2>&1))"

# `import venv` is not the right probe. On Debian and Ubuntu it succeeds while
# `python3 -m venv` still fails, because ensurepip -- which the venv bootstrap
# needs -- ships in a separate package. Checking the wrong module meant passing
# our own check and then failing several steps later with Python's error message
# instead of ours.
venv_works() { "$PYTHON" -c 'import ensurepip, venv' >/dev/null 2>&1; }

if ! venv_works; then
  # Debian names it for the exact minor version (python3.12-venv); the
  # unversioned name is the older convention. Alpine keeps pip in its own
  # package. Everything else bundles ensurepip with the interpreter, so the
  # loop simply finds nothing to do.
  PYVER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "")"
  for package in "python${PYVER}-venv" python3-venv py3-pip python3-pip; do
    case "$package" in
      python-venv) continue ;;   # PYVER was empty; the name would be nonsense
    esac
    install_package "$package" || continue
    venv_works && break
  done
fi

if ! venv_works; then
  PYVER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "3")"
  die "python can create no virtual environment: ensurepip is missing.

     Debian/Ubuntu:  apt-get install -y python${PYVER}-venv
     RHEL/CentOS:    yum install -y python3
     Alpine:         apk add py3-pip
     openSUSE:       zypper install python3-pip

     Then re-run this installer. (It tries this itself unless
     --no-install-deps was given, or no package manager was found.)"
fi

# ------------------------------------------------------------ user and dirs --
step "Creating service account and directories"

group_exists() {
  if command -v getent >/dev/null 2>&1; then
    getent group "$1" >/dev/null 2>&1
  else
    cut -d: -f1 /etc/group | grep -qx "$1"
  fi
}

# Debian keeps nologin in /usr/sbin, RHEL in /sbin, Alpine in /sbin with
# busybox behind it. Pick before creating the account rather than retrying a
# half-applied useradd.
NOLOGIN=/sbin/nologin
for candidate in /usr/sbin/nologin /sbin/nologin /bin/false; do
  [ -x "$candidate" ] && { NOLOGIN="$candidate"; break; }
done

if ! group_exists "$SERVICE_USER"; then
  if command -v groupadd >/dev/null 2>&1; then
    groupadd --system "$SERVICE_USER"
  else
    # busybox: different tool, different flags, same intent.
    addgroup -S "$SERVICE_USER"
  fi
fi

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  if command -v useradd >/dev/null 2>&1; then
    useradd --system --gid "$SERVICE_USER" --home-dir "$STATE_DIR" \
            --shell "$NOLOGIN" --comment "PBXonix monitoring agent" "$SERVICE_USER"
  else
    adduser -S -D -H -h "$STATE_DIR" -s "$NOLOGIN" -G "$SERVICE_USER" \
            -g "PBXonix monitoring agent" "$SERVICE_USER"
  fi
fi

# Reading Asterisk state usually needs group membership, which is preferable to
# sudo. Absent on a plain Asterisk build, which is fine -- the agent then reports
# "unknown" rather than a wrong answer.
if group_exists asterisk; then
  if command -v usermod >/dev/null 2>&1; then
    usermod -a -G asterisk "$SERVICE_USER"
  else
    addgroup "$SERVICE_USER" asterisk
  fi
  ok "Added $SERVICE_USER to the asterisk group"
fi

install -d -m 0750 "$CONFIG_DIR"
chown root:"$SERVICE_USER" "$CONFIG_DIR"
chmod 0750 "$CONFIG_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR"
install -d -m 0755 "$INSTALL_DIR"
ok "$CONFIG_DIR (0750 root:$SERVICE_USER), $STATE_DIR (0750 $SERVICE_USER)"

# ----------------------------------------------------------------- download --
WHEEL="pbxonix_agent-${AGENT_VERSION}-py3-none-any.whl"
# Download with a deadline, resuming across a broken path, and say what went
# wrong when it finally cannot.
#
# A stalled transfer is the failure that actually happens here: the connection
# and TLS succeed, part of the body arrives, and then the path stops carrying
# full-size packets. Restarting from zero hits the same wall at the same offset,
# so each attempt resumes from what is already on disk instead.
#
# --speed-limit/--speed-time abort an attempt once it has effectively stopped,
# rather than waiting out --max-time. Only curl 7.29 flags are used: that is
# what CentOS 7 ships, and an unknown flag would break the download it was meant
# to make reliable.
CURL_COMMON="--proto =https --tlsv1.2 --connect-timeout 15 --max-time 300 --speed-limit 1024 --speed-time 20"

# `wc -c < missing` fails in the shell's own redirection, before wc runs, so a
# 2>/dev/null on wc does not silence it.
size_of() { if [ -f "$1" ]; then wc -c < "$1"; else echo 0; fi; }

fetch() {
  url=$1; dest=$2; what=$3
  attempt=0; max=6; rc=0

  rm -f "$dest"
  while [ "$attempt" -lt "$max" ]; do
    attempt=$((attempt + 1))
    before=$(size_of "$dest")

    # -C - is only meaningful once something is on disk; on a fresh file curl
    # would ask the server to resume from nowhere.
    if [ "$before" -gt 0 ]; then
      # shellcheck disable=SC2086
      curl -fsSL $CURL_COMMON -C - -o "$dest" "$url" && return 0
    else
      # shellcheck disable=SC2086
      curl -fsSL $CURL_COMMON -o "$dest" "$url" && return 0
    fi
    rc=$?

    # 22 is curl's "the server answered with >= 400". That is a decision, not a
    # hiccup: a 404 will be a 404 next time too, and retrying it five more times
    # only delays telling the operator what is wrong.
    [ "$rc" -eq 22 ] && break

    after=$(size_of "$dest")
    if [ "$attempt" -lt "$max" ]; then
      if [ "$after" -gt "$before" ]; then
        step "  transfer stalled at ${after} bytes, resuming (attempt $((attempt + 1)) of ${max})"
      else
        step "  no progress, retrying (attempt $((attempt + 1)) of ${max})"
      fi
      sleep 3
    fi
  done

  # Ask once more without -f so the server's own answer can be reported: a
  # challenge, a 403 and a stalled path are indistinguishable otherwise.
  # Assigned in a way that REPLACES the value on failure rather than appending
  # to whatever curl managed to print first.
  # shellcheck disable=SC2086
  code=$(curl -sSL $CURL_COMMON -o /dev/null -w '%{http_code}' "$url" 2>/dev/null) \
    || code="no response"
  after=$(size_of "$dest")

  die "could not download $what
     url:      $url
     attempts: ${attempt} of ${max}
     got:      ${after} bytes
     response: HTTP ${code}

     A partial byte count with no error means the transfer stalled part-way --
     a path problem between this PBX and the CDN, not a refusal. It often
     clears on its own; if it does not, tell PBXonix support the byte count.
     403 or 503 would mean the CDN is challenging this address instead."
}

TMPDIR="$(mktemp -d)"
cleanup() { rm -rf "$TMPDIR"; }
trap cleanup EXIT

step "Downloading agent ${AGENT_VERSION}"
fetch "$PBXONIX_GET/$WHEEL" "$TMPDIR/$WHEEL" "the agent"
fetch "$PBXONIX_GET/$WHEEL.sha256" "$TMPDIR/$WHEEL.sha256" "the checksum"

step "Verifying checksum"
# Both of these are pipelines whose first stage could fail, and this shell has
# no pipefail. Neither needs it: a failure leaves the variable empty or
# mismatched, and both outcomes are checked below before anything is installed.
EXPECTED="$(awk '{print $1}' "$TMPDIR/$WHEEL.sha256")"
if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL="$(sha256sum "$TMPDIR/$WHEEL" | awk '{print $1}')"
else
  ACTUAL="$(shasum -a 256 "$TMPDIR/$WHEEL" | awk '{print $1}')"
fi
[ -n "$EXPECTED" ] || die "published checksum is empty"
[ -n "$ACTUAL" ] || die "could not compute a checksum for the downloaded wheel"
[ "$EXPECTED" = "$ACTUAL" ] || die "checksum mismatch: expected $EXPECTED, got $ACTUAL"
ok "sha256 $(printf '%.16s' "$ACTUAL")..."

# ------------------------------------------------------------------ install --
step "Installing into $INSTALL_DIR"
# Existence is not health. A partial run leaves bin/python with no pip inside,
# and a check for the file alone then installs into an environment that cannot
# install anything. Ask it whether it can do its one job.
venv_usable() {
  [ -x "$INSTALL_DIR/venv/bin/python" ] \
    && "$INSTALL_DIR/venv/bin/python" -m pip --version >/dev/null 2>&1
}

if ! venv_usable; then
  # Rebuilt rather than repaired: a half-built environment carries no
  # inventory of what it is missing, and this directory is ours to own.
  [ -n "$INSTALL_DIR" ] && rm -rf "$INSTALL_DIR/venv"
  # Reported in our own voice. Python's own failure text is useful but lands
  # without context, and the operator cannot tell whether the installer noticed.
  "$PYTHON" -m venv "$INSTALL_DIR/venv" \
    || die "could not create the virtual environment in $INSTALL_DIR/venv.
     The output above is Python's own. If it mentions ensurepip, install the
     venv package for this interpreter and re-run."

  venv_usable || die "the virtual environment was created but has no pip.
     Usually a partial or interrupted python installation. Try:
       rm -rf $INSTALL_DIR/venv
     then re-run this installer."
fi
AGENT_BIN="$INSTALL_DIR/venv/bin/pbxonix-agent"
# A wheel needs no build step, and --no-index means nothing is fetched beyond
# the file we just checksummed. This works on a PBX with no outbound access to
# PyPI, which is the common case. It is also why the agent depends on nothing
# outside the standard library.
"$INSTALL_DIR/venv/bin/python" -m pip install --quiet --no-index --upgrade "$TMPDIR/$WHEEL" \
  || die "agent installation failed"
ok "Agent $("$AGENT_BIN" --version 2>/dev/null || echo "$AGENT_VERSION") installed"


# Restricted RTCP codec-clock service (systemd only).
pbxonix_install_quality_clock() (
# Run by the root installer. The agent retains NoNewPrivileges and receives
# neither sudo access nor permission to execute arbitrary Asterisk commands.
set -eu
SERVICE_USER="${1:-pbxonix}"
AGENT_PYTHON="${2:-/opt/pbxonix-agent/venv/bin/python}"
case "$SERVICE_USER" in *[!a-zA-Z0-9_-]*|'') exit 1;; esac
command -v systemctl >/dev/null 2>&1 || exit 0
[ -d /run/systemd/system ] || exit 0
if ! HELPER_SOURCE=$("$AGENT_PYTHON" -c 'from pbxonix_agent import quality_clock_helper; print(quality_clock_helper.__file__)' 2>/dev/null); then exit 0; fi
[ -f "$HELPER_SOURCE" ] || exit 1
HELPER_PYTHON=$(command -v python3)
SOCKET_OWNER=asterisk
SOCKET_GROUP=asterisk
if [ -S /var/run/asterisk/asterisk.ctl ]; then
  SOCKET_OWNER=$(stat -c %U /var/run/asterisk/asterisk.ctl)
  SOCKET_GROUP=$(stat -c %G /var/run/asterisk/asterisk.ctl)
fi
case "$SOCKET_OWNER:$SOCKET_GROUP" in *[!a-zA-Z0-9_:-]*) exit 1;; esac
id "$SOCKET_OWNER" >/dev/null 2>&1 || exit 0
install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0644 "$HELPER_SOURCE" /usr/local/libexec/pbxonix-quality-clock.py
cat > /etc/systemd/system/pbxonix-quality-clock.socket <<EOF
[Unit]
Description=PBXonix restricted RTP clock socket
[Socket]
ListenStream=/run/pbxonix-quality-clock.sock
SocketUser=$SERVICE_USER
SocketMode=0600
Accept=yes
MaxConnections=8
[Install]
WantedBy=sockets.target
EOF
cat > /etc/systemd/system/pbxonix-quality-clock@.service <<EOF
[Unit]
Description=PBXonix read-only RTP clock lookup
[Service]
Type=oneshot
User=$SOCKET_OWNER
Group=$SOCKET_GROUP
ExecStart=$HELPER_PYTHON -I -S /usr/local/libexec/pbxonix-quality-clock.py
StandardInput=socket
StandardOutput=socket
StandardError=null
TimeoutStartSec=3
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=yes
CapabilityBoundingSet=
Nice=15
EOF
chmod 0644 /etc/systemd/system/pbxonix-quality-clock.socket /etc/systemd/system/pbxonix-quality-clock@.service
chown root:root /etc/systemd/system/pbxonix-quality-clock.socket /etc/systemd/system/pbxonix-quality-clock@.service
systemctl daemon-reload
systemctl enable pbxonix-quality-clock.socket >/dev/null
systemctl restart pbxonix-quality-clock.socket
systemctl is-active --quiet pbxonix-quality-clock.socket

)
if ! pbxonix_install_quality_clock "$SERVICE_USER" "$INSTALL_DIR/venv/bin/python"; then
  warn "RTP clock helper unavailable; jitter will be omitted when the codec clock is unknown"
fi

# ------------------------------------------------------------------- config --
CONFIG_FILE="$CONFIG_DIR/agent.conf"
if [ ! -f "$CONFIG_FILE" ]; then
  step "Writing $CONFIG_FILE"
  # INI rather than YAML: the agent has no third-party dependencies, so it reads
  # this with configparser from the standard library. It also matches the format
  # PBX administrators already work in.
  cat > "$CONFIG_FILE" <<EOF
# PBXonix agent configuration.
# Credentials live in $CONFIG_DIR/credentials.json (0640 root:$SERVICE_USER)
# and are never written here.

[agent]
api_base_url = $PBXONIX_API
heartbeat_interval_seconds = 30
metrics_interval_seconds = 60
log_level = INFO
verify_tls = true

[buffer]
path = $STATE_DIR/buffer.sqlite3
max_rows = 50000
max_bytes = 67108864

[asterisk]
ami_enabled = false
ami_host = 127.0.0.1
ami_port = 5038
# ami_username and ami_password are read from credentials.json, never from here.
#
# How often to re-read trunks, peers and queues. This is inventory: it changes
# when somebody edits the PBX, not second to second. SIPpeers walks and locks
# chan_sip's whole peer container and QueueStatus iterates every member of every
# queue, so asking every minute made call audio break up on a busy system.
#
# 0 switches this pass off entirely while leaving the cheap per-minute poll
# running: call counts and extension state keep working, only trunks and queues
# stop being refreshed. That is the setting to reach for if operators report
# artefacts during calls -- it makes the agent quiet on the PBX immediately
# without giving up monitoring.
#
# Otherwise 0, or 60 and above.
ami_interval_seconds = 300

[recordings]
# Comma-separated. Only counts, sizes and timestamps are collected -- never
# audio, and never a filename: Asterisk names recordings after the call, so the
# name itself is personal data.
paths = /var/spool/asterisk/monitor
# Recordings do not need per-minute resolution and a large spool is expensive
# to walk. Minimum 30.
interval_seconds = 300
# Safety valves for a spool with hundreds of thousands of files. The walk visits
# newest-first, so hitting either limit leaves the freshness signal correct and
# only the totals best-effort.
max_scan_seconds = 10
max_entries = 200000
EOF
  chown root:"$SERVICE_USER" "$CONFIG_FILE"
  chmod 0640 "$CONFIG_FILE"
  ok "configuration written"
else
  ok "keeping existing $CONFIG_FILE"
fi

# ------------------------------------------------------------------ service --
# Three init systems, one interface. Everything below this block calls
# svc_install / svc_enable / svc_restart / svc_is_active / svc_logs and does not
# care which one is underneath.
#
# The sandboxing that systemd applies declaratively has no equivalent under
# OpenRC or sysvinit. What survives everywhere is the part that matters most on
# a phone system: the agent runs at low priority and yields disk to Asterisk.
# A monitoring tool must never make the thing it monitors worse.
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
INITD_FILE="/etc/init.d/${SERVICE_NAME}"
# /run is the modern location and /var/run a symlink to it -- but the boxes
# that need a sysv init script are exactly the old ones, and CentOS 6 predates
# /run entirely. Pick what exists rather than assume.
if [ -d /run ]; then
  PIDFILE="/run/${SERVICE_NAME}.pid"
else
  PIDFILE="/var/run/${SERVICE_NAME}.pid"
fi

# nice is in POSIX and present everywhere. ionice is not, so it is used only
# where it exists.
NICE_CMD="nice -n 10"
command -v ionice >/dev/null 2>&1 && NICE_CMD="ionice -c 3 $NICE_CMD"

svc_install_systemd() {
  cat > "$UNIT_FILE" <<EOF
[Unit]
Description=PBXonix monitoring agent
Documentation=https://pbxonix.com
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
ExecStart=$AGENT_BIN run --config $CONFIG_FILE
Restart=always
RestartSec=10
# The agent only reads state and dials out. Everything below is unnecessary
# for that, so it is denied.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
ReadWritePaths=$STATE_DIR
CapabilityBoundingSet=
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
# A monitoring agent must never make the thing it monitors worse. On a phone
# system the media path is soft-realtime: a few tens of milliseconds of stolen
# CPU or a blocked disk read is audible. So the agent runs behind everything
# else -- it gets the machine only when nothing important wants it.
#
# This is not a limit on how much it may use; it is a statement about who
# yields. Under contention Asterisk wins every time.
Nice=10
CPUWeight=20
IOWeight=20
IOSchedulingClass=idle

MemoryMax=192M
TasksMax=64

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload >/dev/null 2>&1
  echo "$UNIT_FILE"
}

svc_install_openrc() {
  cat > "$INITD_FILE" <<EOF
#!/sbin/openrc-run

name="PBXonix monitoring agent"
description="Reports PBX health to PBXonix Cloud"

command="$AGENT_BIN"
command_args="run --config $CONFIG_FILE"
command_user="$SERVICE_USER:$SERVICE_USER"
command_background=true
pidfile="$PIDFILE"
# See the note in the systemd unit: under contention Asterisk wins.
start_stop_daemon_args="--nicelevel 10 --ionice 3"
respawn_delay=10

depend() {
    need net
    after firewall
}
EOF
  chmod 0755 "$INITD_FILE"
  echo "$INITD_FILE"
}

svc_install_sysv() {
  cat > "$INITD_FILE" <<EOF
#!/bin/sh
### BEGIN INIT INFO
# Provides:          $SERVICE_NAME
# Required-Start:    \$network \$remote_fs
# Required-Stop:     \$network \$remote_fs
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: PBXonix monitoring agent
# Description:       Reports PBX health to PBXonix Cloud.
### END INIT INFO

# Written by the PBXonix installer. Deliberately plain: this file has to work
# on CentOS 6, on Devuan and on whatever an appliance vendor froze in 2014, so
# it uses no distribution helper library and keeps its own pid file.

NAME=$SERVICE_NAME
DAEMON="$AGENT_BIN"
DAEMON_ARGS="run --config $CONFIG_FILE"
USER=$SERVICE_USER
PIDFILE=$PIDFILE
LOGFILE=/var/log/\$NAME.log

running() {
    [ -f "\$PIDFILE" ] || return 1
    pid=\$(cat "\$PIDFILE" 2>/dev/null) || return 1
    [ -n "\$pid" ] || return 1
    kill -0 "\$pid" 2>/dev/null
}

start() {
    if running; then
        echo "\$NAME is already running"
        return 0
    fi
    # The agent appends; root creates it first so the service user can.
    touch "\$LOGFILE" 2>/dev/null || true
    chown "\$USER" "\$LOGFILE" 2>/dev/null || true
    printf 'Starting %s: ' "\$NAME"
    # See the note in the systemd unit: on a phone system the media path is
    # soft-realtime, so the agent takes the machine only when nothing else
    # wants it.
    su -s /bin/sh -c "exec $NICE_CMD \$DAEMON \$DAEMON_ARGS >> \$LOGFILE 2>&1 & echo \\\$!" "\$USER" > "\$PIDFILE"
    sleep 1
    if running; then echo "ok"; return 0; else echo "failed"; rm -f "\$PIDFILE"; return 1; fi
}

stop() {
    if ! running; then
        echo "\$NAME is not running"
        rm -f "\$PIDFILE"
        return 0
    fi
    printf 'Stopping %s: ' "\$NAME"
    pid=\$(cat "\$PIDFILE")
    kill "\$pid" 2>/dev/null
    # Give it ten seconds to leave on its own before insisting.
    i=0
    while [ \$i -lt 10 ] && kill -0 "\$pid" 2>/dev/null; do
        sleep 1
        i=\$((i + 1))
    done
    kill -0 "\$pid" 2>/dev/null && kill -9 "\$pid" 2>/dev/null
    rm -f "\$PIDFILE"
    echo "ok"
}

case "\$1" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; start ;;
    status)
        if running; then echo "\$NAME is running (pid \$(cat \$PIDFILE))"; exit 0
        else echo "\$NAME is not running"; exit 3; fi
        ;;
    *) echo "Usage: \$0 {start|stop|restart|status}" >&2; exit 2 ;;
esac
EOF
  chmod 0755 "$INITD_FILE"
  echo "$INITD_FILE"
}

svc_install() {
  case "$INIT" in
    systemd) svc_install_systemd ;;
    openrc)  svc_install_openrc ;;
    sysv)    svc_install_sysv ;;
    none)    : ;;
  esac
}

svc_enable() {
  case "$INIT" in
    systemd) systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true ;;
    openrc)  rc-update add "$SERVICE_NAME" default >/dev/null 2>&1 || true ;;
    sysv)
      if command -v chkconfig >/dev/null 2>&1; then
        chkconfig --add "$SERVICE_NAME" >/dev/null 2>&1 || true
        chkconfig "$SERVICE_NAME" on >/dev/null 2>&1 || true
      elif command -v update-rc.d >/dev/null 2>&1; then
        update-rc.d "$SERVICE_NAME" defaults >/dev/null 2>&1 || true
      fi
      ;;
  esac
}

# Always restart, never "start if stopped". On an already-running service the
# latter is a no-op, so an upgrade would install the new version and leave the
# old process serving -- and the version in the dashboard would be a lie.
svc_restart() {
  case "$INIT" in
    systemd) systemctl restart "$SERVICE_NAME" ;;
    openrc)  rc-service "$SERVICE_NAME" restart >/dev/null 2>&1 || rc-service "$SERVICE_NAME" start ;;
    sysv)    "$INITD_FILE" restart >/dev/null 2>&1 || "$INITD_FILE" start >/dev/null 2>&1 || true ;;
  esac
}

svc_is_active() {
  case "$INIT" in
    systemd) systemctl is-active --quiet "$SERVICE_NAME" ;;
    openrc)  rc-service "$SERVICE_NAME" status >/dev/null 2>&1 ;;
    sysv)    "$INITD_FILE" status >/dev/null 2>&1 ;;
  esac
}

# What the operator should type to restart it by hand. The unit path is not
# that command under any of the three.
svc_restart_hint() {
  case "$INIT" in
    systemd) echo "systemctl restart $SERVICE_NAME" ;;
    openrc)  echo "rc-service $SERVICE_NAME restart" ;;
    sysv)    echo "$INITD_FILE restart" ;;
    none)    echo "$AGENT_BIN run --config $CONFIG_FILE" ;;
  esac
}

svc_logs() {
  case "$INIT" in
    systemd) journalctl -u "$SERVICE_NAME" --no-pager --lines=30 2>/dev/null ;;
    *)       tail -n 30 "/var/log/${SERVICE_NAME}.log" 2>/dev/null ;;
  esac
}

svc_log_hint() {
  case "$INIT" in
    systemd) echo "journalctl -u $SERVICE_NAME -f" ;;
    *)       echo "tail -f /var/log/${SERVICE_NAME}.log" ;;
  esac
}

if [ "$INIT" = none ]; then
  warn "no init system on this machine, so no service will be installed"
else
  step "Installing the ${INIT} service"
  SERVICE_PATH="$(svc_install)"
  ok "$SERVICE_PATH"
fi

# ----------------------------------------------------------------- enroll --
CREDENTIALS="$CONFIG_DIR/credentials.json"
if [ -n "$TOKEN" ] && { [ ! -s "$CREDENTIALS" ] || [ "$FORCE_ENROLL" -eq 1 ]; }; then
  step "Enrolling with $PBXONIX_API"
  case "$INIT" in
    systemd) systemctl stop "$SERVICE_NAME" 2>/dev/null || true ;;
    openrc)  rc-service "$SERVICE_NAME" stop >/dev/null 2>&1 || true ;;
    sysv)    [ -x "$INITD_FILE" ] && "$INITD_FILE" stop >/dev/null 2>&1 || true ;;
    none)    : ;;
  esac
  # </dev/null everywhere a subprocess runs: this script usually arrives on
  # stdin from curl, and anything that reads stdin would eat the rest of the
  # installer.
  if ! "$AGENT_BIN" enroll --config "$CONFIG_FILE" --token "$TOKEN" </dev/null; then
    # The agent is already installed at this point, so say so. Otherwise this
    # reads as "the whole install failed" and people start over from scratch.
    # Enrollment tokens last 60 minutes, and an install that needed a detour --
    # a missing python3, a distro fix -- can easily outlive one.
    die "the agent is installed, but enrollment was refused.

     Tokens are single-use and expire 60 minutes after you create them, so the
     usual cause is simply that this one aged out. Create a fresh one in the
     dashboard (Add PBX -> the PBX you created -> enrollment token), then run
     either of these -- no need to reinstall:

       $AGENT_BIN enroll --config ${CONFIG_FILE} --token NEW_TOKEN
       $(svc_restart_hint)

     or just re-run this installer with the new token."
  fi
  chown root:"$SERVICE_USER" "$CREDENTIALS"
  chmod 0640 "$CREDENTIALS"
  ok "enrolled"
elif [ -s "$CREDENTIALS" ]; then
  ok "already enrolled (pass --re-enroll with a new --token to replace)"
else
  die "no enrollment token supplied and no existing credentials. Re-run with --token TOKEN"
fi

# --------------------------------------------------------------------- AMI --
# Without an AMI login the agent can report the machine but not the telephony:
# no trunks, no queues, no call counts. Asterisk will not answer until a user
# exists in manager.conf, and that file belongs to the PBX administrator -- so
# the installer offers to write it and does nothing unless told yes.
#
# What it adds is one read-only account restricted to loopback. It grants none
# of the permissions that could do harm: no originate, no command, no config.

# The prompt reads /dev/tty, not stdin. stdin is the installer itself arriving
# from curl -- reading it would consume the rest of the script.
# Opening it, not stat-ing it. /dev/tty is crw-rw-rw- on every box, so a test
# for -r and -w passes even where there is no controlling terminal to attach to
# -- a systemd unit, a cron job, an unattended pipe. Only the open fails.
#
# In a subshell, and with `true` rather than `:`. Both matter. `:` is a POSIX
# *special* built-in, and a redirection error on a special built-in aborts the
# entire shell rather than failing the command -- so the obvious spelling of
# this test killed the installer outright on every machine without a terminal,
# silently, after it had already enrolled but before it started the service.
# dash exits 2 and runs nothing further. The subshell contains any such abort;
# `true` is not special, so there is nothing to contain.
have_tty() { ( true < /dev/tty ) 2>/dev/null; }

confirm() {
  question=$1
  have_tty || return 1
  printf '%s [y/N]: ' "$question" > /dev/tty
  read -r reply < /dev/tty || return 1
  case "$reply" in
    y|Y|yes|YES|Yes) return 0 ;;
    *) return 1 ;;
  esac
}

# Set key = value inside manager.conf's [general] section, whether the key is
# there already, is there with the wrong value, or is missing entirely.
ini_set_general() {
  key=$1; value=$2; file=$3
  awk -v key="$key" -v value="$value" '
    BEGIN { in_general = 0; done = 0 }
    # Leaving [general] without having written the key: write it now, before
    # the next section header goes out.
    /^[ \t]*\[/ {
      if (in_general && !done) { print key " = " value; done = 1 }
      in_general = ($0 ~ /^[ \t]*\[general\]/)
      print
      next
    }
    { line = $0 }
    in_general && !done && line ~ ("^[ \t]*" key "[ \t]*=") {
      print key " = " value; done = 1; next
    }
    { print line }
    END { if (in_general && !done) print key " = " value }
  ' "$file" > "$file.pbxonix-new" || { rm -f "$file.pbxonix-new"; return 1; }
  # Through the existing inode, not over it: manager.conf holds AMI secrets and
  # its mode and owner are deliberate. A mv would hand it back as root:root 0644.
  cat "$file.pbxonix-new" > "$file"
  rm -f "$file.pbxonix-new"
}

configure_ami() {
  MANAGER_CONF="$ASTERISK_ETC/manager.conf"

  # Nothing to do on a box with no Asterisk: the agent still reports the
  # machine, and there is no config to add a user to.
  if [ ! -d "$ASTERISK_ETC" ] || [ ! -f "$MANAGER_CONF" ]; then
    return 0
  fi

  # Already ours from an earlier run. Re-generating the secret here would
  # invalidate the one Asterisk is holding, so it is left alone.
  if grep -l '^\[pbxonix\]' "$ASTERISK_ETC"/manager*.conf >/dev/null 2>&1; then
    ok "AMI already has a [pbxonix] user"
    ami_enable_in_agent_config
    return 0
  fi

  # FreePBX and Issabel regenerate manager.conf from their own database, so an
  # edit there is erased on the next reload. Both #include a _custom file that
  # exists precisely for additions like this one.
  if grep -q 'manager_custom.conf' "$MANAGER_CONF" 2>/dev/null; then
    AMI_TARGET="$ASTERISK_ETC/manager_custom.conf"
    AMI_NEEDS_INCLUDE=0
  else
    AMI_TARGET="$ASTERISK_ETC/manager_pbxonix.conf"
    AMI_NEEDS_INCLUDE=1
  fi

  # Is AMI switched on at all? FreePBX needs it for itself so it always is; a
  # plain Asterisk ships with it disabled.
  AMI_NEEDS_ENABLE=1
  if awk '
      /^[ \t]*\[/ { in_general = ($0 ~ /^[ \t]*\[general\]/) }
      in_general && /^[ \t]*enabled[ \t]*=[ \t]*(yes|true|1)/ { found = 1 }
      END { exit(found ? 0 : 1) }
    ' "$MANAGER_CONF"; then
    AMI_NEEDS_ENABLE=0
  fi

  # Told no on the command line: say so and stop, without first printing a
  # detailed proposal for a change that will not happen.
  if [ "$CONFIGURE_AMI" = "0" ]; then
    ok "skipping AMI configuration (--no-configure-ami)"
    ami_manual_hint
    return 0
  fi

  echo
  step "Asterisk Manager Interface"
  echo "    Without it the dashboard shows the server but no telephony:"
  echo "    no trunks, no queues, no call counts."
  echo
  echo "    Proposed changes to this PBX:"
  echo "      + $AMI_TARGET"
  echo "        a read-only AMI user 'pbxonix', loopback only (deny 0.0.0.0/0,"
  echo "        permit 127.0.0.1). No originate, no command, no config rights."
  [ "$AMI_NEEDS_INCLUDE" -eq 1 ] && \
  echo "      ~ $MANAGER_CONF: add '#include manager_pbxonix.conf'"
  if [ "$AMI_NEEDS_ENABLE" -eq 1 ]; then
  echo "      ~ $MANAGER_CONF: [general] enabled = yes, bindaddr = 127.0.0.1"
  echo "        (AMI is currently off. It is bound to loopback so switching it"
  echo "        on exposes nothing to the network.)"
  fi
  echo "      ~ $CONFIG_FILE: ami_enabled = true"
  echo "      + a backup of every file changed, alongside it"
  echo "      then: asterisk -rx 'manager reload'"
  echo
  echo "    The secret is generated locally and stored in $CREDENTIALS."
  echo "    PBXonix Cloud never receives it."
  echo

  if [ "$CONFIGURE_AMI" = "1" ]; then
    ok "configuring AMI (--configure-ami)"
  elif ! have_tty; then
    # Piped with no terminal anywhere -- an unattended install. Silence is not
    # consent for editing a phone system's configuration.
    warn "no terminal to ask on, so Asterisk configuration was left alone"
    ami_manual_hint
    return 0
  elif ! confirm "    Configure AMI now?"; then
    ok "left Asterisk configuration untouched"
    ami_manual_hint
    return 0
  fi

  STAMP="$(date +%Y%m%d%H%M%S)"
  # Generated into the scratch directory first. If this fails -- an unreadable
  # credentials file, a full disk -- Asterisk is still reading exactly what it
  # was reading before, and nothing has to be undone.
  if ! "$AGENT_BIN" ami-config --generate --config "$CONFIG_FILE" </dev/null > "$TMPDIR/manager_pbxonix.conf"; then
    warn "could not generate the AMI credential; Asterisk was not touched"
    ami_manual_hint
    return 0
  fi

  if [ -f "$AMI_TARGET" ]; then
    # An existing file is FreePBX's manager_custom.conf, which it ships and
    # owns. Back it up, append, and leave its mode and owner exactly as found.
    cp -p "$AMI_TARGET" "$AMI_TARGET.pbxonix-$STAMP"
    cat "$TMPDIR/manager_pbxonix.conf" >> "$AMI_TARGET"
  else
    cp "$TMPDIR/manager_pbxonix.conf" "$AMI_TARGET"
    # Ours, and it holds a password: readable by Asterisk and nobody else.
    # Ownership is copied from manager.conf rather than guessed at, because the
    # user Asterisk runs as differs between distributions and appliances.
    chmod 0640 "$AMI_TARGET"
    OWNER="$(ls -ld "$MANAGER_CONF" | awk '{print $3 ":" $4}')"
    chown "$OWNER" "$AMI_TARGET" 2>/dev/null || true
  fi
  ok "wrote $AMI_TARGET"

  if [ "$AMI_NEEDS_INCLUDE" -eq 1 ] || [ "$AMI_NEEDS_ENABLE" -eq 1 ]; then
    cp -p "$MANAGER_CONF" "$MANAGER_CONF.pbxonix-$STAMP"
    ok "backed up $MANAGER_CONF.pbxonix-$STAMP"
  fi

  if [ "$AMI_NEEDS_INCLUDE" -eq 1 ]; then
    printf '\n#include manager_pbxonix.conf\n' >> "$MANAGER_CONF"
    ok "added the #include to $MANAGER_CONF"
  fi

  if [ "$AMI_NEEDS_ENABLE" -eq 1 ]; then
    ini_set_general enabled yes "$MANAGER_CONF"
    # Only when we are the ones switching AMI on. If it was already enabled the
    # administrator chose that bind address deliberately and it is not ours to
    # change.
    ini_set_general bindaddr 127.0.0.1 "$MANAGER_CONF"
    ok "enabled AMI on loopback in $MANAGER_CONF"
  fi

  ami_enable_in_agent_config

  if command -v asterisk >/dev/null 2>&1 && asterisk -rx 'core show version' >/dev/null 2>&1; then
    if asterisk -rx 'manager reload' >/dev/null 2>&1; then
      ok "asterisk reloaded its manager configuration"
      # Ask Asterisk what it actually loaded rather than assuming the file
      # landed somewhere it reads. FreePBX and Issabel regenerate manager.conf
      # from their own database, and a regeneration between writing the
      # #include and reloading would drop it -- silently, leaving an agent that
      # authenticates against a user Asterisk has never heard of.
      if asterisk -rx 'manager show users' 2>/dev/null | grep -qw pbxonix; then
        ok "asterisk lists the pbxonix manager user"
      else
        warn "asterisk reloaded but does not list a 'pbxonix' manager user.
     The file was written to $AMI_TARGET but Asterisk is not reading it.
     On FreePBX or Issabel, check that manager.conf still carries its
     '#include manager_custom.conf' line -- a regeneration can drop additions.
     The agent will report ami_state=unreachable until this is resolved."
      fi
    else
      warn "could not reload Asterisk. Run: asterisk -rx 'manager reload'"
    fi
  else
    warn "asterisk is not running, so the new AMI user is not live yet.
     It will be picked up on the next start, or run now:
       asterisk -rx 'manager reload'"
  fi
}

# Flip the agent's own switch. Without this the credential exists on both sides
# and the agent still never connects.
ami_enable_in_agent_config() {
  if grep -q '^[ \t]*ami_enabled[ \t]*=[ \t]*true' "$CONFIG_FILE" 2>/dev/null; then
    return 0
  fi
  # Through the existing inode. `sed -i` renames a new file over the old one,
  # which would hand agent.conf back as root:root -- and the agent runs as
  # pbxonix, so it would silently stop being able to read its own config. That
  # is the same mistake that once left credentials.json unreadable.
  sed 's/^[ \t]*ami_enabled[ \t]*=.*/ami_enabled = true/' "$CONFIG_FILE" \
    > "$CONFIG_FILE.pbxonix-new" || { rm -f "$CONFIG_FILE.pbxonix-new"; return 1; }
  cat "$CONFIG_FILE.pbxonix-new" > "$CONFIG_FILE"
  rm -f "$CONFIG_FILE.pbxonix-new"
  ok "ami_enabled = true in $CONFIG_FILE"
}

ami_manual_hint() {
  echo
  echo "    To set AMI up later, on this machine:"
  echo "      $AGENT_BIN ami-config --generate"
  echo "    then follow the instructions it prints, or re-run this installer"
  echo "    with --configure-ami."
}

# Explicitly non-fatal. By this point the agent is installed and enrolled, and
# it monitors the machine perfectly well without AMI -- so an unexpected failure
# in here must not abort the install and leave the operator thinking nothing
# worked. Under `set -e` a bare call would do exactly that.
configure_ami || warn "the AMI step did not complete. The agent is installed and
     enrolled, and reports everything except trunks, queues and call counts.
     Run '$AGENT_BIN ami-config --generate' to set it up by hand."

# ------------------------------------------------------------------- start --
if [ "$INIT" = none ]; then
  echo
  ok "agent installed and enrolled"
  echo
  printf '%sNothing here supervises services%s, so the agent was not started.\n' "$C_WARN" "$C_OFF"
  printf '  run as %s:  %s run --config %s\n' "$SERVICE_USER" "$AGENT_BIN" "$CONFIG_FILE"
  printf '  config:      %s\n' "$CONFIG_FILE"
  exit 0
fi

step "Starting $SERVICE_NAME"
svc_enable
svc_restart
sleep 3

echo
if svc_is_active; then
  ok "$SERVICE_NAME is running"
  echo
  printf '%sPBXonix agent installed.%s The PBX should appear online in the dashboard within a minute.\n' "$C_OK" "$C_OFF"
  printf '  logs:    %s\n' "$(svc_log_hint)"
  printf '  config:  %s\n' "$CONFIG_FILE"
  printf '  service: %s (%s)\n' "$SERVICE_NAME" "$INIT"
else
  warn "$SERVICE_NAME did not start"
  svc_logs | sed 's/^/    /'
  exit 1
fi
}

# Nothing above this line has run yet. If the download was cut short, the shell
# never reaches here and the installer does nothing -- which is the point.
main "$@"
