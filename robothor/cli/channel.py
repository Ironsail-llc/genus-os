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
of it — and a token passed as a flag is scrubbed out of ``sys.argv`` before
anything else runs, because ``ps`` shows a command line to every account on the
box.
"""

from __future__ import annotations

import argparse  # noqa: TC003 - argparse.Namespace is used at runtime in signatures
import asyncio
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

from robothor.settings.env import process_env_get

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
}

#: Per-channel non-secret settings ``add`` can write. ``(flag attribute, group,
#: field, environment name)``.
_ADDABLE_SETTINGS: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    "slack": (
        ("default_target", "channels", "slack_default_target", "ROBOTHOR_SLACK_DEFAULT_TARGET"),
    ),
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


def _scrub_argv(*values: str) -> None:
    """Remove a credential from this process's own command line.

    ``ps``, ``/proc/<pid>/cmdline`` and any handler that prints ``sys.argv``
    all read it. This cannot un-publish what the shell already recorded in a
    history file — the prompt is the safe path and the help text says so — but
    it closes the window for the rest of this process's life.
    """
    for value in values:
        if not value:
            continue
        sys.argv = [part.replace(value, "***") for part in sys.argv]


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
    """
    from robothor.secrets.env_file import env_line, instance_env_path, write_private

    path = instance_env_path(workspace)
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

    try:
        health = asyncio.run(channel.health())
    except Exception as exc:  # noqa: BLE001
        health = {"error": f"{type(exc).__name__}: {exc}"}
    if health.get("configured") is False:
        _err(f"Channel {name!r} is not configured on this instance; nothing to verify.")
        return 2

    try:
        steps = asyncio.run(verify(getattr(args, "target", None)))
    except Exception as exc:  # noqa: BLE001 — a broken channel is a report, not a traceback
        _err(f"Channel {name!r} raised while verifying: {type(exc).__name__}: {exc}")
        return 1

    rows = [(str(step), bool(ok), str(detail)) for step, ok, detail in steps]
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
        value = (getattr(args, attribute, None) or "").strip()
        source = "flag"
        if not value:
            # Through the settings package's own accessor: the name is built
            # from `_ADDABLE` at runtime, which is exactly the dynamically-named
            # read `process_env_get` exists for.
            value = (process_env_get(env, "") or "").strip()
            source = "environment"
        if not value and interactive:
            value = getpass.getpass(f"{name} {attribute.replace('_', ' ')} (not echoed): ").strip()
            source = "prompt"
        if value:
            resolved[attribute] = (value, field, env)
            logger.debug("genus channel add: %s resolved from the %s", env, source)
    return resolved


def _cmd_add(args: argparse.Namespace) -> int:
    """Store a channel's credentials where the platform reads them.

    The vault when this instance has a master key, the instance env file
    otherwise — and the destination is printed either way. A vault-only ``add``
    fails on the most common install (no master key, no database), and an
    env-only one contradicts the naming the rest of the platform resolves
    through.
    """
    name = (getattr(args, "name", "") or "").strip().lower()
    if name not in _ADDABLE:
        _err(
            f"`genus channel add` does not know how to configure {name!r}. "
            f"Known: {', '.join(sorted(_ADDABLE))}."
        )
        return 2

    resolved = _collect(args, name)
    # Before anything is written, logged, or raised: the flag value must not
    # survive in this process's command line.
    _scrub_argv(*(value for value, _field, _env in resolved.values()))

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
                _err(f"The vault refused {key}: {type(exc).__name__}: {exc}")
                return 1
            written.append(f"  vault  {key}  (sha256:{_fingerprint(value)})")
    else:
        path = _write_env_file(workspace, {env: value for value, _field, env in resolved.values()})
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
