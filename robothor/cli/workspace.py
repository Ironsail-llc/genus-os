"""``genus workspace connect microsoft365`` -- connect an Exchange Online tenant.

What it does, in order:

1. Resolves the directory id, client id and mailboxes from the flags, falling
   back to what an earlier run stored, and validates every one of them before
   anything is written (a mailbox is spliced into PowerShell and a URL).
2. Reuses the app certificate already in the vault, or generates an RSA key
   and a self-signed certificate (``--rotate`` forces a new one).
3. Stores the directory id, client id, certificate and private key in the
   vault under ``workspace/microsoft365/<field>``. The private key is never
   printed, not even with ``--json``.
4. Writes the mailboxes to config.yaml through the same writer as
   ``genus config set`` (never an env file).
5. Prints what the Entra/Exchange admin does next: upload the certificate,
   the Graph permissions, the RBAC-for-Applications PowerShell that scopes the
   app to exactly the assistant and owner mailboxes, and the legacy
   ApplicationAccessPolicy alternative.
6. With ``--enable``, runs the doctor's connection and scope probes and sets
   ``workspace_provider=microsoft365`` only if both pass -- an app that can
   read the canary mailbox is never enabled.

``--dry-run`` reads the vault and settings but writes nothing.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from argparse import Namespace

    from robothor.workspace.microsoft.connect import AppCertificate

__all__ = ["cmd_workspace"]

PROVIDER = "microsoft365"

_ENV = {
    "assistant": "ROBOTHOR_M365_ASSISTANT_MAILBOX",
    "owner": "ROBOTHOR_M365_OWNER_MAILBOX",
    "canary": "ROBOTHOR_M365_SCOPE_CANARY_MAILBOX",
    "provider": "ROBOTHOR_WORKSPACE_PROVIDER",
}


class _RefusedError(Exception):
    """Bad input: exit 2, nothing written."""


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _tenant() -> str:
    from robothor.constants import DEFAULT_TENANT
    from robothor.settings import get_settings

    return get_settings().database.tenant_id or DEFAULT_TENANT


def _key(field: str) -> str:
    from robothor.vault.naming import workspace_key

    return workspace_key(PROVIDER, field)


def _read_vault(tenant: str) -> dict[str, str]:
    from robothor import vault

    stored: dict[str, str] = {}
    for field in ("tenant_id", "client_id", "client_certificate_pem", "client_private_key_pem"):
        value = vault.get(_key(field), tenant_id=tenant)
        if value:
            stored[field] = str(value).strip()
    return stored


def _resolve(args: Namespace, stored: dict[str, str]) -> dict[str, str]:
    """Flags first, then what an earlier run stored. Validated; raises _RefusedError."""
    from robothor.settings import get_settings
    from robothor.workspace.errors import AuthError
    from robothor.workspace.microsoft.auth import validate_tenant_id
    from robothor.workspace.microsoft.connect import _validate_client_id, validate_mailbox

    ws = get_settings().workspace
    raw = {
        "tenant_id": args.tenant_id or stored.get("tenant_id", ""),
        "client_id": args.client_id or stored.get("client_id", ""),
        "assistant": args.assistant_mailbox or ws.m365_assistant_mailbox,
        "owner": args.owner_mailbox or ws.m365_owner_mailbox,
        "canary": (
            args.canary_mailbox if args.canary_mailbox is not None else ws.m365_scope_canary_mailbox
        ),
    }
    flags = {
        "tenant_id": "--tenant-id",
        "client_id": "--client-id",
        "assistant": "--assistant-mailbox",
        "owner": "--owner-mailbox",
    }
    missing = [flag for name, flag in flags.items() if not str(raw[name] or "").strip()]
    if missing:
        raise _RefusedError(f"missing {', '.join(missing)} (nothing is stored yet to fall back on)")

    try:
        resolved = {"tenant_id": validate_tenant_id(raw["tenant_id"])}
    except AuthError as exc:
        raise _RefusedError(str(exc)) from None
    try:
        resolved["client_id"] = _validate_client_id(raw["client_id"])
        for name in ("assistant", "owner"):
            resolved[name] = validate_mailbox(raw[name])
        resolved["canary"] = validate_mailbox(raw["canary"]) if raw["canary"] else ""
    except ValueError as exc:
        raise _RefusedError(str(exc)) from None
    if resolved["assistant"] == resolved["owner"]:
        raise _RefusedError("the assistant and the owner must be two different mailboxes")
    if resolved["canary"] and resolved["canary"] in (resolved["assistant"], resolved["owner"]):
        raise _RefusedError(
            "the canary mailbox must be a third mailbox the app is NOT scoped to, "
            "not the assistant or the owner"
        )
    return resolved


def _certificate(
    stored: dict[str, str], *, rotate: bool, dry_run: bool
) -> tuple[AppCertificate | None, bool]:
    """``(certificate, reused)``. ``None`` only on a dry run with nothing stored."""
    from robothor.workspace.errors import AuthError
    from robothor.workspace.microsoft.connect import generate_certificate, load_certificate

    cert_pem = stored.get("client_certificate_pem", "")
    key_pem = stored.get("client_private_key_pem", "")
    if cert_pem and key_pem and not rotate:
        try:
            return load_certificate(cert_pem, key_pem), True
        except AuthError as exc:
            raise _RefusedError(f"{exc}; re-run with --rotate to replace it") from None
    if dry_run:
        return None, False
    return generate_certificate(), False


def _write_settings(values: dict[str, str]) -> list[str]:
    """Through ``robothor.settings.operator`` -- the `genus config set` writer."""
    from robothor.settings import operator, reset_settings

    planned = []
    for env, value in values.items():
        row = operator.record(env)
        if row is None:  # pragma: no cover - the registry declares all four
            raise RuntimeError(f"{env} is not a declared setting")
        planned.append((row, operator.validate(row, value)))
    applied, _units, errors = operator.apply_batch(
        planned, actor="operator:genus-workspace-connect", reason="genus workspace connect"
    )
    reset_settings()
    if errors:
        raise RuntimeError("; ".join(error["message"] for error in errors))
    return applied


def _plan(resolved: dict[str, str]) -> dict[str, Any]:
    from robothor.workspace.microsoft.connect import (
        OPTIONAL_PERMISSIONS,
        REQUIRED_PERMISSIONS,
        legacy_policy_commands,
        rbac_commands,
    )

    args = (resolved["client_id"], resolved["assistant"], resolved["owner"], resolved["canary"])
    return {
        "permissions": {
            "api": "Microsoft Graph",
            "type": "Application",
            "required": list(REQUIRED_PERMISSIONS),
            "optional": list(OPTIONAL_PERMISSIONS),
        },
        "powershell": {
            "rbac_for_applications": rbac_commands(*args),
            "application_access_policy": legacy_policy_commands(*args),
        },
    }


def _cert_json(cert: AppCertificate | None, reused: bool) -> dict[str, Any] | None:
    if cert is None:
        return None
    return {
        "reused": reused,
        "subject": cert.subject,
        "thumbprint_sha1": cert.thumbprint_sha1,
        "thumbprint_sha256": cert.thumbprint_sha256,
        "not_after": cert.not_after.isoformat() if cert.not_after else None,
        "certificate_pem": cert.certificate_pem,
        "certificate_base64": cert.certificate_base64,
    }


def _print_human(report: dict[str, Any]) -> None:
    r = report
    mode = "DRY RUN - nothing written" if r["dry_run"] else "connected"
    print(f"Microsoft 365 workspace ({mode})")
    print(f"  directory (tenant) id : {r['tenant_id']}")
    print(f"  application (client) id: {r['client_id']}")
    print(f"  assistant mailbox     : {r['mailboxes']['assistant']}")
    print(f"  owner mailbox         : {r['mailboxes']['owner']}")
    print(f"  canary mailbox        : {r['mailboxes']['canary'] or '(none - scope unproven)'}")
    print(f"  platform tenant       : {r['platform_tenant']}")
    for note in r["notes"]:
        print(f"  note: {note}")
    print()

    cert = r["certificate"]
    print("1. Upload this certificate to the app registration")
    print("   (Entra admin center > App registrations > your app > Certificates & secrets >")
    print("   Certificates > Upload certificate; save it as a .cer/.pem file first).")
    if cert is None:
        print("   A new certificate is generated and stored on the real run (not a dry run).")
    else:
        state = "reusing the stored certificate" if cert["reused"] else "new certificate"
        print(f"   {state}; expires {cert['not_after']}")
        print(f"   SHA-1 thumbprint (Entra lists this): {cert['thumbprint_sha1']}")
        print(f"   SHA-256 thumbprint                 : {cert['thumbprint_sha256']}")
        print()
        print(cert["certificate_pem"].rstrip())
    print()

    perms = r["permissions"]
    print("2. Graph application permissions the app needs")
    for permission in perms["required"]:
        print(f"   - {permission} (Application)")
    for permission in perms["optional"]:
        print(f"   - {permission} (Application, optional: timezone check)")
    print("   With RBAC for Applications (step 3, recommended) assign them ONLY through the")
    print("   Exchange role assignments below. Do NOT also admin-consent them in Entra:")
    print("   an Entra consent is tenant-wide and adds to the scoped grant, and the doctor's")
    print("   canary check then fails. With the legacy policy (step 3b) you DO consent them")
    print("   in Entra (API permissions > Add > Microsoft Graph > Application permissions).")
    print()

    print("3. Scope the app to the two mailboxes (Exchange Online PowerShell)")
    print(_indent(r["powershell"]["rbac_for_applications"]))
    print()
    print("3b. Legacy alternative: an ApplicationAccessPolicy (use one, not both)")
    print(_indent(r["powershell"]["application_access_policy"]))
    print()

    print("4. Prove it, then enable")
    print("   genus doctor --category workspace")
    print("   genus workspace connect microsoft365 --enable")
    if r.get("probe"):
        print()
        print("Probe:")
        for line in r["probe"]:
            print(f"   {line}")
    if r["enabled"]:
        print()
        print("Enabled: workspace_provider=microsoft365. Restart the engine to apply it.")


def _indent(text: str) -> str:
    return "\n".join(f"   {line}" if line else "" for line in text.splitlines())


def _connect_microsoft365(args: Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    dry_run = bool(getattr(args, "dry_run", False))
    enable = bool(getattr(args, "enable", False))
    rotate = bool(getattr(args, "rotate", False))
    if enable and dry_run:
        _err("--enable and --dry-run cannot be combined: enabling is a write")
        return 2
    if rotate and dry_run:
        _err("--rotate and --dry-run cannot be combined: rotating is a write")
        return 2

    tenant = _tenant()
    notes: list[str] = []
    try:
        stored = _read_vault(tenant)
    except Exception as exc:  # noqa: BLE001 - a CLI reports, it does not trace
        if not dry_run:
            _err(f"the vault could not be read ({type(exc).__name__}); nothing was written")
            return 1
        stored = {}
        notes.append(f"the vault could not be read ({type(exc).__name__}); showing a fresh plan")

    try:
        resolved = _resolve(args, stored)
        cert, reused = _certificate(stored, rotate=rotate, dry_run=dry_run)
    except _RefusedError as exc:
        _err(f"refused: {exc}")
        return 2

    settings_written: list[str] = []
    if not dry_run:
        from robothor import vault

        assert cert is not None
        try:
            vault.set(_key("tenant_id"), resolved["tenant_id"], tenant_id=tenant)
            vault.set(_key("client_id"), resolved["client_id"], tenant_id=tenant)
            if not reused:
                vault.set(_key("client_certificate_pem"), cert.certificate_pem, tenant_id=tenant)
                vault.set(_key("client_private_key_pem"), cert.private_key_pem, tenant_id=tenant)
        except Exception as exc:  # noqa: BLE001
            _err(f"could not store the credential in the vault: {type(exc).__name__}")
            return 1
        values = {_ENV["assistant"]: resolved["assistant"], _ENV["owner"]: resolved["owner"]}
        if resolved["canary"]:
            values[_ENV["canary"]] = resolved["canary"]
        try:
            settings_written = _write_settings(values)
        except Exception as exc:  # noqa: BLE001
            _err(f"the credential is stored but the settings were not written: {exc}")
            return 1

    report: dict[str, Any] = {
        "provider": PROVIDER,
        "dry_run": dry_run,
        "platform_tenant": tenant,
        "tenant_id": resolved["tenant_id"],
        "client_id": resolved["client_id"],
        "mailboxes": {k: resolved[k] for k in ("assistant", "owner", "canary")},
        "certificate": _cert_json(cert, reused),
        "settings_written": settings_written,
        "enabled": False,
        "notes": notes,
        **_plan(resolved),
    }

    code = 0
    if enable:
        passed, lines = _probe()
        report["probe"] = lines
        if passed:
            report["settings_written"] += _write_settings({_ENV["provider"]: PROVIDER})
            report["enabled"] = True
        else:
            code = 1
            report["notes"].append(
                "not enabled: the doctor probe did not pass (connection and a DENIED canary "
                "are both required); workspace_provider is unchanged"
            )

    if as_json:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)
    if code:
        _err("not enabled: the probe did not pass; workspace_provider is unchanged")
    return code


def _probe() -> tuple[bool, list[str]]:
    from robothor.doctor.checks.workspace_m365 import probe_for_enable
    from robothor.doctor.context import DoctorContext
    from robothor.settings import reset_settings

    reset_settings()
    try:
        return asyncio.run(probe_for_enable(DoctorContext(timeout_s=30.0)))
    except Exception as exc:  # noqa: BLE001
        return False, [f"fail  probe raised {type(exc).__name__}"]


def cmd_workspace(args: Namespace) -> int:
    command = getattr(args, "workspace_command", None)
    provider = getattr(args, "workspace_provider", None)
    if command == "connect" and provider == PROVIDER:
        return _connect_microsoft365(args)
    _err("usage: genus workspace connect microsoft365 [--tenant-id ... --client-id ...]")
    return 2
