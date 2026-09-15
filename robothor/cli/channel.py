"""``genus channel`` — what can deliver, whether it works, and adding a token.

A channel was configurable only by knowing which environment variables to
export, and provable only by scheduling an agent and waiting to see whether a
message arrived. Between those two sat every setup failure Slack has: a bot
token pasted from the app-token field, an app installed without
``chat:write``, a workspace the bot was never invited into. None of them are
visible until a briefing does not turn up, and by then the evidence is a
``failed:`` in ``agent_runs`` and a question about which of four things went
wrong.

So: ``list`` says what a manifest could name, ``verify`` proves a channel works
one step at a time, and ``add`` writes the credential somewhere the platform
actually reads it.

Nothing here prints a credential. ``add`` reports the destination and a
fingerprint — a SHA-256 prefix, which identifies the value without carrying any
of it — and it **refuses** a token passed as a flag rather than pretending to
clean up after one: ``ps`` and ``/proc/<pid>/cmdline`` show a command line to
every account on the box, the shell has already written it to a history file,
and reassigning ``sys.argv`` (what the first cut did, and called a scrub) leaves
both of those untouched. The prompt and the environment are the two paths that
do not publish the value, and the refusal names them.
"""

from __future__ import annotations

import argparse  # noqa: TC003 - argparse.Namespace is used at runtime in signatures
import asyncio
import hashlib
import inspect
import json
import logging
import sys
from pathlib import Path
from typing import Any

from robothor.engine.channels.base import UNCONFIGURED_STEP
from robothor.engine.channels.slack_credentials import environment_token

logger = logging.getLogger(__name__)

__all__ = ["cmd_channel"]

#: The channels ``add`` knows how to configure, and the fields each one needs.
#: Telegram is deliberately absent: ``genus init`` and the Telegram skill own
#: that flow, and a second way to write the same credential is a second thing
#: to keep in step.
_ADDABLE: dict[str, tuple[tuple[str, str, str], ...]] = {
    # channel -> ((attribute, vault field, environment name), ...)
    "slack": (
        ("bot_token", "bot_token", "ROBOTHOR_SLACK_BOT_TOKEN"),
        ("app_token", "app_token", "ROBOTHOR_SLACK_APP_TOKEN"),
    ),
    # Email has ONE credential. The host, port, user and from-address are
    # settings below, not vault rows: they are not secrets, and putting them
    # behind a master key would make them unreadable by the doctor on an
    # instance that has no vault.
    "email": (("smtp_password", "smtp_password", "ROBOTHOR_EMAIL_SMTP_PASSWORD"),),
    # A plugin channel (`genus-teams`), configured through the same table as a
    # built-in. If a channel that ships as a package needed its own branch
    # here, the first one would also be the first whose credentials an operator
    # had to export by hand. ONE credential: the application id and the
    # directory tenant id are not secrets and are settings below.
    "teams": (("app_password", "app_password", "ROBOTHOR_TEAMS_APP_PASSWORD"),),
}

#: Per-channel non-secret settings ``add`` can write. ``(flag attribute, group,
#: field, environment name)``.
_ADDABLE_SETTINGS: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    "slack": (
        ("verify_target", "channels", "slack_verify_target", "ROBOTHOR_SLACK_VERIFY_TARGET"),
    ),
    "email": (
        ("from_address", "channels", "email_from", "ROBOTHOR_EMAIL_FROM"),
        ("smtp_host", "channels", "email_smtp_host", "ROBOTHOR_EMAIL_SMTP_HOST"),
        ("smtp_port", "channels", "email_smtp_port", "ROBOTHOR_EMAIL_SMTP_PORT"),
        ("smtp_starttls", "channels", "email_smtp_starttls", "ROBOTHOR_EMAIL_SMTP_STARTTLS"),
        ("smtp_user", "channels", "email_smtp_user", "ROBOTHOR_EMAIL_SMTP_USER"),
    ),
    "teams": (
        ("app_id", "channels", "teams_app_id", "ROBOTHOR_TEAMS_APP_ID"),
        ("tenant_id", "channels", "teams_tenant_id", "ROBOTHOR_TEAMS_TENANT_ID"),
        ("verify_target", "channels", "teams_verify_target", "ROBOTHOR_TEAMS_VERIFY_TARGET"),
    ),
}

