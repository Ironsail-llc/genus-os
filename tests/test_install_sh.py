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
    assert any("--max-time" in line for line in api_calls), "the API call has no timeout"
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


def test_the_env_file_is_private_and_carries_the_pinned_tag(installer, tmp_path) -> None:
    target = tmp_path / "genus"
    result = _install(installer, target)
    assert result.returncode == 0, result.output

    env_file = target / ".env"
    assert env_file.is_file()
    mode = stat.S_IMODE(env_file.stat().st_mode)
    assert mode == 0o600, f".env is mode {mode:o}, not 600"
    assert f'GENUS_IMAGE_TAG="{PINNED}"' in env_file.read_text(encoding="utf-8")


def test_a_provider_key_with_shell_metacharacters_is_written_inert(installer, tmp_path) -> None:
    target = tmp_path / "genus"
    hostile = 'sk-a"b$(touch pwned)`touch pwned2`'
    result = _install(installer, target, env={"OPENROUTER_API_KEY": hostile})
    assert result.returncode == 0, result.output

    assert not (tmp_path / "pwned").exists()
    assert not (tmp_path / "pwned2").exists()
    body = (target / ".env").read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=" in body
    # Round-tripping the file through a shell must reproduce the value exactly
    # and run nothing.
    probe = subprocess.run(  # noqa: S603
        ["bash", "-c", f'set -a; . "{target / ".env"}"; set +a; printf %s "$OPENROUTER_API_KEY"'],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        check=True,
    )
    assert probe.stdout == hostile
    assert not (tmp_path / "pwned").exists()


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


def test_a_tag_that_does_not_exist_fails_on_the_download(installer, tmp_path) -> None:
    target = tmp_path / "genus"
    result = _install(installer, target, env={"SHIM_CURL_MODE": "download-fails"})
    assert result.returncode != 0
    assert "docker-compose.yml" in result.stderr
    assert "genus init" not in result.calls, "the wizard ran on a failed download"


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
