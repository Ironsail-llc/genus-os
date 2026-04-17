#!/bin/bash
# Nightly goal-achievement sweep — writes one agent_reviews row per active agent.
#
# Runs `python -m robothor.engine.goals nightly-review` which:
#   1. Loads every manifest in docs/agents/
#   2. Computes per-agent goal achievement over the trailing 7 days
#   3. Writes a row into `agent_reviews` with reviewer_type='system'
#   4. Generates action items from the corrective-actions template library
#
# Intended cron schedule (operator's crontab):
#   30 1 * * *  /path/to/robothor/scripts/nightly-goal-review.sh
#
# Goes at 1:30 AM so it runs before Nightwatch's 3 AM invocation, letting
# improvement-analyst pick up fresh goal-breach data.

set -euo pipefail

WORKSPACE="${ROBOTHOR_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Source secrets (SOPS-decrypted at boot) if present.
if [[ -f /run/robothor/secrets.env ]]; then
    # shellcheck disable=SC1091
    source /run/robothor/secrets.env
fi

cd "$WORKSPACE"
exec "$WORKSPACE/venv/bin/python" -m robothor.engine.goals nightly-review 2>&1 | logger -t nightly-goal-review