#: The flags that exist only to be REFUSED, per channel. A map rather than the
#: hard-coded Slack pair this used to be: with one channel it read as a list of
#: every credential flag the command has, and with two it was a list of Slack's
#: — so an operator who put an SMTP password on a command line was told to
#: export a Slack bot token instead, and the advice that mattered (rotate it)
#: named the wrong console.
_REFUSED_FLAGS: dict[str, tuple[tuple[str, str], ...]] = {
    "slack": (("--bot-token", "bot_token"), ("--app-token", "app_token")),
    "email": (("--smtp-password", "smtp_password"),),
    "teams": (("--app-password", "app_password"),),
}


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _workspace() -> Path:
    """This instance's workspace.

    Resolved through ``settings.sources``, not ``os.environ``, so there is one
    answer to "where is this instance" and the env-read ratchet in
    ``tests/test_settings_registry.py`` keeps counting sites elsewhere.
    """
    from robothor.settings.sources import workspace_path

    return workspace_path() or Path("robothor")


def _fingerprint(value: str) -> str:
    """Enough to tell two credentials apart, and nothing of either.

    A prefix of the value itself would be worse than useless: Slack tokens
    share their first two segments, so ``xoxb-1234…`` identifies the workspace
    while proving nothing about which token this is.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _vault_set(key: str, value: str) -> None:
    """Store one credential in the instance vault. The seam the suite replaces."""
    from robothor.vault import set as vault_set

    vault_set(key, value, category="credential")


def _has_master_key(workspace: Path) -> bool:
    """Whether this instance has a vault at all.

    Checked as a file rather than through ``get_master_key``, which caches the
    key in a module global for the life of the process — a cache a CLI that may
    go on to write a different workspace has no business populating.
    """
    return (workspace / ".vault-key").is_file()


def _write_env_file(workspace: Path, pairs: dict[str, str]) -> Path:
    """Upsert ``pairs`` into ``<workspace>/genus.env`` at mode 0600.

    Lines the write supersedes are REMOVED rather than shadowed. A rotated
    token left behind in the file is still a readable credential, and
    ``parse_env_file``'s last-one-wins would make it invisible as well as live.
    Everything else in the file — comments, the database password ``genus
    init`` wrote — is preserved verbatim.

    **A symlink at that path is refused, not followed.**
    ``secrets/env_file.py::load_instance_env`` refuses one on READ with the
    rationale "it is the shape of 'make the daemon read a file it would not
    otherwise open'". This is the other half of that rule, and it was missing:
    ``read_text()`` follows the link, so the linked-to file's contents were
    carried over into the new ``genus.env`` verbatim, and ``write_private``'s
    rename then replaced the symlink with a regular file. Anything the operator
    could read and an attacker could point at was copied into the one file
    ``load_instance_env`` ``setdefault``s straight into the engine's
    environment.

    Raises:
        OSError: for a symlink at the path, and for any failure of the read or
            the write. The caller reports it; nothing here prints, because an
            exception carrying a path is safe and a frame carrying a token is
            not.
    """
    from robothor.secrets.env_file import env_line, instance_env_path, write_private

    path = instance_env_path(workspace)
    if path.is_symlink():
        raise OSError(
            f"{path} is a symlink; refusing to write this instance's credentials "
            "through one. Replace it with the file itself."
        )
    kept: list[str] = []
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            name = raw.strip().removeprefix("export ").partition("=")[0].strip()
            if name in pairs:
                continue
            kept.append(raw)
    body = "\n".join([*kept, *(env_line(name, value) for name, value in pairs.items())])
    path.parent.mkdir(parents=True, exist_ok=True)
    write_private(path, body + "\n")
    return path


def cmd_channel(args: argparse.Namespace) -> int:
    """Dispatch ``genus channel``."""
    sub = getattr(args, "channel_command", None)
    if sub == "list":
        return _cmd_list(args)
    if sub == "verify":
        return _cmd_verify(args)
    if sub == "add":
        return _cmd_add(args)
    print("Usage: genus channel {list|verify|add}")
    return 0


# ── list ─────────────────────────────────────────────────────────────────────


def _cmd_list(args: argparse.Namespace) -> int:
    """Every channel a manifest's ``delivery.channel`` could resolve to.

    The question ``failed:no_channel:<name>`` leaves an operator asking, and it
    is not answerable from the settings reference: a plugin channel appears
    here only once it is both installed AND named in ``ROBOTHOR_CHANNELS``.
    """
    from robothor.engine.channels import list_channels

    channels = list_channels()
    reports: dict[str, dict[str, Any]] = {}
    for name, channel in sorted(channels.items()):
        try:
            reports[name] = asyncio.run(channel.health())
        except Exception as exc:  # noqa: BLE001 — one broken channel is not a broken list
            reports[name] = {"channel": name, "error": f"{type(exc).__name__}: {exc}"}

    if getattr(args, "json", False):
        print(json.dumps(reports, indent=2, sort_keys=True, default=str))
        return 0

    for name, report in reports.items():
        detail = ", ".join(f"{k}={v}" for k, v in sorted(report.items()) if k != "channel")
        print(f"  {name:<12} {detail}")
    print(f"\n{len(reports)} channel(s)")
    return 0


# ── verify ───────────────────────────────────────────────────────────────────


def _cmd_verify(args: argparse.Namespace) -> int:
    """Prove a channel works, step by step.

    Exit codes are the contract, because this is a command scripts wrap:

    * ``0`` — every step passed.
    * ``1`` — a step failed. Something is configured and wrong.
    * ``2`` — there was nothing to verify: the name resolves to nothing, the
      channel declares no ``verify``, or this instance has not configured it.
      Deliberately NOT 1: an instance that never wanted Slack has not failed a
      check it did not ask for, and an install gate that treated the two alike
      would fail every headless deployment.

    "Not configured" is read off ``verify``'s own answer rather than from a
    separate ``health()`` call: a channel signals it by returning a single step
    named ``configuration``. The first cut asked ``health()`` first, which cost
    a second ``auth.test`` round trip per run, and read ``configured`` from a
    call that also leaves the box — so a channel whose ``health`` raised got
    reported as verifiable.
    """
    from robothor.engine.channels import get_channel

    name = (getattr(args, "name", "") or "").strip()
    channel = get_channel(name)
    if channel is None:
        _err(
            f"No channel named {name!r} is registered. `genus channel list` shows "
            "what a manifest can name; a plugin channel also has to be named in "
            "ROBOTHOR_CHANNELS."
        )
        return 2

    verify = getattr(channel, "verify", None)
    if not callable(verify):
        _err(
            f"Channel {name!r} declares no verify step, so there is nothing to prove "
            "here. Reporting a pass would be a green result nobody checked anything for."
        )
        return 2

    # ``--tenant`` reaches only a channel that declares it. The opt-out list the
    # email channel clears ``--to`` against is per-tenant, and a channel that
    # takes no tenant must not be handed one rather than be given a keyword it
    # would reject — a verify that raises on an unrelated channel would be worse
    # than the flag being ignored there.
    tenant = (getattr(args, "tenant", None) or "").strip()
    extra: dict[str, Any] = {}
    if tenant and "tenant" in inspect.signature(verify).parameters:
        extra["tenant"] = tenant

    try:
        steps = asyncio.run(verify(getattr(args, "target", None), **extra))
    except Exception as exc:  # noqa: BLE001 — a broken channel is a report, not a traceback
        _err(f"Channel {name!r} raised while verifying: {type(exc).__name__}: {exc}")
        return 1

    rows = [(str(step), bool(ok), str(detail)) for step, ok, detail in steps]
    if len(rows) == 1 and rows[0][0] == UNCONFIGURED_STEP and not rows[0][1]:
        _err(f"Channel {name!r} is not configured on this instance: {rows[0][2]}")
        return 2
    if getattr(args, "json", False):
        print(
            json.dumps(
                {"channel": name, "steps": [{"step": s, "ok": o, "detail": d} for s, o, d in rows]},
                indent=2,
            )
        )
    else:
        for step, ok, detail in rows:
            print(f"  {'PASS' if ok else 'FAIL'}  {step:<24} {detail}")
    failed = [step for step, ok, _detail in rows if not ok]
    if failed:
        if not getattr(args, "json", False):
            print(f"\n{len(failed)} step(s) failed: {', '.join(failed)}")
        return 1
    if not getattr(args, "json", False):
        print(f"\n{len(rows)} step(s) passed")
    return 0


# ── add ──────────────────────────────────────────────────────────────────────


def _collect(args: argparse.Namespace, name: str) -> dict[str, tuple[str, str, str]]:
    """Resolve each credential from a flag, the environment, or a prompt.

    Prompted with ``getpass``, never ``input``: ``input`` echoes, and a token
    on a terminal is a token in a screen share. ``genus vault set`` set this
    precedent and this follows it.

    Prompting is skipped when stdin is not a terminal. There is nobody there to
    answer in an install script or a CI step, and a ``getpass`` on a closed
    stdin does not fail cleanly — it warns that it cannot control echo and then
    raises on read, which reads like a broken command rather than a missing
    argument.
    """
    import getpass

    interactive = sys.stdin.isatty()
    resolved: dict[str, tuple[str, str, str]] = {}
    for attribute, field, env in _ADDABLE[name]:
        # The environment layer alone. Deliberately not the full secrets
        # resolver: `add` is deciding what to STORE, and answering from the
        # vault would have `--to env` copy a vault row into the instance env
        # file — a credential moving somewhere nobody asked.
        value = environment_token(env)
        source = "environment"
        if not value and interactive:
            value = getpass.getpass(f"{name} {attribute.replace('_', ' ')} (not echoed): ").strip()
            source = "prompt"
        if value:
            resolved[attribute] = (value, field, env)
            logger.debug("genus channel add: %s resolved from the %s", env, source)
    return resolved


def _refuse_command_line_tokens(args: argparse.Namespace, name: str) -> str:
    """The refusal for a token passed as a flag, or "" when none was.

    There is no taking it back. ``ps`` shows any account on the box the full
    command line of every process, ``/proc/<pid>/cmdline`` is the same data, and
    the shell has already written it to a history file. Reassigning ``sys.argv``
    — which is what the first cut of this command did, and called a scrub —
    rebinds a Python list and leaves the kernel's copy exactly as it was;
    ``test_a_python_level_scrub_cannot_clear_proc_cmdline`` proves that rather
    than asserting it. Clearing the real argv needs ``setproctitle`` or
    ``prctl(PR_SET_MM)`` and a capability, neither of which this platform has.

    So the flags exist only to be refused. They stay DECLARED on the parser on
    purpose: argparse's "unrecognized arguments" error prints the offending
    tokens to stderr, so removing them would leak the credential through the
    error message for a flag that no longer exists.

    EVERY channel's flags are checked, not just the named channel's: the name
    is checked after this, so a credential passed alongside a misspelled channel
    must still be refused rather than reported as a typo.

    Checked before the channel name, because a misspelled name is a typo and a
    published credential is an incident.
    """
    offending: dict[str, list[str]] = {}
    for channel, flags in _REFUSED_FLAGS.items():
        for flag, attribute in flags:
            if (getattr(args, attribute, None) or "").strip():
                offending.setdefault(channel, []).append(flag)
    if not offending:
        return ""
    offenders = sorted({flag for flags in offending.values() for flag in flags})
    # The safe environment names to NAME are the ones for the channel the
    # operator is configuring; when the name is a typo, the ones belonging to
    # the flags they actually used.
    channels = [name] if name in _ADDABLE else sorted(offending)
    envs = ", ".join(
        sorted({env for channel in channels for _a, _f, env in _ADDABLE.get(channel, ())})
    )
    return (
        f"Refusing {' and '.join(offenders)}: a credential on a command line is readable "
        "by every account on this box through `ps` and /proc/<pid>/cmdline, and is "
        "already in your shell history. Nothing this command does afterwards can take "
        "that back.\n"
        "Run `genus channel add` with no credential flag and paste it at the prompt (it "
        f"is not echoed), or export it first ({envs}).\n"
        "If it has already been on a command line, rotate it wherever it was issued "
        "rather than storing it."
    )


def _cmd_add(args: argparse.Namespace) -> int:
    """Store a channel's credentials where the platform reads them.

    The vault when this instance has a master key, the instance env file
    otherwise — and the destination is printed either way. A vault-only ``add``
    fails on the most common install (no master key, no database), and an
    env-only one contradicts the naming the rest of the platform resolves
    through.
    """
    name = (getattr(args, "name", "") or "").strip().lower()

    # BEFORE the name check: a misspelled channel is a typo, a published
    # credential is an incident, and returning 2 for the typo first is how the
    # operator never heard about the second.
    refusal = _refuse_command_line_tokens(args, name)
    if refusal:
        _err(refusal)
        return 2

    if name not in _ADDABLE:
        _err(
            f"`genus channel add` does not know how to configure {name!r}. "
            f"Known: {', '.join(sorted(_ADDABLE))}."
        )
        return 2

    resolved = _collect(args, name)

    if not resolved:
        _err(f"No credentials given for {name!r}; nothing was written.")
        return 1

    workspace = _workspace()
    requested = (getattr(args, "to", None) or "").strip().lower()
    destination = requested or ("vault" if _has_master_key(workspace) else "env")

    written: list[str] = []
    if destination == "vault":
        from robothor.vault.naming import channel_field

        for value, field, _env in resolved.values():
            key = channel_field(name, field)
            try:
                _vault_set(key, value)
            except Exception as exc:  # noqa: BLE001
                # Say what DID land first. These are written one at a time, so a
                # refusal on the second leaves the first stored — and an
                # operator who is told only "the vault refused the app token"
                # will re-run the whole command, or worse, assume nothing was
                # written and go looking in the wrong place.
                for line in written:
                    print(line)
                _err(f"The vault refused {key}: {type(exc).__name__}: {exc}")
                if written:
                    _err(f"{len(written)} credential(s) above WERE stored; the rest were not.")
                return 1
            written.append(f"  vault  {key}  (sha256:{_fingerprint(value)})")
    else:
        try:
            path = _write_env_file(
                workspace, {env: value for value, _field, env in resolved.values()}
            )
        except OSError as exc:
            # Caught, never propagated. An uncaught exception here would carry a
            # traceback whose frame locals hold both plaintext tokens, and any
            # `rich`/Sentry/`--verbose` handler anyone bolts on later prints
            # frame locals. `exc` names the path, not the value; binding it to a
            # message and returning drops the traceback with the frames.
            _err(f"Could not write the instance credential file: {exc}")
            _err("Nothing was stored. Fix the permissions on that directory and try again.")
            return 1
        written.extend(
            f"  file   {env}  (sha256:{_fingerprint(value)})"
            for value, _field, env in resolved.values()
        )
        written.append(f"  in     {path} (mode 600)")

    for line in written:
        print(line)

    settings_written = _write_settings(args, name)
    for line in settings_written:
        print(line)

    _note_if_not_installed(name)

    print(
        f"\nWrote {len(resolved)} credential(s) for {name!r} to the "
        f"{'vault' if destination == 'vault' else 'instance env file'}. "
        f"Prove it works with: genus channel verify {name}"
    )
    if destination == "env":
        print(
            "The engine reads that file at start, so restart robothor-engine "
            "(or re-run `genus config apply`) before the change takes effect."
        )
    return 0


def _note_if_not_installed(name: str) -> None:
    """Say so when the credentials are for a channel this instance cannot use.

    ``add`` knows a channel that ships as a PLUGIN (``teams``), because the
    alternative is an operator exporting its credentials by hand. But storing a
    credential for something that is not installed looks exactly like a working
    setup right up until the first delivery records ``failed:no_channel:…``, so
    the command says which step is still missing rather than printing nothing.

    Never fails the command: the write succeeded, and the vault row is correct
    and useful the moment the distribution is installed.
    """
    try:
        from robothor.engine.channels import list_channels

        if name in list_channels():
            return
    except Exception:  # noqa: BLE001 - a diagnosis that cannot run is not an error
        return
    print(
        f"\nNote: {name!r} is not available on this instance yet, so nothing can "
        f"deliver to it. It ships as a plugin: install it (`genus plugin install "
        f"genus-{name}`) and arm it (add {name!r} to ROBOTHOR_CHANNELS), then "
        f"`genus channel verify {name}`."
    )


def _write_settings(args: argparse.Namespace, name: str) -> list[str]:
    """Write the non-secret settings ``add`` accepts, if any were given.

    These go to ``config.yaml`` through the same writer ``genus config set``
    uses, not to the vault: a default target is a channel id, not a credential,
    and putting it behind a master key would make it unreadable by the doctor
    on an instance with no vault.
    """
    from robothor.settings.config_file import write_setting
    from robothor.settings.sources import config_yaml_path

    lines: list[str] = []
    for attribute, group, field, env in _ADDABLE_SETTINGS.get(name, ()):
        value = (getattr(args, attribute, None) or "").strip()
        if not value:
            continue
        path = config_yaml_path()
        if path is None:
            _err(f"no workspace: {env} was not written. Set ROBOTHOR_WORKSPACE and try again.")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        write_setting(group, field, value, names=(env,), path=path)
        lines.append(f"  config {env}={value}")
    return lines
