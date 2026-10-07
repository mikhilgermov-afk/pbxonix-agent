"""Command line interface: enroll once, add an AMI credential, then run."""

import argparse
import getpass
import logging
import sys
import time
from typing import List, Optional

from pbxonix_agent import __version__, amiconf, facts
from pbxonix_agent import credentials as creds
from pbxonix_agent.config import DEFAULT_CONFIG_PATH, ConfigError
from pbxonix_agent.config import load as load_config
from pbxonix_agent.credentials import SERVICE_USER
from pbxonix_agent.transport import ApiError, Client, TransportError

log = logging.getLogger("pbxonix.agent")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        # journald already stamps the time and unit, so the format stays bare.
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pbxonix-agent", description="PBXonix monitoring agent")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")
    # required= is a 3.7+ keyword; setting the attribute works everywhere.
    sub.required = True

    enroll = sub.add_parser("enroll", help="exchange an enrollment token for a credential")
    enroll.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    enroll.add_argument("--token", required=True, help="single-use enrollment token")

    run = sub.add_parser("run", help="run the agent in the foreground")
    run.add_argument("--config", default=DEFAULT_CONFIG_PATH)

    check = sub.add_parser("check", help="print what the agent can see, and exit")
    check.add_argument("--config", default=DEFAULT_CONFIG_PATH)

    manager = sub.add_parser(
        "ami-config", help="print the manager.conf snippet Asterisk needs for AMI"
    )
    manager.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    manager.add_argument(
        "--generate",
        action="store_true",
        help="generate a secret, store it locally, and fill it in below",
    )
    manager.add_argument(
        "--username", default="pbxonix", help="AMI username to create (default: pbxonix)"
    )

    ami = sub.add_parser("set-ami", help="store the local AMI credential")
    ami.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    ami.add_argument("--username", default="pbxonix", help="AMI username (default: pbxonix)")
    ami.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the secret from stdin instead of prompting",
    )

    return parser


