"""What a wheel would do, read before it is allowed to do it.

The loader's guarantee is "an undeclared distribution is never imported". This
module is the guarantee one step earlier: an undeclared, reserved-name-claiming
or outright hostile distribution is never INSTALLED, and the operator is told
which line of which file made that call.

Three design decisions, each the answer to a way scanners fail:

* **AST, never regex over text.** A docstring reading "never call ``eval()``"
  must not block an install, and ``getattr(builtins, "ex" + "ec")`` must. A
  text scan gets both backwards, and the first false positive is the last time
  anybody reads a verdict.
* **A third verdict.** Blocking everything that imports ``httpx`` would block
  every integration worth having; letting a channel plugin through silently is
  how a package that merely got installed becomes the delivery surface for
  every briefing. ``review`` is the state where the operator says yes out loud,
  with ``--accept-review``, having read the reasons.
* **Pure, offline, deterministic.** No provider, no network, no clock. That is
  what lets the publisher's own verdict in the index be *checked* against a
  fresh scan of the downloaded wheel rather than believed.

**What this is not.** It is not a sandbox and it is not a proof. A wheel that
passes still runs with the daemon's privileges once it is imported; the scan
raises the cost of hiding something and names what it found. The honest line is
in every verdict: prompt text is `static-only` unless the operator asks for the
LLM-backed injection screen, because saying a prompt was checked when no
provider was configured is precisely the inert control this platform keeps
shipping.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from robothor.plugins.loader import _GROUPS, CONTRACT_VERSION

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from robothor.plugins.wheel import WheelContents

logger = logging.getLogger(__name__)

__all__ = ["BLOCKED", "REVIEW", "SAFE", "ScanResult", "scan_wheel"]

SAFE = "safe"
REVIEW = "review"
BLOCKED = "blocked"

#: Most files the scan will parse. A wheel with more is not refused -- it is
#: reported as partly unscanned, which is ``review``: "we did not look" must
#: never render as "we looked and it was fine".
MAX_SCAN_FILES = 2000

#: Largest single source file the scan will parse. ``ast.parse`` is roughly
#: linear, but a hostile 200 MB one-liner is still a denial of service against
#: the operator's terminal.
MAX_SOURCE_BYTES = 2 * 1024 * 1024

#: Groups whose contributions run with no tool call and no operator gesture --
#: a hook fires on every turn, a channel receives messages, a job runs on a
#: schedule. Installing one of these is a bigger act than installing a tool.
_AMBIENT_GROUPS = frozenset(
    {
        "genus.hooks",
        "genus.guardrails",
        "genus.sandboxes",
        "genus.channels",
        "genus.memory",
        "genus.jobs",
    }
)

#: Modules whose mere import means the plugin reaches off the box.
_NETWORK_MODULES = frozenset({"requests", "httpx", "urllib", "urllib3", "aiohttp"})

#: Modules that are a way out of Python's own boundaries.
_FOREIGN_MODULES = frozenset({"ctypes", "cffi"})

#: Filename shapes that carry instructions to a model rather than to a CPU.
_PROMPT_SUFFIXES = (".md", ".markdown", ".prompt")
_PROMPT_STEMS = ("skill", "instructions", "system_prompt", "prompt")

#: Call names that write somewhere.
_WRITE_CALLS = frozenset(
    {
        "open",
        "write_text",
        "write_bytes",
        "mkdir",
        "touch",
        "unlink",
        "remove",
        "rename",
        "replace",
        "chmod",
        "symlink_to",
        "copy",
        "copy2",
        "copyfile",
        "copytree",
        "move",
        "rmtree",
    }
)

#: Calls that turn bytes back into something a compiler will accept.
_DECODERS = frozenset(
    {"b64decode", "b32decode", "b16decode", "a85decode", "unhexlify", "decompress"}
)


@dataclass(frozen=True)
class ScanResult:
    """One wheel's verdict, and why.

    ``reasons`` are sentences, each naming a wheel-relative ``file:line`` where
    there is one. Nothing here carries a path from outside the wheel: this
    object crosses an HTTP response, and where the instance keeps its files is
    not a platform fact.
    """

    verdict: str = SAFE
    reasons: tuple[str, ...] = ()
    #: ``static-only`` unless the operator asked for the LLM-backed injection
    #: screen. Never implies a prompt was checked when it was not.
    prompt_scan: str = "static-only"
    files_scanned: int = 0
    prompt_files: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.verdict == BLOCKED

    def as_json(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "prompt_scan": self.prompt_scan,
            "files_scanned": self.files_scanned,
            "prompt_files": list(self.prompt_files),
        }


def _builtin_names(group: str) -> set[str]:
    """The names the host already owns in *group*.

    Its own function so a test can substitute a registry without importing the
    engine, and so the rule reads from :func:`robothor.plugins.loader.builtin_names`
    -- the live registry -- rather than from a hand-maintained list. A
    hand-maintained list of built-in names is exactly the defect #329/#330/#331
    turned out to be.
    """
    from robothor.plugins.loader import builtin_names

    return builtin_names(group)


def _contract_matches(declared: Any) -> bool:
    """Whether a manifest's ``contract_version`` is the one this engine speaks.

    ``CONTRACT_VERSION`` is the string ``"1.0"`` and every manifest published so
    far writes the integer ``1``; the loader never compared them because it
    checks the PAYLOAD's version instead. Comparing them as numbers is what
    keeps this scanner from blocking every plugin that exists on a spelling.
    """
    if declared is None:
        return False
    try:
        return float(str(declared)) == float(CONTRACT_VERSION)
    except (TypeError, ValueError):
        return str(declared) == CONTRACT_VERSION


def _declared_contract(manifest_text: str, parsed: Any) -> Any:
    """The contract version as the manifest SPELLS it.

    :func:`robothor.plugins.manifest.parse_manifest` keeps only an ``int``,
    because that is all the loader ever needed. Here the difference matters:
    a manifest saying ``contract_version: 1.0`` would arrive as None and be
    blocked for "declaring nothing", which is both wrong and unactionable. The
    raw value is re-read rather than changing what the loader parses.
    """
    if parsed is not None:
        return parsed
    try:
        import yaml

        data = yaml.safe_load(manifest_text) or {}
    except Exception:  # noqa: BLE001 - an unparseable manifest is handled by the caller
        return None
    return data.get("contract_version") if isinstance(data, dict) else None


def _is_prompt_file(relative: str) -> bool:
    from pathlib import PurePosixPath

    path = PurePosixPath(relative)
    if path.suffix.lower() in _PROMPT_SUFFIXES:
        return True
    return path.stem.lower().startswith(_PROMPT_STEMS)


def _root_name(node: ast.AST) -> str:
    """The leftmost name of an attribute chain: ``a.b.c`` -> ``a``."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _literal_str(node: ast.AST) -> str | None:
    """The value of a plain string literal, or None when it is anything else.

    Deliberately NOT ``ast.literal_eval``: ``"ex" + "ec"`` is a literal to
    ``literal_eval`` and a dynamic construction to a reviewer, and the whole
    point of the exec rule is to tell those apart.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _writes_somewhere_protected(value: str) -> str:
    """A sentence naming why this string is a protected location, or ""."""
    from robothor.engine import secret_paths

    candidate = value.strip()
    if not candidate:
        return ""
    normalized = candidate.replace("\\", "/")
    if normalized.startswith("/etc/") or normalized == "/etc":
        return "the system configuration directory"
    if normalized.startswith(("~/.ssh", "/root/.ssh")) or "/.ssh/" in normalized:
        return "an SSH directory"
    try:
        if secret_paths.is_secret_path(candidate):
            return "a credentials file"
    except Exception:  # noqa: BLE001 - the denylist must never crash a scan
        return ""
    return ""


class _SourceVisitor(ast.NodeVisitor):
    """One module's findings. Blocking rules first, then the review rules."""

    def __init__(self, relative: str) -> None:
        self.relative = relative
        self.blocked: list[str] = []
        self.review: list[str] = []

    # -- helpers ----------------------------------------------------------
    def _at(self, node: ast.AST) -> str:
        return f"{self.relative}:{getattr(node, 'lineno', 0)}"

    def _block(self, node: ast.AST, sentence: str) -> None:
        self.blocked.append(f"{self._at(node)}: {sentence}")

    def _flag(self, node: ast.AST, sentence: str) -> None:
        self.review.append(f"{self._at(node)}: {sentence}")

    # -- imports ----------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._module(node, alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self._module(node, node.module)
        self.generic_visit(node)

    def _module(self, node: ast.AST, dotted: str) -> None:
        top = dotted.split(".", 1)[0]
        if top in _FOREIGN_MODULES:
            self._block(
                node,
                f"imports {top}, which calls native code outside every guardrail "
                "the engine applies to Python.",
            )
        elif top in _NETWORK_MODULES:
            self._flag(node, f"imports {top}, so this plugin reaches off the box.")
        elif top == "socket":
            self._flag(node, "imports socket.")

    # -- calls ------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        dotted = _dotted(node.func)
        first = node.args[0] if node.args else None

        if dotted in ("os.system", "posix.system"):
            self._block(node, "calls os.system(), which runs a shell command string.")
        if dotted.endswith("popen") or dotted in ("os.popen", "os.execv", "os.execve"):
            self._block(node, f"calls {dotted}(), which executes a program directly.")

        for keyword in node.keywords:
            if (
                keyword.arg == "shell"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
            ):
                self._block(
                    node,
                    f"calls {dotted or name}() with shell=True, which hands a string to a shell.",
                )

        if name in ("eval", "exec", "compile") and isinstance(node.func, ast.Name):
            if first is not None and _literal_str(first) is None:
                decoder = self._decoder_in(first)
                if decoder:
                    self._block(
                        node,
                        f"calls {name}() on the output of {decoder}(), which is code "
                        "hidden from review inside encoded data.",
                    )
                else:
                    self._block(
                        node,
                        f"calls {name}() on input that is not a literal, so what runs "
                        "cannot be read here.",
                    )
            elif first is None:
                self._block(node, f"calls {name}() with no readable argument.")

        if name == "getattr" and first is not None:
            root = _dotted(first)
            if root.split(".", 1)[0] in ("builtins", "__builtins__"):
                self._block(
                    node,
                    "looks an attribute up on builtins by name, which is how exec() "
                    "and eval() are reached without naming them.",
                )
        if name == "__import__" and first is not None and _literal_str(first) is None:
            self._block(node, "calls __import__() on a computed name.")

        if dotted in ("socket.socket", "socket.create_connection") or (
            name == "connect" and _root_name(node.func) == "socket"
        ):
            self._block(
                node,
                "opens a raw socket. A plugin that needs the network should use a "
                "declared HTTP client so the engine's egress rules apply.",
            )

        if name in _WRITE_CALLS:
            # The literal is as often in the RECEIVER as in the arguments:
            # `Path("~/.ssh/authorized_keys").write_text(key)` puts it in
            # neither `args` nor `keywords`, and checking only those let the
            # most obvious spelling of the attack through.
            candidates = list(node.args) + [kw.value for kw in node.keywords]
            candidates.extend(
                child for child in ast.walk(node.func) if isinstance(child, ast.Constant)
            )
            for arg in candidates:
                literal = _literal_str(arg)
                if literal is None:
                    continue
                why = _writes_somewhere_protected(literal)
                if why:
                    self._block(
                        node,
                        f"writes to {literal} ({why}) through {name}().",
                    )

        self.generic_visit(node)

    def _decoder_in(self, node: ast.AST) -> str:
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                inner = _call_name(child)
                if inner in _DECODERS:
                    return _dotted(child.func) or inner
        return ""

    # -- environment ------------------------------------------------------
    def visit_Attribute(self, node: ast.Attribute) -> None:
        if _dotted(node) in ("os.environ", "os.environb"):
            self._flag(
                node,
                "reads os.environ directly rather than through the settings "
                "accessor, so what it depends on is not declared anywhere.",
            )
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self.generic_visit(node)


