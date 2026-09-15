"""``scripts/install.sh`` is the front door, so its refusals are the contract.

The one-liner in the quickstart is the first command a stranger runs, piped
straight into ``bash`` from a URL. Everything that makes that acceptable is a
property of this script and nothing else: it refuses root, it refuses to be
interpreted by ``sh``, it never ``sudo``s, it never pipes anything else into a
shell, every download is pinned to a release tag rather than ``main``, and a
run that was not explicitly told ``--yes`` writes nothing at all.

So the tests run the real script under ``bash`` with a PATH of recording
shims — ``curl``, ``docker``, ``pipx``, ``python3``, ``id`` — in a throwaway
HOME, and assert on what it printed and on what it did to the filesystem.
Nothing here reaches the network, starts a container, or touches the machine
it runs on.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "install.sh"

#: A tag that is deliberately not any real release, so a download that fell
#: back to `main` or to the stamped default is visible in the assertion.
PINNED = "v9.9.9"

#: What the release API shim answers with, when a test lets it answer.
API_TAG = "v8.8.8"


# --------------------------------------------------------------------------
# shims
# --------------------------------------------------------------------------

#: A `curl` that never leaves the machine. `--max-time` and every URL are
#: appended to $SHIM_LOG; `-o PATH` targets are filled with a parseable stub.
CURL_SHIM = r"""#!/usr/bin/env bash
set -euo pipefail
printf 'curl %s\n' "$*" >> "$SHIM_LOG"

mode="${SHIM_CURL_MODE:-ok}"
out=""
url=""
prev=""
for arg in "$@"; do
  case "$prev" in
    -o|--output) out="$arg" ;;
  esac
  case "$arg" in
    http*) url="$arg" ;;
  esac
  prev="$arg"
done

case "$url" in
  *api.github.com*)
    if [ "$mode" = "api-fails" ]; then
      exit 28
    fi
    if [ "$mode" = "api-hangs" ]; then
      # Deliberately ignores --max-time: only an outer bound can save a run
      # from a curl that does not honour its own flag.
      sleep "${SHIM_HANG_SECONDS:-45}"
      exit 0
    fi
    printf '{"tag_name": "%s", "name": "release"}\n' "$SHIM_API_TAG"
    exit 0
    ;;
esac

if [ "$mode" = "download-fails" ]; then
  echo "curl: (22) The requested URL returned error: 404" >&2
  exit 22
fi

if [ -n "$out" ]; then
  printf 'services:\n  placeholder:\n    image: "busybox:${GENUS_IMAGE_TAG:?set it}"\n' > "$out"
fi
exit 0
"""

DOCKER_SHIM = r"""#!/usr/bin/env bash
set -euo pipefail
printf 'docker %s\n' "$*" >> "$SHIM_LOG"
if [ "${1:-}" = "compose" ] && [ "${2:-}" = "version" ]; then
  echo "Docker Compose version v2.29.7"
  exit 0
fi
case "$*" in
  # What compose would interpolate from, recorded from the environment the
  # installer handed it rather than from argv.
  *config*)
    printf 'config-env GENUS_IMAGE_TAG=%s ROBOTHOR_DB_PASSWORD=%s\n' \
      "${GENUS_IMAGE_TAG:-unset}" "${ROBOTHOR_DB_PASSWORD:-unset}" >> "$SHIM_LOG"
    ;;
esac
exit 0
"""

PIPX_SHIM = r"""#!/usr/bin/env bash
set -euo pipefail
printf 'pipx %s\n' "$*" >> "$SHIM_LOG"
exit 0
"""

#: `python3 -m venv DIR` is the only thing the installer asks python for. The
#: shim builds the two entry points the script then calls by absolute path,
#: each of which records its own argv.
PYTHON_SHIM = r"""#!/usr/bin/env bash
set -euo pipefail
printf 'python3 %s\n' "$*" >> "$SHIM_LOG"
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ]; then
  venv="${3:?venv path}"
  mkdir -p "$venv/bin"
  cat > "$venv/bin/pip" <<PIP
#!/usr/bin/env bash
printf 'pip %s\n' "\$*" >> "\$SHIM_LOG"
if [ -n "\${SHIM_PYPI_MISSING:-}" ]; then
  case "\$*" in
    *git+*) exit 0 ;;
    *) echo "ERROR: No matching distribution found for genusos" >&2; exit 1 ;;
  esac
fi
exit 0
PIP
  cat > "$venv/bin/genus" <<GENUS
