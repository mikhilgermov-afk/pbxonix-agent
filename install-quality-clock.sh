#!/bin/sh
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
