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

#: Modules whose presence is worth telling the operator about even when no
#: dangerous call is reached. ``import subprocess`` was not a reason at all
#: before a hostile review pointed out that a plugin could shell out through a
#: list argv and be graded ``safe``; the import is the thing a reviewer wants
#: to see, whatever the call site looks like.
_NOTABLE_MODULES = {
    "os": "the process, filesystem and environment surface.",
    "subprocess": "it can start other programs.",
    "socket": "raw network access.",
    "importlib": "it can import modules by name at runtime.",
    "shutil": "bulk filesystem operations.",
    "pty": "it can allocate a terminal for another process.",
    "multiprocessing": "it can start other interpreters.",
}

#: The builtins that turn data into code.
_BUILTIN_EXEC = frozenset({"exec", "eval", "compile"})

#: ``os`` functions that replace or spawn a process image.
_OS_EXEC = frozenset({"system", "popen", "fork", "forkpty", "startfile"})
_OS_EXEC_PREFIXES = ("exec", "spawn", "posix_spawn")

#: Calls that hand back a namespace dictionary. Indexing one is name
#: resolution by string, which is the shape every evasion took.
_NAMESPACE_CALLS = frozenset({"globals", "locals", "vars"})

#: What :meth:`_SourceVisitor._resolve` returns for a name chosen at runtime.
#: Its own value rather than ``""`` because "computed" is a FINDING, and the
#: whole class of defects here came from treating "we could not tell" as "it
#: was fine".
_DYNAMIC = "<dynamic>"

#: Filename shapes that carry instructions to a MODEL rather than to a CPU.
#: Deliberately narrow: matching every ``*.md`` meant a plugin that merely
#: vendored a README needed ``--accept-review`` on every install, which is how
#: a review verdict stops meaning anything.
_PROMPT_SUFFIXES = (".prompt",)
_PROMPT_STEMS = ("skill", "instructions", "system_prompt", "prompt")
#: Directories whose whole contents are read by a model.
_PROMPT_DIRS = ("skills", "prompts", "instructions")

#: Suffixes that are inert data: the scan reads their TYPE and concludes they
#: cannot execute. This is what makes "every member is accounted for" true
#: without making every member a finding.
_ASSET_SUFFIXES = frozenset(
    {
        ".yaml",
        ".yml",
        ".json",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".csv",
        ".tsv",
        ".xml",
        ".html",
        ".htm",
        ".css",
        ".jinja",
        ".j2",
        ".template",
        ".sql",
        ".po",
        ".mo",
        ".pot",
        ".prompt",
        ".license",
        ".in",
        ".typed",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".ico",
        ".webp",
        ".bmp",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".pdf",
    }
)

#: Extensionless files that are conventionally documentation or metadata.
_ASSET_STEMS = frozenset(
    {
        "license",
        "licence",
        "copying",
        "notice",
        "readme",
        "changelog",
        "changes",
        "authors",
        "contributors",
        "manifest",
        "py.typed",
        "record",
        "wheel",
        "metadata",
        "installer",
        "requested",
        "entry_points",
        "top_level",
        "namespace_packages",
        "zip-safe",
        "dependency_links",
        "not-zip-safe",
        "pbr",
        "direct_url",
    }
)

#: Anything that runs as a program rather than as data.
_NATIVE_SUFFIXES = frozenset({".so", ".pyd", ".dylib", ".dll", ".a", ".o", ".exe", ".bin"})
_SCRIPT_SUFFIXES = frozenset(
    {".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd", ".pl", ".rb", ".php", ".js", ".mjs"}
)

#: ``*.data/`` subdirectories pip installs OUTSIDE the package: onto PATH,
#: under ``sys.prefix``, into the include tree. A plugin has no business in
#: any of them, and ``scripts/`` in particular is how a wheel ships a shell
#: script the operator later runs by name.
_DATA_ESCAPES = ("scripts", "data", "headers")

