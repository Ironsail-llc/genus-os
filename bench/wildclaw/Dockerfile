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
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        unzip \
        jq \
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
RUN /opt/venv/bin/pip install --no-cache-dir Pillow numpy openai
