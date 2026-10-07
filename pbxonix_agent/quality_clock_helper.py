"""Socket-activated codec-clock reader. Its only output is a numeric RTP clock.

Installed root-owned outside the agent venv and run as the Asterisk socket owner.
Never import agent configuration or accept arbitrary commands or filesystem paths.
"""

import os
import re
import subprocess
import sys

CHANNEL = re.compile(r"^(?:SIP|PJSIP)/[A-Za-z0-9_.@+:-]{1,200}-[a-fA-F0-9]{6,}$")
CLOCKS = {
    "ulaw": 8000,
    "alaw": 8000,
    "gsm": 8000,
    "g729": 8000,
    "g722": 8000,
    "g726": 8000,
    "g726aal2": 8000,
    "g723": 8000,
    "ilbc": 8000,
    "opus": 48000,
}


def codec_clock(text):
    match = re.search(r"^\s*NativeFormats:\s*\(([^)]+)\)\s*$", text, re.M)
    if not match:
        return None
    clocks = {CLOCKS.get(value.strip()) for value in match.group(1).split("|")}
    return clocks.pop() if len(clocks) == 1 and None not in clocks else None


def read_clock(channel):
    if not CHANNEL.fullmatch(channel):
        return None
    binary = next(
        (
            p
            for p in ("/usr/sbin/asterisk", "/usr/bin/asterisk", "/sbin/asterisk")
            if os.path.isfile(p)
        ),
        None,
    )
    if binary is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 -- fixed binary, strict channel grammar, no shell
            [binary, "-rx", "core show channel " + channel],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
            timeout=1,
            check=False,
        )
        return codec_clock(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def main():
    try:
        data = sys.stdin.buffer.readline(256)
        channel = data[:-1].decode("ascii") if data.endswith(b"\n") else ""
        value = read_clock(channel)
    except Exception:
        value = None
    sys.stdout.write(str(value or 0) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