#!/usr/bin/env bash
printf 'genus %s\n' "\$*" >> "\$SHIM_LOG"
case "\$*" in
  *--json*init*|*init*--json*)
    echo '{"first_run_url": "http://127.0.0.1:3004/setup?token=shim-token", "exit_code": 0}'
    ;;
  *doctor*)
    echo '{"summary": {"required_failed": 0}}'
    ;;
esac
exit 0
GENUS
  chmod +x "$venv/bin/pip" "$venv/bin/genus"
fi
exit 0
"""

ROOT_ID_SHIM = r"""#!/usr/bin/env bash
echo 0
"""

DEFAULT_SHIMS = {
    "curl": CURL_SHIM,
    "docker": DOCKER_SHIM,
    "pipx": PIPX_SHIM,
    "python3": PYTHON_SHIM,
}


class Run:
    """One completed run of the installer, plus what its shims recorded."""

    def __init__(self, completed: subprocess.CompletedProcess[str], log: Path, home: Path):
        self.completed = completed
        self.log = log
        self.home = home

    @property
    def returncode(self) -> int:
        return self.completed.returncode

    @property
    def stdout(self) -> str:
        return self.completed.stdout

    @property
    def stderr(self) -> str:
        return self.completed.stderr

    @property
    def output(self) -> str:
        return self.completed.stdout + self.completed.stderr

    @property
    def calls(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""


@pytest.fixture
def installer(tmp_path: Path):
    """Run ``scripts/install.sh`` in a throwaway HOME with recording shims."""
    assert SCRIPT.is_file(), f"{SCRIPT} is missing"

    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "shim.log"
    log.write_text("", encoding="utf-8")

    def run(
        *args: str,
        shims: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        interpreter: str = "bash",
        script: Path | None = None,
        timeout: int = 60,
    ) -> Run:
        for name, body in {**DEFAULT_SHIMS, **(shims or {})}.items():
            target = bindir / name
            target.write_text(body, encoding="utf-8")
            target.chmod(0o755)

        environ = {
            "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(home),
            "SHIM_LOG": str(log),
            "SHIM_API_TAG": API_TAG,
            "TERM": "dumb",
        }
        environ.update(env or {})
        completed = subprocess.run(  # noqa: S603 - the script under test
            [interpreter, str(script or SCRIPT), *args],
            capture_output=True,
            text=True,
            env=environ,
            cwd=str(tmp_path),
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        return Run(completed, log, home)

    return run


def _tree(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*")}


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


def test_help_exits_zero_and_names_every_flag(installer) -> None:
    result = installer("--help")
    assert result.returncode == 0, result.output
    for flag in ("--substrate", "--version", "--dir", "--yes", "--dry-run", "--help"):
        assert flag in result.stdout, f"--help does not document {flag}"


def test_refuses_to_run_as_root(installer) -> None:
    result = installer("--dry-run", "--substrate", "compose", shims={"id": ROOT_ID_SHIM})
    assert result.returncode != 0
    assert "root" in result.stderr.lower()
    # And it said so before doing anything at all.
    assert "curl" not in result.calls


def test_refuses_to_be_interpreted_by_sh(installer) -> None:
    sh = shutil.which("dash") or shutil.which("sh")
    assert sh, "no POSIX sh on this machine"
    result = installer("--help", interpreter=sh)
    assert result.returncode != 0
    assert "bash" in result.stderr.lower()


def _code_lines() -> list[str]:
    """Every line of the script that is not a comment."""
    return [
        line
        for line in SCRIPT.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]


def test_never_sudos_and_never_pipes_into_a_shell() -> None:
    for line in _code_lines():
        assert not re.search(r"(^|[;&|(]\s*)sudo\s", line), f"the installer elevates: {line}"
        assert not re.search(r"\beval\s", line), f"the installer evals: {line}"
        # A download that reaches a shell is the whole threat this script is
        # allowed to exist despite. `say`/`printf` lines quote the documented
        # one-liner, which is text, not a pipeline.
        if re.search(r"^\s*(say|printf|echo|cat)\b", line):
            continue
        assert not re.search(r"\|\s*(ba)?sh\b", line), f"the installer pipes into a shell: {line}"


def test_every_download_is_pinned_to_a_tag_never_main() -> None:
    body = SCRIPT.read_text(encoding="utf-8")
    assert "raw.githubusercontent.com" in body
    assert "/main/" not in body, "a download from main is not pinned to a release"


# --------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------


def test_dry_run_compose_prints_a_pinned_plan_and_writes_nothing(installer, tmp_path) -> None:
    target = tmp_path / "genus"
    before = _tree(tmp_path)

    result = installer(
        "--substrate",
        "compose",
        "--version",
        PINNED,
        "--dir",
        str(target),
        "--dry-run",
    )

    assert result.returncode == 0, result.output
    out = result.stdout
    base = f"https://raw.githubusercontent.com/Ironsail-llc/genus-os/{PINNED}/infra"
    assert f"{base}/docker-compose.yml" in out
    assert f"{base}/docker-compose.apps.yml" in out
    assert f"genusos=={PINNED.lstrip('v')}" in out
    assert "genus init --substrate compose" in out
    assert "genus doctor --json" in out
    assert "/main/infra" not in out

    assert not target.exists(), "--dry-run created the install directory"
    fixture_owned = {
        entry
        for entry in _tree(tmp_path)
        if entry == "bin" or entry.startswith("bin/") or entry in {"home", "shim.log"}
    }
    assert _tree(tmp_path) - before - fixture_owned == set(), "--dry-run wrote outside the plan"
    assert _tree(result.home) == set(), "--dry-run wrote into HOME"


def test_dry_run_pipx_plan_pins_the_version_and_uses_the_local_substrate(
    installer, tmp_path
) -> None:
    result = installer("--substrate", "pipx", "--version", PINNED, "--dry-run")
    assert result.returncode == 0, result.output
    assert f"pipx install genusos=={PINNED.lstrip('v')}" in result.stdout
    assert "genus init --substrate local" in result.stdout
    assert _tree(result.home) == set()


def test_a_piped_run_without_yes_is_a_preview_that_writes_nothing(installer, tmp_path) -> None:
    """`curl … | bash` with no --yes must never write: stdin is not a terminal."""
    target = tmp_path / "genus"
    result = installer("--substrate", "compose", "--version", PINNED, "--dir", str(target))

    assert result.returncode == 0, result.output
    assert "preview" in result.output.lower()
    assert "--yes" in result.output
    assert not target.exists()
    assert "genus init" not in result.calls
    # A preview whose printed command could not be retyped is a preview of
    # nothing: no empty owner flags.
    assert "--owner-name ''" not in result.output
    assert '--owner-name ""' not in result.output
    assert "--owner-email ''" not in result.output


def test_the_epilogue_says_where_the_cli_is_and_which_workspace_it_serves(
    installer, tmp_path
) -> None:
    """`~/genus` is not the platform's default workspace, and `.venv/bin` is
    not on PATH — an operator who types `genus doctor` next gets either
    "command not found" or a doctor pointed at an empty ~/robothor."""
    target = tmp_path / "genus"
    result = _install(installer, target)
    assert result.returncode == 0, result.output

    out = result.stdout
    assert f"{target}/.venv/bin/genus" in out, "the epilogue never names the CLI it installed"
    assert f"export ROBOTHOR_WORKSPACE={target}" in out or (
        f'ROBOTHOR_WORKSPACE="{target}"' in out
    ), "the epilogue never points the shell at this workspace"
    assert "genus.env" in out, "the epilogue never names the file the stack runs with"


# --------------------------------------------------------------------------
# version resolution
# --------------------------------------------------------------------------


def test_the_version_stamp_line_exists_and_is_a_release_tag() -> None:
    body = SCRIPT.read_text(encoding="utf-8")
    match = re.search(
        r'^INSTALL_SH_DEFAULT_VERSION="(v[0-9]+\.[0-9]+\.[0-9]+)"$', body, re.MULTILINE
    )
    assert match, "scripts/install.sh carries no INSTALL_SH_DEFAULT_VERSION stamp"


def test_the_releases_api_is_asked_with_a_timeout(installer, tmp_path) -> None:
    result = installer("--substrate", "pipx", "--dry-run")
    assert result.returncode == 0, result.output
    api_calls = [line for line in result.calls.splitlines() if "api.github.com" in line]
    assert api_calls, "nothing asked the releases API for the latest tag"
    # The value matters, not just the flag: a metadata refresh that can hang is
    # the reason an install hangs, and curl's own default is no timeout at all.
    seconds = [
        int(match.group(1)) for line in api_calls if (match := re.search(r"--max-time (\d+)", line))
    ]
    assert seconds, "the API call has no timeout"
    assert max(seconds) <= 15, f"the releases API may block for {max(seconds)}s"
    assert f"genusos=={API_TAG.lstrip('v')}" in result.stdout


def test_the_environment_can_supply_the_version(installer) -> None:
    result = installer("--substrate", "pipx", "--dry-run", env={"GENUS_VERSION": PINNED})
    assert result.returncode == 0, result.output
    assert f"genusos=={PINNED.lstrip('v')}" in result.stdout
    assert "api.github.com" not in result.calls, "an explicit version still called the API"


def test_a_releases_api_that_fails_falls_back_to_the_stamp_and_says_so(installer) -> None:
    result = installer(
        "--substrate",
        "pipx",
        "--dry-run",
        env={"SHIM_CURL_MODE": "api-fails"},
        timeout=45,
    )
    assert result.returncode == 0, result.output
    assert "releases api" in result.stderr.lower()
    body = SCRIPT.read_text(encoding="utf-8")
    stamp = re.search(r'^INSTALL_SH_DEFAULT_VERSION="(v[^"]+)"$', body, re.MULTILINE).group(1)
    assert f"genusos=={stamp.lstrip('v')}" in result.stdout


def test_a_releases_api_that_fails_with_no_stamp_is_an_error_not_a_hang(
    installer, tmp_path
) -> None:
    unstamped = tmp_path / "unstamped.sh"
    unstamped.write_text(
        re.sub(
            r'^INSTALL_SH_DEFAULT_VERSION="[^"]*"$',
            'INSTALL_SH_DEFAULT_VERSION=""',
            SCRIPT.read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        ),
        encoding="utf-8",
    )
    result = installer(
        "--substrate",
        "pipx",
        "--dry-run",
        script=unstamped,
        env={"SHIM_CURL_MODE": "api-fails"},
        timeout=45,
    )
    assert result.returncode != 0
    assert "--version" in result.stderr


def test_a_releases_api_that_hangs_is_bounded_by_the_script_not_by_curl(
    installer, tmp_path
) -> None:
    """A curl that ignores --max-time must not be able to wedge an install."""
    result = installer(
        "--substrate",
        "pipx",
        "--dry-run",
        env={"SHIM_CURL_MODE": "api-hangs", "SHIM_HANG_SECONDS": "45"},
        timeout=40,
    )
    assert result.returncode == 0, result.output
    stamp = re.search(
        r'^INSTALL_SH_DEFAULT_VERSION="(v[^"]+)"$', SCRIPT.read_text(encoding="utf-8"), re.MULTILINE
    ).group(1)
    assert f"genusos=={stamp.lstrip('v')}" in result.stdout


@pytest.mark.parametrize(
    "bad",
    ["v1.2.3; touch pwned", "$(touch pwned)", "main", "latest", "1.2.3"],
)
def test_a_version_that_is_not_a_release_tag_is_refused(installer, tmp_path, bad: str) -> None:
    result = installer("--substrate", "pipx", "--dry-run", "--version", bad)
    assert result.returncode != 0, f"{bad!r} was accepted"
    assert "version" in result.stderr.lower()
    assert not (tmp_path / "pwned").exists()


# --------------------------------------------------------------------------
# a real (shimmed) compose install
# --------------------------------------------------------------------------


def _install(installer, target: Path, **kwargs):
    return installer(
        "--substrate",
        "compose",
        "--version",
        PINNED,
        "--dir",
        str(target),
        "--yes",
        "--owner-name",
        "Ada Lovelace",
        "--owner-email",
        "ada@example.com",
        **kwargs,
    )


def test_a_compose_install_downloads_pinned_files_and_runs_the_wizard(installer, tmp_path) -> None:
    target = tmp_path / "genus"
    result = _install(installer, target)
    assert result.returncode == 0, result.output

    calls = result.calls
    for name in ("docker-compose.yml", "docker-compose.apps.yml"):
        assert f"/Ironsail-llc/genus-os/{PINNED}/infra/{name}" in calls
    assert "/main/infra/" not in calls

    assert "docker compose version" in calls, "compose v2 was never verified"
    assert "config -q" in calls, "the downloaded compose files were never parsed"
    # The parse check supplies its own environment rather than pointing compose
    # at a dotenv file, so no unescaping rule but this script's own is in play.
    parse = next(line for line in calls.splitlines() if "config -q" in line)
    assert "--env-file" not in parse, "the parse check trusts compose's dotenv parser"
    env_line = next(line for line in calls.splitlines() if line.startswith("config-env "))
    assert f"GENUS_IMAGE_TAG={PINNED}" in env_line, "compose was not told which tag to resolve"
    assert "ROBOTHOR_DB_PASSWORD=unset" not in env_line
    assert f"pip install --disable-pip-version-check genusos=={PINNED.lstrip('v')}" in calls
    assert "genus init --substrate compose --yes" in calls
    assert f"--workspace {target}" in calls
    assert "genus doctor --json" in calls
    assert "/setup?token=shim-token" in result.stdout, "the first-run URL was never printed"


def test_the_install_directory_may_contain_spaces(installer, tmp_path) -> None:
    target = tmp_path / "my genus dir"
    result = _install(installer, target)
    assert result.returncode == 0, result.output
    assert (target / ".env").is_file()
    assert f"--workspace {target}" in result.calls


def test_the_env_file_carries_the_pinned_tag_and_no_credential(installer, tmp_path) -> None:
    """`genus.env` is the only copy of a credential on disk — the wizard says so."""
    target = tmp_path / "genus"
    result = _install(installer, target, env={"OPENROUTER_API_KEY": "sk-not-a-real-key"})
    assert result.returncode == 0, result.output

    env_file = target / ".env"
    assert env_file.is_file()
    body = env_file.read_text(encoding="utf-8")
    assert f'GENUS_IMAGE_TAG="{PINNED}"' in body
    assert "sk-not-a-real-key" not in body, "the installer wrote a second copy of the key"
    for name in ("API_KEY", "PASSWORD", "SECRET", "TOKEN"):
        assert name not in body, f".env carries a {name} line"


def test_a_provider_key_with_shell_metacharacters_never_executes(installer, tmp_path) -> None:
    target = tmp_path / "genus"
    hostile = 'sk-a"b$(touch pwned)`touch pwned2`'
    result = _install(installer, target, env={"OPENROUTER_API_KEY": hostile})
    assert result.returncode == 0, result.output

    assert not (tmp_path / "pwned").exists()
    assert not (tmp_path / "pwned2").exists()
    assert hostile not in (target / ".env").read_text(encoding="utf-8")
    # It reached the wizard, which is the only thing that should store it, and
    # it reached it as one argument rather than as shell.
    assert "genus init" in result.calls


def test_a_credential_carrying_a_newline_aborts_before_anything_is_written(
    installer, tmp_path
) -> None:
    """A newline in a key is a mis-paste, and an env file cannot carry one.

    The refusal used to live inside a command substitution, where `die` exited
    only the subshell: the install ran to completion and reported success with
    the key silently dropped.
    """
    target = tmp_path / "genus"
    result = _install(
        installer,
        target,
        env={"OPENAI_API_KEY": "sk-line1\nGENUS_IMAGE_TAG=evil"},
    )
    assert result.returncode != 0, "a newline-bearing credential was accepted"
    assert "OPENAI_API_KEY" in result.stderr
    assert "newline" in result.stderr.lower()
    assert not target.exists(), "the install directory was created anyway"
    assert "genus init" not in result.calls


def test_the_env_file_is_written_without_a_quoting_round_trip(installer, tmp_path) -> None:
    """The one value in `.env` is a tag this script validated, so the file has
    no reason to need a parser more forgiving than compose's own."""
    target = tmp_path / "genus"
    result = _install(installer, target)
    assert result.returncode == 0, result.output
    lines = [
        line
        for line in (target / ".env").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert lines == [f'GENUS_IMAGE_TAG="{PINNED}"']


def test_no_provider_key_is_named_rather_than_prompted_for(installer, tmp_path) -> None:
    target = tmp_path / "genus"
    result = _install(installer, target)
    assert result.returncode == 0, result.output
    assert "OPENROUTER_API_KEY" in result.output, "the expected variable names were never printed"


def test_yes_without_an_owner_is_refused_before_anything_is_written(installer, tmp_path) -> None:
    target = tmp_path / "genus"
    result = installer("--substrate", "compose", "--version", PINNED, "--dir", str(target), "--yes")
    assert result.returncode != 0
    assert "--owner-name" in result.stderr
    assert not target.exists()


def test_a_tag_that_does_not_exist_fails_and_leaves_no_directory(installer, tmp_path) -> None:
    target = tmp_path / "genus"
    result = _install(installer, target, env={"SHIM_CURL_MODE": "download-fails"})
    assert result.returncode != 0
    assert "docker-compose.yml" in result.stderr
    assert "genus init" not in result.calls, "the wizard ran on a failed download"
    assert not target.exists(), "a failed download left an empty install directory behind"


def test_compose_files_that_do_not_parse_are_named(installer, tmp_path) -> None:
    """Docker's own stderr plus exit 15 names the file and not the decision."""
    target = tmp_path / "genus"
    result = _install(
        installer,
        target,
        shims={
            "docker": (
                "#!/usr/bin/env bash\n"
                'printf \'docker %s\\n\' "$*" >> "$SHIM_LOG"\n'
                'if [ "${1:-}" = "compose" ] && [ "${2:-}" = "version" ]; then\n'
                '  echo "Docker Compose version v2.29.7"; exit 0\n'
                "fi\n"
                'case "$*" in *config*) echo "yaml: line 3: mapping values" >&2; exit 15 ;; esac\n'
                "exit 0\n"
            )
        },
    )
    assert result.returncode != 0
    assert "parse" in result.stderr.lower() or "compose" in result.stderr.lower()
    assert "genus init" not in result.calls
    assert not target.exists(), "an unparseable download left an install directory behind"


def test_pypi_without_the_release_falls_back_to_the_git_tag(installer, tmp_path) -> None:
    target = tmp_path / "genus"
    result = _install(installer, target, env={"SHIM_PYPI_MISSING": "1"})
    assert result.returncode == 0, result.output
    assert f"git+https://github.com/Ironsail-llc/genus-os@{PINNED}" in result.calls
    assert "pypi" in result.output.lower(), "the fallback was silent"


def test_a_compose_install_writes_only_inside_the_target_directory(installer, tmp_path) -> None:
    target = tmp_path / "genus"
    result = _install(installer, target)
    assert result.returncode == 0, result.output
    stray = {
        entry
        for entry in _tree(result.home)
        if not entry.startswith(".cache")  # nothing the installer itself writes
    }
    assert stray == set(), f"the installer wrote into HOME: {sorted(stray)}"


def test_missing_docker_is_named_rather_than_assumed(installer, tmp_path) -> None:
    target = tmp_path / "genus"
    result = _install(
        installer,
        target,
        shims={"docker": "#!/usr/bin/env bash\nexit 127\n"},
    )
    assert result.returncode != 0
    assert "docker" in result.stderr.lower()
    assert "compose" in result.stderr.lower()


# --------------------------------------------------------------------------
# the gate that keeps the script honest
# --------------------------------------------------------------------------


def test_ci_runs_shellcheck_over_the_shell_scripts() -> None:
    """A shell script nothing lints is a shell script nobody reviews twice."""
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "shellcheck" in ci, "CI does not lint the shell in scripts/"
    assert re.search(r"shellcheck[^\n]*scripts/\*\.sh", ci), (
        "the shellcheck step does not cover scripts/*.sh"
    )


def test_the_docs_site_publishes_the_script_it_documents() -> None:
    """The one-liner's URL must actually serve this file, not a stale copy."""
    docs_workflow = (REPO_ROOT / ".github" / "workflows" / "docs.yml").read_text(encoding="utf-8")
    assert re.search(r"cp\s+scripts/install\.sh\s+docs/install\.sh", docs_workflow), (
        "the docs build does not copy scripts/install.sh into the site"
    )
    assert "scripts/install.sh" in docs_workflow.split("jobs:")[0], (
        "a change to the installer does not rebuild the site"
    )

    mkdocs = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert "!/install.sh" in mkdocs, "the site allowlist strips install.sh before publishing"

    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "docs/install.sh" in ignored, (
        "docs/install.sh is a build artifact; a committed copy would drift from scripts/"
    )


def test_the_quickstart_documents_the_one_liner() -> None:
    quickstart = (REPO_ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8")
    assert "https://ironsail-llc.github.io/genus-os/install.sh" in quickstart
    assert "--substrate compose" in quickstart
    assert "<!-- install-gate: install-sh -->" in quickstart, (
        "the one-liner is documented but nothing replays it"
    )


def test_the_install_gate_replays_the_one_liner_block() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "install-gate.yml").read_text(
        encoding="utf-8"
    )
    assert "--block install-sh" in workflow, "the gate never extracts the install-sh block"
    assert "scripts/install.sh" in workflow.split("jobs:")[0], (
        "a change to the installer does not run the gate on its own pull request"
    )
    # The version the gate installs is the pull request's own, through the
    # documented environment variable rather than an edited command.
    assert "GENUS_VERSION" in workflow
