"""The warmup's environment, checked before a sweep instead of after one.

A WildClawBench warmup is written against the benchmark authors' container and
declares none of what it assumes. Twice now that has cost a measurement: `npm`
(six Productivity tasks, which then ran without the skill every other harness
gets) and `~/miniconda3/envs/eval/bin/pip` (the two SAM3 tasks, which since
warmups began failing loudly are skipped and score 0 without running).

Both were one Dockerfile line. What was missing was anything that said so in
advance, which is these three tests:

* the corpus has no command head `warmup_env` does not account for — catches a
  new or re-worded task spec;
* every claim in `warmup_env` names something the Dockerfile actually installs
  — catches a list that drifts from the image it describes;
* `command -v` for each, inside the built image — the only one of the three
  that can tell you the image is right, which is why the first two are not
  enough and this one is marked `slow`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from bench.wildclaw import corpus, harness, warmup_env

REPO = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO / "bench" / "wildclaw" / "Dockerfile"


class TestTheCorpusAssumesNothingUndocumented:
    """Static, over the 60 specs. Skips without a checkout, like every other
    corpus-backed test here — a machine with no benchmark cannot lie about it."""

    @staticmethod
    def _tasks_dir() -> Path:
        tasks = corpus.tasks_dir()
        if tasks is None:
            pytest.skip("benchmark checkout not present")
        return tasks

    def test_every_declared_warmup_resolves(self):
        missing = warmup_env.unresolved(self._tasks_dir())
        assert not missing, (
            "warmup command heads with nothing in the image behind them: "
            + "; ".join(f"{head} ({', '.join(specs)})" for head, specs in sorted(missing.items()))
            + " — add the install to bench/wildclaw/Dockerfile and the entry to "
            "bench/wildclaw/warmup_env.py, or record it in KNOWN_GAPS"
        )

    def test_the_corpus_is_the_one_we_think_it_is(self):
        """A resolver that finds nothing to resolve passes vacuously. The suite
        is 60 tasks plus a template, and nearly every one declares a warmup."""
        tasks = self._tasks_dir()
        with_warmup = [
            spec
            for spec in tasks.rglob("*.md")
            if warmup_env.declared_warmup(spec.read_text(encoding="utf-8", errors="replace"))
        ]
        assert len(with_warmup) >= 40, f"only {len(with_warmup)} specs declared a warmup"

    def test_the_eval_environment_is_actually_asked_for(self):
        """The reason the venv exists. If the corpus stops naming that path,
        this test is the one that says the Dockerfile layer can go."""
        tasks = self._tasks_dir()
        askers = [
            spec.name
            for spec in tasks.rglob("*.md")
            if any(
                head.startswith(("~/miniconda3", warmup_env.EVAL_PREFIX))
                for head in warmup_env.command_heads(
                    warmup_env.declared_warmup(spec.read_text(encoding="utf-8", errors="replace"))
                )
            )
        ]
        assert len(askers) == 2, f"expected the two SAM3 tasks, got {askers}"


class TestTheListDescribesTheImage:
    """Runs anywhere, corpus or not: it reads the Dockerfile, not the tasks."""

    @staticmethod
    def _dockerfile() -> str:
        return DOCKERFILE.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        ("command", "token"),
        [
            ("npm", "npm"),
            ("node", "nodejs"),
            ("ffmpeg", "ffmpeg"),
            ("pdftotext", "poppler-utils"),
            ("git", "git"),
            ("jq", "jq"),
            ("curl", "curl"),
            ("unzip", "unzip"),
            ("playwright", "playwright"),
            ("conda", "conda_shim.sh"),
        ],
    )
    def test_the_dockerfile_installs_what_the_list_claims(self, command, token):
        assert command in warmup_env.IMAGE_BINARIES, f"{command} is not in the documented list"
        assert token in self._dockerfile(), f"{command} is claimed but {token!r} is not installed"

    def test_the_eval_prefix_is_created_at_the_path_the_tasks_name(self):
        """`~` is HOME, and HOME is pinned to /root in the same file — a venv
        at any other prefix satisfies nothing, however correct it is."""
        dockerfile = self._dockerfile()
        assert f"venv {warmup_env.EVAL_PREFIX}" in dockerfile
        assert "ENV HOME=/root" in dockerfile
        assert warmup_env.EVAL_PREFIX.startswith("/root/")

    def test_the_pins_the_sam3_warmups_install_are_already_there(self):
        """Pre-installed so the warmup is a satisfied no-op. It still RUNS —
        we do not edit a task's declared warmup — it just cannot fail on a
        slow mirror inside the task's own clock."""
        dockerfile = self._dockerfile()
        assert "numpy==1.26.4" in dockerfile
        assert "opencv-python==4.9.0.80" in dockerfile

    def test_opencv_s_shared_libraries_are_installed_too(self):
        """The wheel installs without them and `import cv2` is what fails —
        a warmup that exits 0 and a task that cannot start."""
        assert "libgl1" in self._dockerfile()

    def test_the_conda_shim_says_it_is_a_shim(self):
        """An undocumented fake is the inert control this codebase keeps
        re-learning about: it has to answer honestly when asked to do
        something it cannot."""
        shim = (REPO / "bench" / "wildclaw" / "conda_shim.sh").read_text(encoding="utf-8")
        assert "shim" in shim.lower()
        assert warmup_env.EVAL_PREFIX in shim

    def test_the_gaps_are_named_rather_than_silent(self):
        assert warmup_env.KNOWN_GAPS, "an empty gap list is a claim, not an absence"
        for gap, why in warmup_env.KNOWN_GAPS.items():
            assert len(why) > 40, f"{gap} is listed without saying what it costs"


