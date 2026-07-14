# Sandbox container for computer-use agents
# ARM64-native, includes Xvfb + Chromium + xdotool for desktop automation
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    xdotool \
    scrot \
    wmctrl \
    imagemagick \
    chromium-browser \
    openbox \
    fonts-liberation \
    # The `exec` tool routes into this container, so it must contain what the
    # agents actually run. Without these an `auto-agent` run — whose entire job
    # is executing the test suite — lands in a container with no python and no
    # git and fails on its first command.
    git \
    python3 \
    python3-venv \
    ca-certificates \
    curl \
    jq \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. The engine also passes --user <uid>:<gid> so container
# processes map to the engine user and files written to the mounted workspace
# come back host-owned rather than root-owned.
RUN useradd -m -s /bin/bash sandbox

# Where the engine mounts the workspace (SANDBOX_WORKDIR in sandbox.py).
RUN mkdir -p /workspace && chown sandbox:sandbox /workspace

# Lives outside the home dir: the engine mounts the workspace over the working
# directory, and the entrypoint must not be shadowed by a mount.
COPY entrypoint.sh /usr/local/bin/sandbox-entrypoint.sh
RUN chmod +x /usr/local/bin/sandbox-entrypoint.sh

USER sandbox
WORKDIR /workspace

EXPOSE 9222

ENTRYPOINT ["/usr/local/bin/sandbox-entrypoint.sh"]
