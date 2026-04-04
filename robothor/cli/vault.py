"""Vault (secret management) commands."""

from __future__ import annotations

import argparse  # noqa: TC003
import os
from pathlib import Path


def cmd_vault(args: argparse.Namespace) -> int:
    sub = getattr(args, "vault_command", None)

    workspace = Path(os.environ.get("ROBOTHOR_WORKSPACE", Path.home() / "robothor"))

    if sub == "init":
        from robothor.vault.crypto import init_master_key

        key_path = init_master_key(workspace)
        print(f"Vault master key: {key_path}")
        return 0

    if sub == "set":
        import getpass

        from robothor.vault import set as vault_set
        from robothor.vault.crypto import get_master_key

        try:
            get_master_key(workspace)
        except FileNotFoundError:
            print("Error: No vault master key. Run 'robothor vault init' first.")
            return 1
        value = args.value
        if value is None:
            value = getpass.getpass(f"Value for {args.key}: ")
        vault_set(args.key, value, category=args.category)
        print(f"Stored: {args.key} [{args.category}]")
        return 0

    if sub == "get":
        from robothor.vault import get as vault_get
        from robothor.vault.crypto import get_master_key

        try:
            get_master_key(workspace)
        except FileNotFoundError:
            print("Error: No vault master key. Run 'robothor vault init' first.")
            return 1
        value = vault_get(args.key)
        if value is None:
            print(f"Not found: {args.key}")
            return 1
        print(value)
        return 0

    if sub == "list":
        from robothor.vault.crypto import get_master_key
        from robothor.vault.dal import list_keys as vault_list_keys

        try:
            get_master_key(workspace)
        except FileNotFoundError:
            print("Error: No vault master key. Run 'robothor vault init' first.")
            return 1
        keys = vault_list_keys(category=args.category)
        if not keys:
            print("Vault is empty.")
        else:
            for k in keys:
                print(f"  {k}")
            print(f"\n{len(keys)} secret(s)")
        return 0

    if sub == "delete":
        from robothor.vault import delete as vault_delete

        deleted = vault_delete(args.key)
        print(f"{'Deleted' if deleted else 'Not found'}: {args.key}")
        return 0 if deleted else 1

    if sub == "import-env":
        from robothor.vault import set as vault_set
        from robothor.vault.crypto import get_master_key

        try:
            get_master_key(workspace)
        except FileNotFoundError:
            print("Error: No vault master key. Run 'robothor vault init' first.")
            return 1
        env_path = Path(args.file)
        if not env_path.exists():
            print(f"Error: File not found: {env_path}")
            return 1
        count = 0
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = (
                key.strip().lower().replace("_", "/", 1)
            )  # TELEGRAM_BOT_TOKEN -> telegram/bot_token
            value = value.strip().strip("'\"")
            if value:
                vault_set(key, value, category="credential")
                count += 1
        print(f"Imported {count} secret(s)")
        return 0

    if sub == "export-env":
        from robothor.vault import export_env
        from robothor.vault.crypto import get_master_key

        try:
            get_master_key(workspace)
        except FileNotFoundError:
            print("Error: No vault master key. Run 'robothor vault init' first.")
            return 1
        secrets = export_env()
        for k, v in sorted(secrets.items()):
            print(f"{k}={v}")
        return 0

    if sub == "audit":
        return cmd_vault_audit()

    print("Usage: robothor vault {init|set|get|list|delete|import-env|export-env|audit}")
    return 0


def cmd_vault_audit() -> int:
    """Audit secret usage: find used, unused, and missing keys."""
    import re
    from pathlib import Path

    secrets_file = Path("/run/robothor/secrets.env")
    repo_root = Path(__file__).resolve().parent.parent.parent

    # 1. Load keys from secrets.env
    available_keys: set[str] = set()
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key = line.split("=", 1)[0]
                available_keys.add(key)
        print(f"Secrets file: {len(available_keys)} keys loaded from {secrets_file}")
    else:
        print(f"WARNING: {secrets_file} not found. Run decrypt-secrets.sh first.")
        print("Checking codebase references only.\n")

    # 2. Grep codebase for secret references
    patterns = [
        r'os\.getenv\(["\'](\w+)["\']\)',
        r'os\.environ\.get\(["\'](\w+)["\']\)',
        r'os\.environ\[["\'](\w+)["\']\]',
        r"\$\{(\w+)\}",
        r"\$(\w+)",
    ]

    referenced_keys: set[str] = set()
    shell_builtins = {
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "TERM",
        "LANG",
        "PWD",
        "OLDPWD",
        "HOSTNAME",
        "EDITOR",
        "PAGER",
        "DISPLAY",
        "LOGNAME",
        "MAIL",
        "TMPDIR",
    }
    # Only scan Python and shell files in the project
    scan_dirs = [
        repo_root / "robothor",
        repo_root / "scripts",
        repo_root / "crm",
        repo_root / "brain",
    ]

    for scan_dir in scan_dirs:
        if not scan_dir.exists():
            continue
        for ext in ("*.py", "*.sh"):
            for filepath in scan_dir.rglob(ext):
                try:
                    content = filepath.read_text(errors="ignore")
                    for pattern in patterns:
                        for match in re.finditer(pattern, content):
                            key = match.group(1)
                            # Filter to likely secret keys (all uppercase, 3+ chars)
                            if (
                                key.isupper()
                                and len(key) >= 3
                                and "_" in key
                                and key not in shell_builtins
                            ):
                                referenced_keys.add(key)
                except Exception:
                    continue

    # 3. Report
    used = available_keys & referenced_keys
    unused = available_keys - referenced_keys
    missing = referenced_keys - available_keys

    # Filter missing to only plausible secret names
    secret_prefixes = {
        "OPENROUTER",
        "ROBOTHOR",
        "ANTHROPIC",
        "PERPLEXITY",
        "SOPS",
        "AGE",
        "CLOUDFLARE",
    }
    missing = {k for k in missing if any(k.startswith(p) for p in secret_prefixes)}

    print(f"\n{'=' * 50}")
    print("SECRET AUDIT REPORT")
    print(f"{'=' * 50}")

    print(f"\n  Used keys ({len(used)}):")
    for k in sorted(used):
        print(f"    + {k}")

    if unused:
        print(f"\n  Unused keys ({len(unused)}) — removal candidates:")
        for k in sorted(unused):
            print(f"    ? {k}")

    if missing:
        print(f"\n  Referenced but missing ({len(missing)}):")
        for k in sorted(missing):
            print(f"    ! {k}")

    print()
    if missing:
        print(f"RESULT: {len(missing)} referenced key(s) not in secrets file")
        return 1
    print("RESULT: All referenced keys are available")
    return 0
