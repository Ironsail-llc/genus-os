#!/usr/bin/env bash
#
# Move the GitOps image tags onto a freshly released version, and land that
# on main even though main is moving.
#
# A release build takes tens of minutes: it cuts the tag, then builds and
# scans two images, and only then pushes the values bump. Every PR that
# merges in that window puts main ahead of the checkout this job is holding,
# so a single `git push origin HEAD:main` is a coin flip. On 2026-08-27 it
# came up tails — four PRs merged during the v1.57.0 build, the push was
# rejected non-fast-forward, and the promotion was simply dropped.
#
# The cost of dropping it is not one missed deploy. `Production release
# gate` fails on exactly that lag and it is the first job in the workflow,
# so every later job is skipped — including this one. The gate then blocks
# the only thing that can clear the condition it is gating on, and no PR can
# merge until a human pushes to main by hand.
#
# So the push retries, and it retries by RE-DERIVING rather than rebasing:
# reset onto the new main and recompute the bump from scratch. The promotion
# is a pure function of the version, so recomputing is always correct, can
# never conflict, and can never clobber the merge it raced.
set -euo pipefail

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
  echo "usage: promote-release.sh <version>" >&2
  exit 2
fi
VERSION="${VERSION#v}"

MAX_ATTEMPTS="${PROMOTE_MAX_ATTEMPTS:-5}"
BRANCH="${PROMOTE_BRANCH:-main}"

git config user.name "${PROMOTE_GIT_NAME:-github-actions[bot]}"
git config user.email "${PROMOTE_GIT_EMAIL:-github-actions[bot]@users.noreply.github.com}"

attempt=1
while :; do
  node scripts/promote-release-values.js "$VERSION"
  node scripts/check-version-consistency.js
  git diff --check
  git add helm/genus-os/values-production.yaml helm/genus-os/values-staging.yaml

  if git diff --staged --quiet; then
    if [ "$attempt" -eq 1 ]; then
      # Nothing to do on the FIRST attempt means the release output and the
      # values already disagree about what is being deployed. That was worth
      # refusing before the retry existed and it still is.
      echo "Release v${VERSION} is already promoted; refusing an ambiguous no-op." >&2
      exit 1
    fi
    # Nothing to do after a rejected push means the run that beat us to the
    # push also promoted. The deploy is correct; this is success.
    echo "Release v${VERSION} was already promoted by a concurrent run."
    exit 0
  fi

  git commit -q -m "chore(deploy): promote v${VERSION} [skip ci]"

  if git push -q origin "HEAD:${BRANCH}"; then
    echo "Promoted v${VERSION} on attempt ${attempt}."
    if [ -n "${GITHUB_OUTPUT:-}" ]; then
      echo "promotion_commit=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"
    fi
    exit 0
  fi

  if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
    echo "Push rejected ${MAX_ATTEMPTS} times; ${BRANCH} is moving faster than the" >&2
    echo "release can promote. Left unpromoted deliberately — forcing would" >&2
    echo "discard whatever landed instead." >&2
    exit 1
  fi

  echo "Push rejected — ${BRANCH} moved. Re-deriving on top of it (attempt ${attempt})."
  # FETCH_HEAD, not origin/<branch>: actions/checkout configures a narrow
  # refspec, so the remote-tracking ref is not guaranteed to exist or to be
  # updated by this fetch. FETCH_HEAD always names what we just fetched.
  git fetch -q origin "$BRANCH"
  git reset -q --hard FETCH_HEAD
  attempt=$((attempt + 1))
done