#: Refused wholesale, wherever it sits. ``site.py`` executes every ``.pth``
#: line beginning with ``import`` at EVERY interpreter start -- before the
#: loader, before the manifest gate, before ``enabled: false``. A built wheel
#: has no legitimate reason to carry one.
_BOOTSTRAP_SUFFIX = ".pth"

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
    #: How many members the scan classified. Reported so "we did not look" is a
    #: number an operator can compare against the wheel, rather than a claim.
    members_accounted: int = 0

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
            "members_accounted": self.members_accounted,
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
    """Whether this file is text a MODEL reads, not text a person reads.

    Narrowed after a hostile review measured the cost of the old rule: every
    ``*.md`` counted, so a plugin vendoring a README needed ``--accept-review``
    forever and the reason stopped carrying information. A README is
    documentation; ``SKILL.md`` and ``instructions.txt`` are an instruction
    channel into the agent.
    """
    from pathlib import PurePosixPath

    path = PurePosixPath(relative)
    if path.suffix.lower() in _PROMPT_SUFFIXES:
        return True
    if any(part.lower() in _PROMPT_DIRS for part in path.parts[:-1]):
        return True
    return path.stem.lower().startswith(_PROMPT_STEMS)


def _classify_member(relative: str, dist_info: str) -> tuple[str, str]:
    """``(kind, sentence)`` for one wheel member. ``kind`` drives the verdict.

    Kinds: ``source`` (parse it), ``prompt`` (text a model reads), ``asset``
    (inert, allowed by type), ``blocked`` (say why).

    The old walk had only the first two and dropped everything else on the
    floor, so four wheels carrying a payload that executes -- a ``.pth``, a
    compiled extension, a ``*.data/scripts/`` shell script, and code parked in
    ``.dist-info/`` -- graded ``safe`` with zero reasons. Every member is
    accounted for here, and anything this cannot classify is refused rather
    than ignored.
    """
    from pathlib import PurePosixPath

    path = PurePosixPath(relative)
    suffix = path.suffix.lower()
    parts = path.parts

    if suffix == _BOOTSTRAP_SUFFIX:
        return "blocked", (
            f"{relative} is a .pth file. site.py executes one at EVERY interpreter "
            "start -- before the loader, before the manifest gate and before a "
            "disabled row is read -- so the plugin would run without ever being "
            "loaded. This installer only accepts pure-Python wheels; compiled or "
            "bootstrap payloads are refused."
        )
    if suffix in _NATIVE_SUFFIXES:
        return "blocked", (
            f"{relative} is a compiled extension. Nothing here can read machine "
            "code. This installer only accepts pure-Python wheels; compiled or "
            "bootstrap payloads are refused."
        )
    if suffix in _SCRIPT_SUFFIXES:
        return "blocked", (
            f"{relative} is a script in another language, which nothing here can "
            "read. This installer only accepts pure-Python wheels; compiled or "
            "bootstrap payloads are refused."
        )
    for index, part in enumerate(parts[:-1]):
        if part.endswith(".data") and index + 1 < len(parts) - 1:
            where = parts[index + 1]
            if where in _DATA_ESCAPES:
                return "blocked", (
                    f"{relative} is installed OUTSIDE the package (*.data/{where}/ "
                    "goes onto PATH or under sys.prefix). A plugin contributes "
                    "through its entry points, not by writing into the environment."
                )

    if suffix == ".py":
        return "source", ""
    if _is_prompt_file(relative):
        return "prompt", ""
    if suffix in _ASSET_SUFFIXES:
        return "asset", ""
    if not suffix and path.name.lower() in _ASSET_STEMS:
        return "asset", ""
    if relative.startswith(dist_info + "/") and path.name.lower() in _ASSET_STEMS:
        return "asset", ""
    return "blocked", (
        f"{relative} is a file type this scan cannot read, so nothing can say what "
        "it does. This installer only accepts pure-Python wheels and inert data "
        "(yaml, json, markdown, text, images); anything else is refused."
    )