class TestTheHeadParserFindsTheRealCommand:
    @pytest.mark.parametrize(
        ("warmup", "expected"),
        [
            ("npm install -g agent-browser", ["npm"]),
            ("apt-get update && apt-get install -y ffmpeg", ["apt-get", "apt-get"]),
            ("export SLACK_FIXTURES=/x.json && python3 /srv.py &", ["export", "python3"]),
            ("~/miniconda3/envs/eval/bin/pip install numpy", ["~/miniconda3/envs/eval/bin/pip"]),
            ("# a comment\n\npip install -q fastapi", ["pip"]),
            ("FOO=bar python3 x.py", ["python3"]),
            ("pip install -q fastapi uvicorn 2>/dev/null", ["pip"]),
        ],
        ids=[
            "plain",
            "chained",
            "assignment-then-background",
            "absolute-interpreter",
            "comments-dropped",
            "assignment-prefix",
            "redirected",
        ],
    )
    def test_heads(self, warmup, expected):
        assert warmup_env.command_heads(warmup) == expected

    def test_the_tilde_resolves_to_the_container_s_home(self):
        assert warmup_env.resolve("~/miniconda3/envs/eval/bin/pip") is not None
        assert warmup_env.resolve("~/miniconda3/envs/other/bin/pip") is None

    def test_an_unknown_command_is_not_quietly_accepted(self):
        assert warmup_env.resolve("yt-dlp") is None


@pytest.mark.slow
@pytest.mark.timeout(600)  # pytest.ini's global 30s is for unit tests, not podman
class TestTheBuiltImageAnswersForEachOne:
    """The only test here that can tell you the image is right.

    Everything above reads text: a Dockerfile line can be present and the
    package still absent under a renamed trixie spelling, which is exactly how
    `libglib2.0-0` would have got through. This runs `command -v` in the real
    image, one name at a time — `command -v` in dash answers only for its FIRST
    argument, so a single call with ten names is a test that checks one.

    Read-only, no network (`--network=none`), no benchmark data, and never the
    production tenant: `podman run --rm <image> sh -c 'command -v …'`. The
    image is `harness.IMAGE`, so `BENCH_IMAGE=…` points this at a candidate
    build before it becomes `:latest`.
    """

    @staticmethod
    def _image() -> str:
        if shutil.which("podman") is None:
            pytest.skip("podman not installed")
        image = harness.IMAGE
        exists = subprocess.run(  # noqa: S603 — fixed argv
            ["podman", "image", "exists", image], capture_output=True
        )
        if exists.returncode != 0:
            pytest.skip(f"bench image {image} is not built — see bench/wildclaw/README.md")
        return image

    def test_command_v_answers_for_every_documented_name(self):
        image = self._image()
        script = "\n".join(
            f'printf "%s\\t" {name!r}; command -v {name} || echo MISSING'
            for name in warmup_env.probe_commands()
        )
        proc = subprocess.run(  # noqa: S603 — fixed argv, our own generated script
            ["podman", "run", "--rm", "--network=none", image, "sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert proc.returncode == 0, proc.stderr
        missing = [
            line.split("\t")[0]
            for line in proc.stdout.splitlines()
            if line.endswith("MISSING") or "\tMISSING" in line
        ]
        assert not missing, (
            f"documented in warmup_env.py, absent from {image}: {missing}. "
            "Rebuild the tools image, or fix the Dockerfile line the list names."
        )

    def test_the_eval_interpreter_already_satisfies_the_sam3_pins(self):
        """The warmup would install these; the image pre-installs them so the
        line cannot fail on a mirror. If that ever stops being true the warmup
        still works — it just costs network inside the task's clock, which is
        worth knowing about here rather than in a transcript."""
        image = self._image()
        proc = subprocess.run(  # noqa: S603 — fixed argv
            [
                "podman",
                "run",
                "--rm",
                "--network=none",
                image,
                f"{warmup_env.EVAL_PREFIX}/bin/python",
                "-c",
                "import numpy, cv2; print(numpy.__version__, cv2.__version__)",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.split() == ["1.26.4", "4.9.0"], proc.stdout

    def test_the_two_sam3_warmups_run_clean_offline(self):
        """The literal line from the two SAM3 specs, with no network at all.
        Before this layer it exited 127; a pass here is the whole fix."""
        image = self._image()
        proc = subprocess.run(  # noqa: S603 — fixed argv
            [
                "podman",
                "run",
                "--rm",
                "--network=none",
                image,
                "sh",
                "-c",
                "~/miniconda3/envs/eval/bin/pip install numpy==1.26.4 opencv-python==4.9.0.80",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