def cmd_enroll(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    _configure_logging(config.log_level)

    payload = {"token": args.token, "agent_version": __version__}
    payload.update({k: v for k, v in facts.collect().items() if v is not None})

    client = Client(config.api_base_url, verify_tls=config.verify_tls)
    try:
        response = client.post_with_retry("/v1/agent/enroll", payload, attempts=3)
    except ApiError as exc:
        log.error("enrollment refused: %s", exc.message or exc)
        return 1
    except TransportError as exc:
        log.error("could not reach %s: %s", config.api_base_url, exc)
        return 1

    # Preserve an AMI credential across a re-enrollment: it is unrelated to the
    # cloud token and re-entering it would be a pointless trip to the PBX.
    #
    # A file we cannot read means there is nothing to preserve -- it must never
    # mean enrollment fails. Enrolling is the operation that *establishes* this
    # file; refusing to do it because the old one is damaged leaves the operator
    # with no way forward that the error message even hints at. The write below
    # is atomic, so a truncated file arrives by other means: a filled disk, a
    # hand edit, a restore of the wrong thing.
    try:
        existing = creds.load(config.credentials_path)
    except creds.CredentialsError as exc:
        log.warning(
            "existing %s is unreadable (%s); enrolling anyway and starting fresh. "
            "Any stored AMI credential is lost -- re-run 'pbxonix-agent ami-config "
            "--generate' if this PBX uses AMI.",
            config.credentials_path,
            exc,
        )
        existing = None
    credential = creds.Credentials(
        pbx_id=response["pbx_id"],
        agent_token=response["agent_token"],
        pbx_name=response.get("pbx_name", ""),
        ami_username=existing.ami_username if existing else "",
        ami_password=existing.ami_password if existing else "",
    )
    creds.save(config.credentials_path, credential)

    print("Enrolled as '{}' ({})".format(credential.pbx_name, credential.pbx_id))
    print("Credential written to {}".format(config.credentials_path))
    if not credential.has_ami:
        print()
        print("Next, for SIP trunk and queue monitoring:")
        print("  1. pbxonix-agent ami-config --generate    # prints what Asterisk needs")
        print("  2. install that as /etc/asterisk/manager_pbxonix.conf, #include it")
        print("     from manager.conf, then: asterisk -rx 'manager reload'")
        print("  3. Set ami_enabled = true in {}, then restart".format(config.config_path))
    return 0


def cmd_ami_config(args: argparse.Namespace) -> int:
    """Print what Asterisk needs. Never write it.

    /etc/asterisk belongs to the PBX administrator, and a monitoring agent that
    edits the dialplan host's configuration is exactly the kind of thing nobody
    wants installed on a phone system. Printing keeps the change reviewable and
    the reload deliberate.
    """
    config = load_config(args.config)
    body = amiconf.MANAGER_CONF

    # Re-running this must not silently invalidate a secret already written into
    # manager.conf. Without --generate, an existing stored credential is filled
    # in rather than the placeholder, so the command is safe to run twice and
    # always prints what the agent will actually send.
    if not args.generate:
        try:
            existing = creds.load(config.credentials_path)
        except creds.CredentialsError:
            existing = None
        if existing is not None and existing.has_ami:
            body = body.replace(amiconf.SECRET_PLACEHOLDER, existing.ami_password)
            body = body.replace(amiconf.HOW_TO_GENERATE, amiconf.ALREADY_FILLED)
            if existing.ami_username != "pbxonix":
                body = body.replace("[pbxonix]", "[{}]".format(existing.ami_username))
            print(body)
            print("; ---------------------------------------------------------------")
            print("; This is the credential already stored in {}".format(
                config.credentials_path
            ))
            print("; Re-run with --generate only if you want to replace it -- doing so")
            print("; invalidates whatever is currently in manager.conf.")
            if not config.asterisk.ami_enabled:
                print(";")
                print("; Still to do: set ami_enabled = true in {}".format(
                    config.config_path
                ))
                print("; then: systemctl restart pbxonix-agent")
            return 0

    if args.generate:
        # 32 bytes from the OS CSPRNG, base64 for a config file. Local only --
        # this secret never leaves the machine.
        import base64
        import os as _os

        secret = base64.b64encode(_os.urandom(32)).decode("ascii")
        try:
            creds.set_ami(config.credentials_path, args.username, secret)
        except creds.CredentialsError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        body = body.replace(amiconf.SECRET_PLACEHOLDER, secret)
        body = body.replace(amiconf.HOW_TO_GENERATE, amiconf.ALREADY_FILLED)
        if args.username != "pbxonix":
            body = body.replace("[pbxonix]", "[{}]".format(args.username))

    print(body)

    if args.generate:
        print("; ---------------------------------------------------------------")
        print("; The secret above is already stored in {}".format(config.credentials_path))
        print("; It stays on this machine. PBXonix Cloud never receives it.")
        if not config.asterisk.ami_enabled:
            print(";")
            print("; Still to do: set ami_enabled = true in {}".format(config.config_path))
            print("; then: systemctl restart pbxonix-agent")
    return 0


def cmd_set_ami(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    _configure_logging(config.log_level)

    if args.password_stdin:
        secret = sys.stdin.readline().rstrip("\n")
    else:
        # getpass, never argv: a secret on the command line lands in the shell
        # history and in every other user's `ps` output.
        secret = getpass.getpass("AMI secret for '{}': ".format(args.username))

    if not secret:
        print("no secret supplied", file=sys.stderr)
        return 1

    try:
        creds.set_ami(config.credentials_path, args.username, secret)
    except creds.CredentialsError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print("AMI credential stored in {} (0640 root:pbxonix)".format(config.credentials_path))
    print("It stays on this machine. PBXonix Cloud never receives it.")

    if not config.asterisk.ami_enabled:
        print()
        print("ami_enabled is still false in {}.".format(config.config_path))
        print("Set it to true and restart: systemctl restart pbxonix-agent")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from pbxonix_agent.runner import Agent

    config = load_config(args.config)
    _configure_logging(config.log_level)

    try:
        agent = Agent(config)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    agent.install_signal_handlers()
    return agent.run()


def cmd_check(args: argparse.Namespace) -> int:
    from pbxonix_agent.collectors import asterisk as asterisk_collector
    from pbxonix_agent.collectors import recordings as recording_collector
    from pbxonix_agent.collectors import snapshot as ami_snapshot
    from pbxonix_agent.collectors import system as system_collector

    config = load_config(args.config)
    _configure_logging(config.log_level)

    print("host facts:")
    for key, value in sorted(facts.collect().items()):
        print("  {:<18} {}".format(key, value))

    sampler = system_collector.CpuSampler()
    sampler.sample()
    time.sleep(1)

    print("\nsystem metrics:")
    for key, value in sorted(system_collector.collect(sampler).items()):
        print("  {:<18} {}".format(key, value))

    print("\nasterisk:")
    for key, value in sorted(asterisk_collector.collect().items()):
        print("  {:<18} {}".format(key, value))

    # A blank active_calls means one of two very different things and the
    # dashboard cannot tell them apart. Say which one this is.
    reason = asterisk_collector.cli_error()
    if reason:
        print("  {:<18} unavailable -- {}".format("cli", reason))
        print("    Active call and channel counts come from the local Asterisk")
        print("    CLI and stay blank until this works. The service runs as")
        print("    {}, so test it as that user rather than as root:".format(
            SERVICE_USER
        ))
        print("      sudo -u {} asterisk -rx 'core show channels count'".format(
            SERVICE_USER
        ))
    elif asterisk_collector.cli_attempted():
        print("  {:<18} ok".format("cli"))
    else:
        print("  {:<18} not asked -- Asterisk is not running, or the agent".format("cli"))
        print("                     cannot tell whether it is")

    credential = creds.load(config.credentials_path)
    print("\nenrolled: {}".format("yes" if credential else "no"))
    if credential:
        print("  pbx_id           {}".format(credential.pbx_id))
        print("  pbx_name         {}".format(credential.pbx_name))

    print("\nami (SIP trunks and queues):")
    has_ami = bool(credential and credential.has_ami)
    if not config.asterisk.ami_enabled:
        print("  disabled -- set ami_enabled = true in {}".format(config.config_path))
    elif not has_ami:
        print("  enabled, but no local AMI credential is stored")
    else:
        # Configuration is not the same as a working connection. Reporting
        # "enabled, credential present" while the agent was being refused is
        # what sent the last diagnosis in the wrong direction.
        print("  enabled, credential present, {}:{}".format(
            config.asterisk.ami_host, config.asterisk.ami_port
        ))
        try:
            result = ami_snapshot.collect(
                host=config.asterisk.ami_host,
                port=config.asterisk.ami_port,
                username=credential.ami_username,
                secret=credential.ami_password,
            )
        # Deliberately broad: this is a diagnostic, and a failure the operator
        # cannot see is worse than any exception type purity.
        except Exception as exc:
            print("  connection FAILED: {}".format(exc))
            print("  If this says the login was rejected, check the ACL order in")
            print("  manager_pbxonix.conf: Asterisk applies the last matching rule,")
            print("  so 'deny' must come before 'permit'.")
        else:
            core = result.get("core") or {}
            counts = result.get("endpoints") or {}
            trunks = result.get("trunks") or []
            queues = result.get("queues") or []
            print("  connected")
            print("  endpoints        {}/{} online".format(
                counts.get("sip_endpoints_online"), counts.get("sip_endpoints_total")
            ))
            print("  active calls     {} (read over AMI, not the CLI)".format(
                core.get("active_calls")
            ))
            print("  active channels  {}".format(core.get("active_channels")))
            print("  trunks           {} found".format(len(trunks)))
            for trunk in trunks:
                print("    {:<26} {:<8} {:<14} {}".format(
                    trunk.get("name", "?"),
                    trunk.get("technology", "?"),
                    trunk.get("state", "?"),
                    trunk.get("detail") or "",
                ))
            print("  queues           {} found".format(len(queues)))
            for queue in queues:
                print("    {:<26} {} waiting, {}/{} agents".format(
                    queue.get("name", "?"),
                    queue.get("calls_waiting"),
                    queue.get("agents_available"),
                    queue.get("agents_logged_in"),
                ))
    if not (config.asterisk.ami_enabled and has_ami):
        print("  SIP trunks and queues stay empty in the dashboard until both are set.")

    print("\nrecordings:")
    if not config.recordings.paths:
        print("  (no paths configured)")
    else:
        for path in config.recordings.paths:
            print("  path             {}".format(path))
        scanner = recording_collector.RecordingScanner(
            config.recordings.paths,
            max_seconds=config.recordings.max_scan_seconds,
            max_entries=config.recordings.max_entries,
        )
        # A first scan has no watermark, so new_files is 0 by definition.
        found = scanner.collect() or {}
        for key in (
            "total_files",
            "total_size_bytes",
            "last_recording_at",
            "truncated",
            "paths_missing",
            "scan_seconds",
        ):
            value = found.get(key)
            if key == "last_recording_at" and value:
                value = "{} ({})".format(
                    value, time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(value))
                )
            print("  {:<16} {}".format(key, value))

    return 0

def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "ami-config": cmd_ami_config,
        "enroll": cmd_enroll,
        "run": cmd_run,
        "check": cmd_check,
        "set-ami": cmd_set_ami,
    }
    try:
        return handlers[args.command](args)
    except ConfigError as exc:
        print("configuration error: {}".format(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
