"""What a task's warmup is allowed to assume the image provides.

WildClawBench tasks were written against the benchmark authors' own container,
so their warmups take a whole environment for granted and never declare it.
Every one of those assumptions is a way for a task to score zero without
running, and each has cost us a measurement already:

* `npm install -g agent-browser` — six of ten Productivity tasks. The image
  had no npm; the warmup failed silently and six tasks ran without the skill
  every other harness gets (2026-09-16).
* `~/miniconda3/envs/eval/bin/pip install …` — the two SAM3 tasks. The image
  had no such path, so the warmup exited 127. Once warmups started failing
  loudly, those two were skipped and scored 0 without running.

The fix for each was a line in `bench/wildclaw/Dockerfile`; what was missing
was anything that would have said so BEFORE a sweep. This module is the
written-down contract — every command head a warmup may open with, and the
thing in the image that answers it — and `tests/test_warmup_environment.py`
checks the corpus against it (statically) and the built image against it
(under `slow`, via `podman run`).

Adding an entry here is a claim about the image. The slow test is what makes
it a checked one.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

#: The `eval` environment the Code Intelligence tasks name by absolute path.
#: A venv, not a conda install — see the Dockerfile for why, and
#: `conda_shim.sh` for what `conda` itself is here.
EVAL_PREFIX = "/root/miniconda3/envs/eval"

#: Shell builtins and control words. Not binaries, always available, and a
#: warmup line can legitimately open with any of them (`export FOO=… && …`).
SHELL_WORDS = frozenset(
    {
        "cd",
        "echo",
        "eval",
        "exec",
        "exit",
        "export",
        "false",
        "printf",
        "pwd",
        "read",
        "set",
        "source",
        "test",
        "trap",
        "true",
        "unset",
        "wait",
        ".",
        ":",
    }
)

#: Command name -> what puts it in the image. Every value names a line of
#: `bench/wildclaw/Dockerfile` (or the base it builds on), so a reader can
#: check the claim without leaving the repository.
IMAGE_BINARIES: dict[str, str] = {
    # coreutils / util-linux, from the Debian base.
    "cat": "debian base (coreutils)",
    "chmod": "debian base (coreutils)",
    "cp": "debian base (coreutils)",
    "ln": "debian base (coreutils)",
    "ls": "debian base (coreutils)",
    "mkdir": "debian base (coreutils)",
    "mv": "debian base (coreutils)",
    "rm": "debian base (coreutils)",
    "sed": "debian base",
    "sleep": "debian base (coreutils)",
    "tar": "debian base",
    "touch": "debian base (coreutils)",
    # apt, from the Debian base. Both spellings appear in the corpus.
    "apt": "debian base",
    "apt-get": "debian base",
    "dpkg": "debian base",
    # Added by the tools layer.
    "curl": "Dockerfile: apt-get install curl",
    "ffmpeg": "Dockerfile: apt-get install ffmpeg",
    "ffprobe": "Dockerfile: apt-get install ffmpeg",
    "git": "Dockerfile: apt-get install git",
    "jq": "Dockerfile: apt-get install jq",
    "node": "Dockerfile: apt-get install nodejs",
    "npm": "Dockerfile: apt-get install npm",
    "npx": "Dockerfile: apt-get install npm",
    "pdftotext": "Dockerfile: apt-get install poppler-utils",
    "unzip": "Dockerfile: apt-get install unzip",
    # Python. `pip`/`python3` resolve to /opt/venv — the whole point of the
    # ensurepip line in the Dockerfile is that they resolve to the SAME
    # interpreter, because they did not once and every warmup install landed
    # where nothing imported from.
    "pip": "Dockerfile: ensurepip into /opt/venv",
    "pip3": "Dockerfile: ensurepip into /opt/venv",
    "python": "/opt/venv (base image)",
    "python3": "/opt/venv (base image)",
    "playwright": "Dockerfile: pip install playwright into /opt/venv",
    # A shim, deliberately, and it says so when asked to do anything it is
    # not: bench/wildclaw/conda_shim.sh.
    "conda": "Dockerfile: COPY conda_shim.sh -> /root/miniconda3/bin/conda",
}

#: Absolute (or `~`-rooted) command paths a warmup may name, and what creates
#: each. Unlike a bare name these cannot be satisfied by anything on PATH: the
#: task asks for that exact file.
IMAGE_PATHS: dict[str, str] = {
    f"{EVAL_PREFIX}/bin/pip": "Dockerfile: python3 -m venv /root/miniconda3/envs/eval",
    f"{EVAL_PREFIX}/bin/pip3": "Dockerfile: python3 -m venv /root/miniconda3/envs/eval",
    f"{EVAL_PREFIX}/bin/python": "Dockerfile: python3 -m venv /root/miniconda3/envs/eval",
    f"{EVAL_PREFIX}/bin/python3": "Dockerfile: python3 -m venv /root/miniconda3/envs/eval",
    "/root/miniconda3/bin/conda": "Dockerfile: COPY conda_shim.sh",
}

#: Assumptions we have NOT closed, and what a task pays for each. Listed so
#: they are a known cost rather than a surprise in a transcript; a new entry
#: here is a decision, and the test that reads this module prints it.
KNOWN_GAPS: dict[str, str] = {
    "torch (in the eval environment)": (
        "The SAM3 codebase depends on timm, hence torch, and the two SAM3 warmups "
        "install only numpy and opencv — so the authors' eval environment carries "
        "torch already. A CPU torch is ~1GB on an image all 60 tasks pull, for two "
        "of them. Build with `--build-arg EVAL_TORCH=1` to close it; otherwise the "
        "agent installs it itself, inside its 1200s budget."
    ),
    "network at warmup time": (
        "`apt-get update`, `npm install -g`, `pip install` and "
        "`playwright install chromium` all reach the network. ffmpeg, "
        "poppler-utils, playwright and the two SAM3 pins are pre-installed so "
        "those lines are satisfied no-ops, but a warmup naming anything else "
        "still needs a reachable mirror."
    ),
}

#: A warmup is the fenced block under a `## Warmup` heading. Read from the
#: spec rather than through the harness's loader, so a test reads what the
#: benchmark wrote.
_WARMUP_RE = re.compile(
    r"^##\s+Warmup\s*$\n+```[a-z]*\n(.*?)^```", re.MULTILINE | re.DOTALL | re.IGNORECASE
)

#: Where one shell line ends and the next command begins. Deliberately crude:
#: it only has to find the HEAD of each command, and over-splitting yields a
#: token that is either resolvable or a finding worth looking at.
_SEPARATORS = re.compile(r"&&|\|\||[;|]|\bthen\b|\bdo\b")

#: Words that precede the real command without being one.
_PREFIXES = frozenset({"sudo", "nohup", "time", "if", "for", "while", "until"})


def declared_warmup(spec_text: str) -> str:
    """The warmup block a task spec declares, or "" when it declares none."""
    match = _WARMUP_RE.search(spec_text)
    return match.group(1) if match else ""


def command_heads(warmup: str) -> list[str]:
    """Every command head in a warmup block, in order, with duplicates kept.

    `VAR=x cmd` yields `cmd`: an assignment prefix is not the command. A bare
    `VAR=x` with nothing after it yields nothing, which is correct — it runs no
    binary at all.
    """
    heads: list[str] = []
    for raw in warmup.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for part in _SEPARATORS.split(line):
            part = part.strip()
            if not part:
                continue
            try:
                words = shlex.split(part)
            except ValueError:
                words = part.split()
            for word in words:
                # An assignment prefix (`FOO=bar cmd`) — but not a path, which
                # may legitimately contain `=` after its first segment.
                head_segment = word.split("/")[0]
                if "=" in head_segment and not word.startswith(("/", "~", ".")):
                    continue
                if word in _PREFIXES:
                    continue
                heads.append(word)
                break
    return heads


def resolve(token: str) -> str | None:
    """What the image offers for this command head, or None if nothing does.

    `~` is expanded against `/root`, which is what the container's HOME is and
    what the Dockerfile pins it to.
    """
    if token in SHELL_WORDS:
        return "shell builtin"
    if token.startswith(("/", "~", "./")):
        absolute = token.replace("~", "/root", 1) if token.startswith("~") else token
        return IMAGE_PATHS.get(absolute)
    return IMAGE_BINARIES.get(token)


def unresolved(tasks_dir: Path) -> dict[str, list[str]]:
    """Command heads no entry above accounts for -> the specs that use them."""
    missing: dict[str, list[str]] = {}
    for spec in sorted(tasks_dir.rglob("*.md")):
        warmup = declared_warmup(spec.read_text(encoding="utf-8", errors="replace"))
        for head in command_heads(warmup):
            if resolve(head) is None:
                missing.setdefault(head, []).append(spec.name)
    return missing


def probe_commands() -> list[str]:
    """The names a `command -v` probe of the built image should run.

    Bare names and absolute paths; shell builtins are excluded because
    `command -v` answers for those whether or not the image is right.
    """
    return sorted(IMAGE_BINARIES) + sorted(IMAGE_PATHS)
