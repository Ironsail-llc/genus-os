#!/usr/bin/env python3
"""Build and sign a plugin registry index from a directory of wheels.

This is what the ``genus-plugins`` repository's CI runs. It takes a directory of
built wheels and produces the two files a registry serves:

* ``index.json`` — canonical JSON (sorted keys, no whitespace), exactly the
  bytes that get signed. ``robothor.plugins.registry`` refuses a served file
  that is not already in this form, so the writer and the verifier share one
  definition of "canonical" rather than two that drift.
* ``index.json.sig`` — ``{"key_id": ..., "signature": ...}``, a detached Ed25519
  signature over those bytes.

**Everything in an entry is read out of the wheel**, and that is the point. The
name and version come from ``METADATA``, the contributed groups from
``entry_points.txt``, ``manifest_sha256`` from the ``genus-plugin.yaml`` the
loader will later read from the installed distribution, and the verdict from
:mod:`robothor.plugins.scan` -- the same offline scanner the installer re-runs
on the wheel it downloads. A publisher who could type those fields by hand
would be marking their own homework inside a signed document, and the installer
would have no way to notice.

The signing key is a path the caller names. Nothing here stores one: a signing
key committed to a repository is a signing key in every clone of it.

Usage::

    python scripts/build_plugin_index.py dist/ \\
        --out index.json --key ~/.keys/genus-plugins.pem --key-id genus-2026 \\
        --publisher genus --base-url https://example.org/genus-plugins/wheels/

Exit codes: 0 wrote the index, 2 refused and said why.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from robothor.plugins import registry, scan, wheel  # noqa: E402
from robothor.plugins.manifest import MANIFEST_NAME, parse_manifest  # noqa: E402


def _fail(message: str) -> int:
    print(f"build_plugin_index: {message}", file=sys.stderr)
    return 2


def _entry(path: Path, *, base_url: str, yanked: dict[str, str]) -> tuple[dict[str, Any], str]:
    """One index entry, read entirely out of the wheel. Returns (entry, verdict)."""
    data = path.read_bytes()
    with tempfile.TemporaryDirectory(prefix="genus-index-") as unpacked:
        contents = wheel.open_wheel(path, Path(unpacked) / "w")
        if not contents.manifest_text:
            raise wheel.WheelError(
                f"{path.name} ships no {MANIFEST_NAME}, so nothing declares what it "
                "contributes. It would be refused by every installer."
            )
        verdict = scan.scan_wheel(contents)
        manifest = parse_manifest(contents.manifest_text)
        contract = "" if manifest is None else str(manifest.contract_version or "")

        key = f"{contents.name}=={contents.version}"
        entry: dict[str, Any] = {
            "name": contents.name,
            "version": contents.version,
            "summary": contents.summary,
            "contract_version": contract,
            "groups": list(contents.genus_groups()),
            "python": contents.requires_python,
            "artifacts": [
                {
                    "kind": "wheel",
                    "filename": path.name,
                    "url": base_url + path.name,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                }
            ],
            "manifest_sha256": contents.manifest_sha256,
            "scan": {
                "verdict": verdict.verdict,
                "reasons": list(verdict.reasons),
                # The publisher's own scan time. Advisory: the installer
                # re-scans what it downloads rather than trusting this.
                "scanned_at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
        }
        if contents.homepage:
            entry["homepage"] = contents.homepage
        if contents.license:
            entry["license"] = contents.license
        if key in yanked:
            # Marked, never deleted: an installer that looks up a yanked
            # version must be told WHY, and an entry that simply vanished
            # reads as "never published".
            entry["yanked"] = True
            entry["yank_reason"] = yanked[key]
        return entry, verdict.verdict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_plugin_index.py",
        description="Build and sign a Genus plugin registry index from a directory of wheels.",
    )
    parser.add_argument("directory", help="Directory holding the built wheels")
    parser.add_argument("--out", required=True, help="Path to write index.json")
    parser.add_argument("--key", required=True, help="PEM Ed25519 PRIVATE key to sign with")
    parser.add_argument("--key-id", required=True, help="The key_id consumers pin")
    parser.add_argument("--publisher", default="genus", help="Publisher id recorded in the index")
    parser.add_argument(
        "--base-url",
        required=True,
        help="https prefix the wheels are served from; each artifact URL is this "
        "plus the wheel's filename",
    )
    parser.add_argument(
        "--yanked",
        help="JSON file mapping 'name==version' to a yank reason. Yanked entries "
        "stay in the index so an installer can say why.",
    )
    parser.add_argument(
        "--allow-blocked",
        action="store_true",
        help="Publish an entry the scanner blocked. Off by default: signing one "
        "means signing an entry every installer will refuse.",
    )
    args = parser.parse_args(argv)

    base_url = args.base_url
    if not base_url.startswith("https://"):
        return _fail(f"--base-url must be https, got {base_url!r}.")
    if not base_url.endswith("/"):
        base_url += "/"

    directory = Path(args.directory)
    if not directory.is_dir():
        return _fail(f"{args.directory!r} is not a directory.")
    wheels = sorted(directory.glob("*.whl"))
    if not wheels:
        # An index with no plugins is a valid signed document that silently
        # removes every plugin the previous one published.
        return _fail(f"there are no wheels in {args.directory!r}; refusing to sign an empty index.")

    yanked: dict[str, str] = {}
    if args.yanked:
        try:
            loaded = json.loads(Path(args.yanked).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return _fail(f"--yanked could not be read ({type(exc).__name__}).")
        if not isinstance(loaded, dict):
            return _fail("--yanked must hold a JSON object of 'name==version' -> reason.")
        yanked = {str(k): str(v) for k, v in loaded.items()}

    try:
        private_pem = Path(args.key).read_text(encoding="utf-8")
    except OSError as exc:
        return _fail(f"the signing key could not be read ({type(exc).__name__}).")

    entries: list[dict[str, Any]] = []
    for path in wheels:
        try:
            entry, verdict = _entry(path, base_url=base_url, yanked=yanked)
        except wheel.WheelError as exc:
            return _fail(str(exc))
        if verdict == scan.BLOCKED and not args.allow_blocked:
            reasons = "\n  - ".join(entry["scan"]["reasons"])
            return _fail(
                f"{path.name} is blocked by the static scan and will be refused by "
                f"every installer:\n  - {reasons}\n"
                "Fix it, or pass --allow-blocked to publish the entry anyway."
            )
        entries.append(entry)

    seen: set[str] = set()
    for entry in entries:
        key = f"{entry['name']}=={entry['version']}"
        if key in seen:
            return _fail(f"{key} appears twice in {args.directory!r}.")
        seen.add(key)

    payload: dict[str, Any] = {
        "schema": registry.SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "publisher": {"id": args.publisher, "key_id": args.key_id},
        "plugins": sorted(entries, key=lambda e: (e["name"], e["version"])),
    }

    try:
        raw = registry.canonical_bytes(payload)
        signature = registry.signature_document(payload, private_pem, key_id=args.key_id)
    except registry.RegistryError as exc:
        return _fail(str(exc))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw + b"\n")
    Path(str(out) + ".sig").write_text(signature + "\n", encoding="utf-8")

    # Verified with the SHIPPED parser before anything is announced. A builder
    # that cannot produce a document its own platform accepts is a registry
    # nobody can publish to, and finding that out in CI beats finding it out
    # from an operator.
    from cryptography.hazmat.primitives import serialization

    public_pem = (
        serialization.load_pem_private_key(private_pem.encode(), password=None)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    try:
        registry.parse_index(
            out.read_bytes(),
            Path(str(out) + ".sig").read_bytes(),
            keys={args.key_id: public_pem},
        )
    except registry.RegistryError as exc:
        return _fail(f"the index this build produced does not verify: {exc}")

    print(f"wrote {out} ({len(entries)} plugin(s)) and {out.name}.sig signed by {args.key_id}")
    for entry in payload["plugins"]:
        marks = [entry["scan"]["verdict"]]
        if entry.get("yanked"):
            marks.append("yanked")
        print(f"  {entry['name']} {entry['version']}  {' '.join(marks)}")
    return 0


if __name__ == "__main__":  # pragma: no cover - console entry
    raise SystemExit(main())