def _literal_str(node: ast.AST) -> str | None:
    """The value of a plain string literal, or None when it is anything else.

    Deliberately NOT ``ast.literal_eval``: ``"ex" + "ec"`` is a literal to
    ``literal_eval`` and a dynamic construction to a reviewer, and telling those
    apart is the entire point of the exec rule.
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
    """One module's findings, resolved through names rather than spellings.

    The first version of this class matched on the text at the call site --
    ``os.system(...)`` was blocked, ``from os import system; system(...)`` was
    not. A hostile review produced eight one-line evasions that all graded
    ``safe`` while running a shell command, two of them the exact constructs the
    task brief had named. The lesson is structural: a scanner that compares
    spellings is defeated by every rename Python allows.

    So this builds a per-module binding table and resolves every call target
    through it:

    * ``import subprocess as s`` binds ``s`` -> ``subprocess``
    * ``from os import system`` binds ``system`` -> ``os.system``
    * ``e = exec`` binds ``e`` -> ``builtins.exec``
    * ``__import__("os")`` and ``importlib.import_module("os")`` RESOLVE to the
      module they name, so ``__import__("os").system(c)`` is ``os.system``
    * anything whose name is computed at runtime -- a non-literal
      ``__import__``/``import_module``/``getattr``, a ``globals()[...]`` lookup,
      a concatenated module name -- resolves to :data:`_DYNAMIC` and is refused
      on that ground alone, because a name this cannot read is a name it cannot
      judge.

    The binding table is deliberately simple (module scope plus straight-line
    assignment, last write wins). It is a lower bound on what the code can
    reach, not a proof: a determined author can still defeat it with control
    flow. What it must never do is the opposite -- report ``safe`` over an
    obvious rename -- and that is what the table buys.
    """

    def __init__(self, relative: str) -> None:
        self.relative = relative
        self.blocked: list[str] = []
        self.review: list[str] = []
        #: local name -> canonical dotted origin ("s" -> "subprocess").
        self.bindings: dict[str, str] = {}

    # -- helpers ----------------------------------------------------------
    def _at(self, node: ast.AST) -> str:
        return f"{self.relative}:{getattr(node, 'lineno', 0)}"

    def _block(self, node: ast.AST, sentence: str) -> None:
        self.blocked.append(f"{self._at(node)}: {sentence}")

    def _flag(self, node: ast.AST, sentence: str) -> None:
        self.review.append(f"{self._at(node)}: {sentence}")

    # -- binding table ----------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.split(".", 1)[0]
            self.bindings[alias.asname or top] = alias.name if alias.asname else top
            self._module(node, alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if not node.module:
            # A relative import inside the distribution. It binds names to the
            # plugin's own modules, which this walk sees separately.
            self.generic_visit(node)
            return
        for alias in node.names:
            self.bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        self._module(node, node.module)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        """Straight-line rebinding: ``e = exec``, ``s = subprocess``, ``m = __import__("os")``."""
        origin = self._resolve(node.value)
        if origin and origin != _DYNAMIC:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.bindings[target.id] = origin
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
        elif top in _NOTABLE_MODULES:
            self._flag(node, f"imports {top}: {_NOTABLE_MODULES[top]}")

    # -- name resolution --------------------------------------------------
    def _resolve(self, node: ast.AST) -> str:
        """The canonical dotted origin of an expression, or "" when unknown.

        :data:`_DYNAMIC` means "this names something chosen at runtime", which
        is itself a finding rather than an absence of one.
        """
        if isinstance(node, ast.Name):
            bound = self.bindings.get(node.id)
            if bound:
                return bound
            if node.id in _BUILTIN_EXEC or node.id in ("getattr", "__import__"):
                return f"builtins.{node.id}"
            return node.id
        if isinstance(node, ast.Attribute):
            base = self._resolve(node.value)
            if base == _DYNAMIC:
                return _DYNAMIC
            return f"{base}.{node.attr}" if base else node.attr
        if isinstance(node, ast.Call):
            return self._resolve_call_result(node)
        if isinstance(node, ast.Subscript):
            # ``globals()["ev" + "al"]`` and friends.
            container = self._resolve(node.value)
            if container in _NAMESPACE_CALLS:
                return _DYNAMIC
            return ""
        return ""

    def _resolve_call_result(self, node: ast.Call) -> str:
        """What a call EVALUATES to, for the two calls that return a module."""
        target = self._resolve(node.func)
        if target in ("builtins.__import__", "importlib.import_module"):
            named = _literal_str(node.args[0]) if node.args else None
            return named.split(".", 1)[0] if named else _DYNAMIC
        if target == "builtins.getattr":
            if len(node.args) >= 2:
                attribute = _literal_str(node.args[1])
                if attribute is None:
                    return _DYNAMIC
                base = self._resolve(node.args[0])
                if base == _DYNAMIC:
                    return _DYNAMIC
                return f"{base}.{attribute}" if base else ""
            return _DYNAMIC
        if target in _NAMESPACE_CALLS:
            return target
        return ""

    # -- calls ------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        target = self._resolve(node.func)
        self._judge_call(node, target)
        self._judge_writes(node)
        self.generic_visit(node)

    def _judge_call(self, node: ast.Call, target: str) -> None:
        if target == _DYNAMIC:
            self._block(
                node,
                "calls something found by dynamic name resolution, so what runs "
                "cannot be read here.",
            )
            return
        if not target:
            return

        head, _, attribute = target.partition(".")

        # FIRST, because it is the most specific thing that can be said about
        # the call: `subprocess.run(x, shell=True)` is refused for the shell,
        # not merely for being a subprocess call, and the operator reading the
        # reason wants the sharper sentence.
        for keyword in node.keywords:
            if (
                keyword.arg == "shell"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
            ):
                self._block(
                    node,
                    f"calls {target}() with shell=True, which hands a string to a shell.",
                )
                return

        if head == "builtins" and attribute in _BUILTIN_EXEC:
            first = node.args[0] if node.args else None
            if first is None:
                self._block(node, f"calls {attribute}() with no readable argument.")
            elif _literal_str(first) is None:
                decoder = self._decoder_in(first)
                if decoder:
                    self._block(
                        node,
                        f"calls {attribute}() on the output of {decoder}(), which is "
                        "code hidden from review inside encoded data.",
                    )
                else:
                    self._block(
                        node,
                        f"calls {attribute}() on input that is not a literal, so what "
                        "runs cannot be read here.",
                    )
            return

        if target == "builtins.__import__":
            if not node.args or _literal_str(node.args[0]) is None:
                self._block(
                    node,
                    "calls __import__() on a name computed at runtime — dynamic name "
                    "resolution, so which module is imported cannot be read here.",
                )
            return

        if target == "importlib.import_module":
            if not node.args or _literal_str(node.args[0]) is None:
                self._block(
                    node,
                    "calls importlib.import_module() on a name computed at runtime — "
                    "dynamic name resolution, so which module is imported cannot be "
                    "read here.",
                )
            return

        if target == "builtins.getattr":
            if len(node.args) >= 2 and _literal_str(node.args[1]) is None:
                receiver = self._resolve(node.args[0]) or "an object"
                self._block(
                    node,
                    f"looks an attribute up on {receiver} by a name computed at runtime "
                    "— dynamic name resolution, which is how exec() and os.system() are "
                    "reached without naming them.",
                )
            return

        if head == "subprocess":
            self._block(
                node,
                f"calls {target}(), which starts a process. A plugin runs inside the "
                "daemon; spawning is outside every guardrail applied to it.",
            )
            return

        if head == "os" and (attribute in _OS_EXEC or attribute.startswith(_OS_EXEC_PREFIXES)):
            self._block(node, f"calls {target}(), which executes a program directly.")
            return

        if target in ("socket.socket", "socket.create_connection") or (
            attribute == "connect" and head == "socket"
        ):
            self._block(
                node,
                "opens a raw socket. A plugin that needs the network should use a "
                "declared HTTP client so the engine's egress rules apply.",
            )
            return

        for keyword in node.keywords:
            if (
                keyword.arg == "shell"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
            ):
                self._block(
                    node,
                    f"calls {target or '?'}() with shell=True, which hands a string to a shell.",
                )
                return

    def _judge_writes(self, node: ast.Call) -> None:
        name = target_attr = ""
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = target_attr = node.func.attr
        if name not in _WRITE_CALLS:
            return
        # The literal is as often in the RECEIVER as in the arguments:
        # ``Path("~/.ssh/authorized_keys").write_text(key)`` puts it in neither
        # ``args`` nor ``keywords``, and checking only those let the most
        # obvious spelling of the attack through.
        candidates: list[ast.AST] = [*node.args, *(kw.value for kw in node.keywords)]
        candidates.extend(child for child in ast.walk(node.func) if isinstance(child, ast.Constant))
        for arg in candidates:
            literal = _literal_str(arg)
            if literal is None:
                continue
            why = _writes_somewhere_protected(literal)
            if why:
                self._block(node, f"writes to {literal} ({why}) through {target_attr or name}().")
                return

    def _decoder_in(self, node: ast.AST) -> str:
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                resolved = self._resolve(child.func)
                leaf = resolved.rpartition(".")[2] or resolved
                if leaf in _DECODERS:
                    return resolved or leaf
        return ""

    # -- subscripts and bare references -----------------------------------
    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self._resolve(node.value) in _NAMESPACE_CALLS:
            self._block(
                node,
                "indexes the module namespace (globals/locals/vars) by name — dynamic "
                "name resolution, so what is being fetched cannot be read here.",
            )
        self.generic_visit(node)

    # -- environment ------------------------------------------------------
    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self._resolve(node) in ("os.environ", "os.environb"):
            self._flag(
                node,
                "reads os.environ directly rather than through the settings "
                "accessor, so what it depends on is not declared anywhere.",
            )
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
    """The verdict for one extracted wheel. Pure, offline, deterministic.

    **Every member is accounted for.** Code is parsed, prompt text is named,
    inert data is allowed by TYPE, and anything else is refused with the file
    named. The first version classified a walked file as either Python or
    prompt text and silently dropped the rest, which meant a wheel whose whole
    payload was a ``.pth``, a ``.so`` or a ``*.data/scripts/`` shell script came
    back ``safe`` with zero reasons -- a clean verdict about bytes nobody had
    opened.
    """
    blocked, review = _declaration_reasons(contents)

    root = contents.root
    sources: list[tuple[Path, str]] = []
    prompt_files: list[str] = []
    unscanned = 0
    members = 0

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
        members += 1
        kind, sentence = _classify_member(relative, contents.dist_info)
        if kind == "blocked":
            blocked.append(sentence)
        elif kind == "source":
            sources.append((path, relative))
        elif kind == "prompt":
            prompt_files.append(relative)

    # The execute bit is a header fact: extraction narrows every member to
    # 0600, so this is the only place it can still be seen.
    for member in contents.executable_members:
        blocked.append(
            f"{member} is an executable member: it ships with the execute bit set in "
            "the wheel's own headers. A plugin contributes through its entry points, "
            "never as a program somebody runs."
        )

    if len(contents.manifest_candidates) > 1:
        # Two declarations, and the installer pins one while the loader reads
        # the other. The drift check would catch the divergence at load time,
        # but "installed, verdict safe, never loads" is not an answer -- and the
        # property this whole pipeline sells ("the manifest the index signed is
        # the one the engine enforces") would not be established.
        blocked.append(
            "The wheel carries more than one genus-plugin.yaml ("
            + ", ".join(contents.manifest_candidates)
            + "), so which declaration the engine would enforce is ambiguous."
        )

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
    if scan_prompts:
        # Screened over prompt text AND documentation: with the prompt rule
        # narrowed to files a model actually reads, a README is no longer an
        # automatic `review`, so the screen is the only thing that can raise
        # one over a document -- which is what makes the flag able to change a
        # verdict at all rather than only adding sentences under one.
        screened = sorted({*prompt_files, *(_text_members(root))})
        prompt_scan, prompt_reasons = _screen_prompts(root, screened)
        review.extend(prompt_reasons)
    if prompt_files:
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
        members_accounted=members,
    )


def _text_members(root: Path) -> list[str]:
    """Documentation the prompt screen can read, when asked to."""
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix.lower() in (".md", ".markdown", ".rst", ".txt"):
            found.append(path.relative_to(root).as_posix())
    return found


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
