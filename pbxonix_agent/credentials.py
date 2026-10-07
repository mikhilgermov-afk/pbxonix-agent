"""Persistence of local credentials.

The file is written 0640 root:pbxonix. The agent runs as pbxonix and can read
it; nothing else on the box can.

It holds two unrelated things:

* the cloud credential the agent was issued at enrollment, and
* the AMI username and secret, which are **local only**. They are never sent to
  PBXonix Cloud, never logged, and never leave this machine.
"""

import json
import os
import tempfile
from typing import Any, Dict, Optional

# The account the systemd unit runs as. Group ownership -- not the mode -- is
# what decides whether the running agent can read what enrollment just wrote.
SERVICE_GROUP = "pbxonix"
SERVICE_USER = "pbxonix"


class CredentialsError(Exception):
    pass


class Credentials:
    def __init__(
        self,
        pbx_id: str,
        agent_token: str,
        pbx_name: str = "",
        ami_username: str = "",
        ami_password: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.pbx_id = pbx_id
        self.agent_token = agent_token
        self.pbx_name = pbx_name
        self.ami_username = ami_username
        self.ami_password = ami_password
        # Anything a newer agent wrote that this one does not understand. Kept
        # so a downgrade cannot silently discard configuration.
        self.extra = extra or {}

    @property
    def has_ami(self) -> bool:
        return bool(self.ami_username and self.ami_password)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = dict(self.extra)
        data.update(
            {
                "pbx_id": self.pbx_id,
                "pbx_name": self.pbx_name,
                "agent_token": self.agent_token,
            }
        )
        if self.ami_username:
            data["ami_username"] = self.ami_username
        if self.ami_password:
            data["ami_password"] = self.ami_password
        return data

    def __repr__(self) -> str:
        # Deliberate: a stray repr() in a log line must not print secrets.
        return "Credentials(pbx_id={!r}, ami={})".format(
            self.pbx_id, "set" if self.has_ami else "unset"
        )


_KNOWN = {"pbx_id", "pbx_name", "agent_token", "ami_username", "ami_password"}


def load(path: str) -> Optional[Credentials]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise CredentialsError("could not read {}: {}".format(path, exc)) from exc

    if not isinstance(data, dict):
        raise CredentialsError("{} does not contain a JSON object".format(path))

    token = data.get("agent_token")
    pbx_id = data.get("pbx_id")
    if not token or not pbx_id:
        raise CredentialsError("{} is missing pbx_id or agent_token".format(path))

    return Credentials(
        pbx_id=pbx_id,
        agent_token=token,
        pbx_name=data.get("pbx_name", ""),
        ami_username=data.get("ami_username", ""),
        ami_password=data.get("ami_password", ""),
        extra={k: v for k, v in data.items() if k not in _KNOWN},
    )


def _give_service_account_access(path: str) -> None:
    """Set group ownership so the agent can read its own credential.

    The file is 0640 and written by root, so without this it is root:root and
    the service -- which runs as pbxonix -- cannot read it. The installer used
    to patch this up afterwards, which meant running `pbxonix-agent enroll` by
    hand produced a credential the agent could not use, and the service failed
    to start for a reason that pointed nowhere near the cause.

    Best effort on purpose. Not root, no such group, or a platform without
    either all mean this is not the packaged service, and the mode alone is
    already correct.
    """
    try:
        import grp

        gid = grp.getgrnam(SERVICE_GROUP).gr_gid
    except (ImportError, KeyError):
        return
    try:
        os.chown(path, -1, gid)  # -1 leaves the owner alone
    except (OSError, AttributeError):
        return


def save(path: str, credentials: Credentials) -> None:
    """Write atomically so an interrupted write cannot leave a half-file."""
    directory = os.path.dirname(path) or "."
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory, delete=False, prefix=".credentials-"
    )
    try:
        # Restrict before any content is written, not after.
        os.chmod(handle.name, 0o640)
        json.dump(credentials.to_dict(), handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, path)
        _give_service_account_access(path)
    except BaseException:
        handle.close()
        if os.path.exists(handle.name):
            os.unlink(handle.name)
        raise


def set_ami(path: str, username: str, password: str) -> Credentials:
    """Add or replace the AMI credential, preserving everything else."""
    existing = load(path)
    if existing is None:
        raise CredentialsError(
            "not enrolled yet: {} does not exist. Run 'pbxonix-agent enroll' first".format(path)
        )
    existing.ami_username = username
    existing.ami_password = password
    save(path, existing)
    return existing