def _scan_source(path: Path, relative: str) -> tuple[list[str], list[str]]:
    """One module's (blocked, review) reasons. Never raises."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return [], [f"{relative}: not scanned ({type(exc).__name__})."]
    if size > MAX_SOURCE_BYTES:
        return [], [
            f"{relative}: not scanned — it is {size} bytes, over the "
            f"{MAX_SOURCE_BYTES}-byte limit for a single source file."
        ]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text, filename=relative)
    except (OSError, SyntaxError, ValueError, RecursionError) as exc:
        return [], [
            f"{relative}: could not be parsed ({type(exc).__name__}), so nothing in it was checked."
        ]
    visitor = _SourceVisitor(relative)
    try:
        visitor.visit(tree)
    except RecursionError:
        return visitor.blocked, [
            *visitor.review,
            f"{relative}: could not be parsed (RecursionError), so the rest of it was not checked.",
        ]
    return visitor.blocked, visitor.review


def _declaration_reasons(contents: WheelContents) -> tuple[list[str], list[str]]:
    """What the manifest and the entry points say about each other."""
    from robothor.plugins.manifest import MANIFEST_NAME, parse_manifest

    blocked: list[str] = []
    review: list[str] = []

    groups = contents.genus_groups()
    manifest = parse_manifest(contents.manifest_text) if contents.manifest_text else None
    if manifest is None:
        if groups:
            blocked.append(
                f"The wheel publishes {', '.join(groups)} but ships no readable "
                f"{MANIFEST_NAME}, so nothing declares what it will contribute."
            )
        else:
            review.append(
                f"The wheel ships no {MANIFEST_NAME} and publishes no genus.* entry "
                "points, so it contributes nothing to this engine."
            )
        return blocked, review

    declared_contract = _declared_contract(contents.manifest_text, manifest.contract_version)
    if not _contract_matches(declared_contract):
        blocked.append(
            f"{MANIFEST_NAME} declares contract_version "
            f"{declared_contract!r}; this engine speaks {CONTRACT_VERSION!r}."
        )

    for group in groups:
        kind = _GROUPS.get(group)
        if kind is None:
            blocked.append(
                f"The wheel publishes into {group}, which is not an extension group "
                "this engine knows."
            )
            continue
        declared = manifest.declares(kind)
        if not declared:
            blocked.append(
                f"The wheel publishes into {group} but {MANIFEST_NAME} declares no "
                f"{kind!r}. Undeclared surface is refused: the loader would import "
                "it before anything could compare the two."
            )
            continue
        reserved = sorted(declared & _builtin_names(group))
        if reserved:
            blocked.append(
                f"{MANIFEST_NAME} claims the built-in name(s) {', '.join(reserved)} in "
                f"{group}. Shadowing a built-in is a takeover, not an extension."
            )

    ambient = sorted(set(groups) & _AMBIENT_GROUPS)
    if ambient:
        review.append(
            f"The wheel contributes to {', '.join(ambient)} — these run without a "
            "tool call, on a schedule or on every turn, so installing it changes "
            "what the engine does by itself."
        )
    return blocked, review


def scan_wheel(contents: WheelContents, *, scan_prompts: bool = False) -> ScanResult:
    """The verdict for one extracted wheel. Pure, offline, deterministic."""
    blocked, review = _declaration_reasons(contents)

    root = contents.root
    dist_info_prefix = contents.dist_info + "/"
    sources: list[tuple[Path, str]] = []
    prompt_files: list[str] = []
    unscanned = 0

    try:
        walk = sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())
    except OSError as exc:
        return ScanResult(
            verdict=REVIEW,
            reasons=(f"The wheel could not be walked ({type(exc).__name__}).",),
            prompt_scan="static-only",
        )

    for path in walk:
        relative = path.relative_to(root).as_posix()
        if relative.startswith(dist_info_prefix):
            # A wheel's own metadata. METADATA holds the long description, which
            # is not prompt text shipped TO a model, and nothing here is code.
            continue
        if path.suffix == ".py":
            sources.append((path, relative))
        elif _is_prompt_file(relative):
            prompt_files.append(relative)

    if len(sources) > MAX_SCAN_FILES:
        unscanned = len(sources) - MAX_SCAN_FILES
        sources = sources[:MAX_SCAN_FILES]

    for path, relative in sources:
        file_blocked, file_review = _scan_source(path, relative)
        blocked.extend(file_blocked)
        review.extend(file_review)

    if unscanned:
        review.append(
            f"{unscanned} Python file(s) were not scanned — the wheel holds more "
            f"than the {MAX_SCAN_FILES}-file limit, so the scan is incomplete."
        )

    prompt_scan = "static-only"
    if prompt_files:
        if scan_prompts:
            prompt_scan, prompt_reasons = _screen_prompts(root, prompt_files)
            review.extend(prompt_reasons)
        review.append(
            f"The wheel ships prompt text ({', '.join(prompt_files[:5])}"
            f"{', …' if len(prompt_files) > 5 else ''}). Text a model reads is an "
            f"instruction channel; prompt screening here is {prompt_scan}."
        )

    verdict = BLOCKED if blocked else (REVIEW if review else SAFE)
    return ScanResult(
        verdict=verdict,
        reasons=tuple(blocked + review),
        prompt_scan=prompt_scan,
        files_scanned=len(sources),
        prompt_files=tuple(prompt_files),
    )


def _screen_prompts(root: Path, relatives: list[str]) -> tuple[str, list[str]]:
    """Run the platform's existing injection screen over the wheel's prompt text.

    OPT-IN, and off by default, for two reasons that pull the same way. The
    screen belongs to the unattended-run path
    (:func:`robothor.engine.cron_safety.scan_assembled_cron_prompt`) and its
    pattern set is tuned for an assembled prompt rather than for documentation,
    so running it over every README would produce findings an operator learns to
    scroll past. And this module's contract is pure and offline: importing an
    engine module on the default path would make the verdict depend on what else
    is installed.

    So the default verdict for prompt text is ``static-only`` -- the honest
    statement that the bytes were noticed and not read. An import or a scan that
    fails here returns ``static-only`` with a sentence, never silence: "we could
    not check" reported as "clean" is the inert control this platform keeps
    shipping.
    """
    try:
        from robothor.engine.cron_safety import scan_assembled_cron_prompt
    except Exception as exc:  # noqa: BLE001 - no engine on the path is a normal state
        return "static-only", [
            "The prompt screen is not available on this instance "
            f"({type(exc).__name__}), so the shipped prompt text was not screened."
        ]

    findings: list[str] = []
    for relative in relatives[:MAX_SCAN_FILES]:
        try:
            text = (root / relative).read_text(encoding="utf-8", errors="replace")
            finding = scan_assembled_cron_prompt(text)
        except Exception as exc:  # noqa: BLE001
            return "static-only", [
                f"{relative}: the prompt screen could not run ({type(exc).__name__}), "
                "so this file was not screened."
            ]
        if finding:
            findings.append(f"{relative}: the prompt screen flagged this text ({finding}).")
    return ("flagged" if findings else "screened"), findings
