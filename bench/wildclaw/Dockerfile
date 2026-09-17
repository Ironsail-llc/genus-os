# The image under test: production Genus plus the tools the tasks assume.
#
# WildClawBench ships a full toolchain in each harness's container — git,
# media tools, the usual CLI furniture — because its tasks are "in the wild"
# and take that environment for granted. Our production image is deliberately
# slim, so running the benchmark against it directly measures the Dockerfile,
# not the harness: on the first run the agent burned an entire task trying to
# install git from apt, apk, yum, conda, pip, and finally a static tarball.
#
# So the tooling is added HERE rather than in Dockerfile.python. The image
# that ships stays slim; the image that is measured is equivalent to what the
# other harnesses get. Nothing agent-specific goes in this layer — no task
# hints, no benchmark data, no configuration the fleet would not have.
FROM localhost/genus-bench:latest

USER root

# `~` in a warmup is $HOME, and two Code Intelligence warmups spell their
# interpreter `~/miniconda3/envs/eval/bin/pip`. Podman derives HOME from the
# image user's passwd entry, so this is already /root — pinned because the
# eval-environment contract below is written in terms of it, and a silent
# change of HOME would turn those two warmups back into `not found`.
ENV HOME=/root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        unzip \
        jq \
        # Six of the ten Productivity tasks declare `npm install -g
        # agent-browser` as their warmup. Without npm the container logged
        # `sh: 1: npm: not found`, the harness carried on, and those six ran
        # without the skill the benchmark hands every harness — six scores
        # that were not scores for this platform (measured 2026-09-16). The
        # warmup now fails the task loudly; this is what stops it failing.
        nodejs \
        npm \
        # Five Creative Synthesis warmups open with `apt-get update && apt-get
        # install -y ffmpeg`, and one Productivity warmup with `apt install
        # poppler-utils -y`. Both still run — they are the task's declared
        # warmup and we do not edit those — but pre-installing makes them a
        # no-op instead of a two-minute download charged to a 600s budget,
        # and survives an apt mirror that is having a bad day.
        ffmpeg \
        poppler-utils \
        # `opencv-python` (NOT the headless wheel) is what the two SAM3
        # warmups pin, and it dlopens libGL at import. Installing the wheel
        # would have succeeded and the first `import cv2` would have failed.
        # `libglib2.0-0t64` is the trixie (Debian 13) spelling — the base is
        # `python:3.12-slim`, which is trixie today. On an older base the
        # package is `libglib2.0-0`; apt names the miss loudly, and this layer
        # is built by hand rather than in CI, so a rename fails visibly.
        libgl1 \
        libglib2.0-0t64 \
    && rm -rf /var/lib/apt/lists/*

# git refuses to operate on a repository owned by another uid, which is
# exactly the shape of a bind-mounted workspace under rootless podman.
RUN git config --system --add safe.directory '*' \
    && git config --system user.email "agent@example.com" \
    && git config --system user.name "Agent"

# The production venv ships without pip (uv-built), so the bare `pip` on
# PATH belonged to the SYSTEM interpreter while `python` is the venv's —
# every task warmup and every agent `pip install X` installed into an
# interpreter nothing runs, and the first import failed anyway. jigsaw's
# grader died on `No module named 'PIL'` one line after a successful
# `pip install Pillow`. Give the venv its own pip so installs land where
# python looks.
RUN /opt/venv/bin/python -m ensurepip --default-pip 2>/dev/null \
    || (curl -sS https://bootstrap.pypa.io/get-pip.py | /opt/venv/bin/python)

# The toolchain the benchmark's harness images pre-install ("pinned Python
# toolchain, and pre-installed binaries" — WildClawBench report §3). Task
# graders import these without declaring them.
#
# `playwright` is here for its BINARY, not only its module: one Creative
# Synthesis warmup's second line is a bare `playwright install chromium`,
# which resolves only because its first line pip-installed the package. The
# image providing it means a warmup whose pip line is slow or rate-limited
# still reaches a working browser, and `command -v playwright` is true from a
# cold container — which is what the image contract test can actually check.
RUN /opt/venv/bin/pip install --no-cache-dir Pillow numpy openai playwright

# ─── the `eval` environment, at the path the tasks name ─────────────────────
#
# THE CONTRACT. Three of the twelve Code Intelligence tasks tell the agent its
# Python lives at `~/miniconda3/envs/eval` (task 6 spells it `/root/miniconda3
# /envs/eval` and calls it "the `eval` conda environment ... some common
# packages are pre-installed"). Two of them go further and declare it as their
# WARMUP:
#
#     ~/miniconda3/envs/eval/bin/pip install numpy==1.26.4 opencv-python==4.9.0.80
#
# That is the benchmark authors' own container contract — their reference
# images ship a miniconda with an `eval` env — and ours had no such path. The
# warmup exited 127 (`sh: pip: not found`). While warmups failed silently the
# agent simply ran anyway; since the warmup started failing loudly (#580) both
# tasks are SKIPPED and score 0 without ever running. Two tasks in the weakest
# category, lost to a missing directory.
#
# This is a venv, not a conda install. Miniconda would add ~600MB and a
# solver we have no use for; what the tasks actually exercise is
# `<prefix>/bin/pip` and `<prefix>/bin/python`, which a venv provides at the
# same path with the same semantics. `conda` itself is a shim (below), and
# `bench/wildclaw/README.md` says so — a fake that is not documented is the
# inert control this codebase keeps re-learning about.
#
# Python 3.12: the README does not state the authors' version, so this is the
# newest one BOTH pinned wheels publish (numpy 1.26.4 is cp39–cp312;
# opencv-python 4.9.0.80 is cp37-abi3). It is also the interpreter the rest of
# the image runs, so the agent does not have to reason about two.
# `/usr/local/bin/python3`, not the `python3` on PATH: that one is /opt/venv's,
# and an environment the tasks are told is separate should not be a venv of the
# platform's venv — the point of the `eval` prefix is that what the agent
# installs there cannot break the harness it is being measured by.
RUN /usr/local/bin/python3 -m venv /root/miniconda3/envs/eval \
    && /root/miniconda3/envs/eval/bin/pip install --no-cache-dir --upgrade pip \
    # The exact pins the two SAM3 warmups install. Pre-installed so the
    # warmup is a satisfied no-op rather than a network round-trip inside the
    # task's clock — and so it still succeeds if PyPI is unreachable.
    && /root/miniconda3/envs/eval/bin/pip install --no-cache-dir \
        numpy==1.26.4 \
        opencv-python==4.9.0.80 \
    # "some common packages are pre-installed" (task 6), kept to what the rest
    # of the corpus reaches for. Not a task hint: no task names these.
    && /root/miniconda3/envs/eval/bin/pip install --no-cache-dir \
        requests \
        Pillow \
        tqdm \
        pyyaml

# `conda` resolvable, and honest about what it is.
#
# No warmup runs `conda`, but three task prompts name the environment as a
# conda env, so an agent that reads them reaches for `conda activate eval` and
# gets `not found` — which reads as "this environment does not exist" rather
# than "use its interpreter directly". The shim answers `run` and the env
# listings for real, and for everything else exits non-zero naming the
# interpreter path; `bench/wildclaw/conda_shim.sh` is the whole of it, and
# says in its own header that it is a shim. A fake nobody documented is the
# inert control this codebase keeps re-learning about.
COPY bench/wildclaw/conda_shim.sh /root/miniconda3/bin/conda
RUN chmod 0755 /root/miniconda3/bin/conda \
    && ln -sf /root/miniconda3/bin/conda /usr/local/bin/conda

# ─── the gap we did not close by default ────────────────────────────────────
#
# The SAM3 codebase depends on `timm`, which depends on torch, and the two
# SAM3 warmups install only numpy and opencv — so the authors' `eval` env
# almost certainly carries torch already. Ours does not: a CPU torch is ~1GB
# on an image every one of the 60 tasks pulls, for two of them. Build with
# `--build-arg EVAL_TORCH=1` when measuring 02_Code_Intelligence; without it
# the agent can still `pip install torch` itself, at a few minutes of a 1200s
# budget. Named here rather than discovered in a transcript.
ARG EVAL_TORCH=0
RUN if [ "$EVAL_TORCH" = "1" ]; then \
        /root/miniconda3/envs/eval/bin/pip install --no-cache-dir \
            --index-url https://download.pytorch.org/whl/cpu torch torchvision \
        && /root/miniconda3/envs/eval/bin/pip install --no-cache-dir timm; \
    fi
