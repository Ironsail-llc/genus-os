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
    && rm -rf /var/lib/apt/lists/*

# Non-root user
RUN useradd -m -s /bin/bash sandbox
USER sandbox
WORKDIR /home/sandbox

COPY --chown=sandbox:sandbox entrypoint.sh /home/sandbox/entrypoint.sh
RUN chmod +x /home/sandbox/entrypoint.sh

EXPOSE 9222

ENTRYPOINT ["/home/sandbox/entrypoint.sh"]
