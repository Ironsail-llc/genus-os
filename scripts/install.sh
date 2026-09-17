#!/usr/bin/env bash
#
# Genus OS — the one-line installer.
#
#   curl -fsSL https://ironsail-llc.github.io/genus-os/install.sh | bash -s -- \
#     --substrate compose --yes --owner-name "Ada Lovelace" --owner-email ada@example.com
#
# What it does is exactly what docs/quickstart.md's install-gate blocks do, and
# no more: fetch the two compose files, put the CLI somewhere private, and hand
# over to `genus init`. The wizard owns every decision after that.
#
# The properties that make `curl … | bash` acceptable here, each of them a test
# in tests/test_install_sh.py:
#
#   * it refuses to run as root, and refuses to be interpreted by `sh`;
#   * it never sudos, never pipes anything into a shell, never evals;
#   * every download is pinned to a release TAG, never to `main`, so the bytes
#     a reader gets are the bytes a release shipped;
#   * a run whose stdin is not a terminal and that was not given --yes is a
#     PREVIEW: it prints the plan and writes nothing, so the bare one-liner
#     above cannot install anything by accident;
#   * no secret is ever read from stdin, and none is written: a provider key
#     found in the environment is handed to `genus init`, which puts it in
#     `genus.env` -- the instance's only copy on disk. The only thing this
#     script writes into the install directory is the image tag.

# POSIX prologue: this must still be able to say "use bash" when `sh` runs it,
# so nothing bash-only may appear above the guard.
if [ -z "${BASH_VERSION:-}" ]; then
  echo "genus install: this installer needs bash, not sh." >&2
  echo "  Run:  bash install.sh --help" >&2
  echo "  Or:   curl -fsSL <url>/install.sh | bash -s -- --help" >&2
  exit 1
fi

set -euo pipefail

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

# Rewritten by scripts/update-helm-values.sh on every release, so the published
# script defaults to the release it shipped with and the API call below is only
# a refresh. Keep the exact shape: the updater and its test both match it.
INSTALL_SH_DEFAULT_VERSION="v1.99.0"

REPO_SLUG="Ironsail-llc/genus-os"
RAW_BASE="https://raw.githubusercontent.com/${REPO_SLUG}"
RELEASES_API="https://api.github.com/repos/${REPO_SLUG}/releases/latest"
GIT_REMOTE="https://github.com/${REPO_SLUG}"

BASE_FILE="docker-compose.yml"
APPS_FILE="docker-compose.apps.yml"

# Seconds the releases API may take before the stamped default wins. A wedged
# metadata call must never be the reason an install hangs.
API_TIMEOUT_S=10

# Seconds one compose file download may take.
DOWNLOAD_TIMEOUT_S=60

# Compose v2 as a `docker` subcommand and `service_completed_successfully` both
# landed well before this; robothor/init/substrates/compose.py agrees.
MINIMUM_DOCKER_MAJOR=24

# A release tag, and nothing that could be a URL path, a flag or a shell word.
VERSION_RE='^v[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$'

# Provider credentials the wizard reads from the environment. Named, never
# prompted for: a secret typed into a pipe is a secret in a shell history.
PROVIDER_VARS=(
  OPENROUTER_API_KEY
  OPENAI_API_KEY
  ANTHROPIC_API_KEY
  GEMINI_API_KEY
  GROQ_API_KEY
)

# --------------------------------------------------------------------------
# options
# --------------------------------------------------------------------------

SUBSTRATE=""
VERSION="${GENUS_VERSION:-}"
DIR=""
OWNER_NAME="${GENUS_OWNER_NAME:-}"
OWNER_EMAIL="${GENUS_OWNER_EMAIL:-}"
ASSUME_YES=0
DRY_RUN=0

usage() {
  cat <<'USAGE'
Genus OS installer — one wizard, two substrates.

Usage: install.sh [options]

  --substrate compose|pipx   compose runs the whole stack in containers; pipx
                             installs the CLI and runs `genus init --substrate
                             local`. Default: compose when Docker and the
                             Compose v2 plugin are both present, else pipx.
  --version vX.Y.Z           the release to install. Default: $GENUS_VERSION,
                             else the latest GitHub release, else the version
                             this script shipped with.
  --dir DIR                  where the compose install lives (default: ~/genus).
  --owner-name NAME          operator identity for owner.yaml (or
                             $GENUS_OWNER_NAME). Required with --yes.
  --owner-email EMAIL        operator identity for owner.yaml (or
                             $GENUS_OWNER_EMAIL). Required with --yes.
  --yes                      run the plan. Without it, and with no terminal on
                             stdin, this prints the plan and writes nothing.
  --dry-run                  print the plan and every command, execute nothing.
  --help                     this text.

Provider credentials are read from the environment and never prompted for:
OPENROUTER_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY,
GROQ_API_KEY.

Nothing here runs as root, elevates, or pipes anything into a shell.
USAGE
}

die() {
  printf 'genus install: %s\n' "$1" >&2
  exit 1
}

warn() {
  printf 'genus install: %s\n' "$1" >&2
}

say() {
  printf '%s\n' "$1"
}

need_value() {
  [ "$2" -gt 1 ] || die "$1 needs a value"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --substrate) need_value "$1" "$#"; SUBSTRATE="$2"; shift 2 ;;
    --substrate=*) SUBSTRATE="${1#*=}"; shift ;;
    --version) need_value "$1" "$#"; VERSION="$2"; shift 2 ;;
    --version=*) VERSION="${1#*=}"; shift ;;
    --dir) need_value "$1" "$#"; DIR="$2"; shift 2 ;;
    --dir=*) DIR="${1#*=}"; shift ;;
    --owner-name) need_value "$1" "$#"; OWNER_NAME="$2"; shift 2 ;;
    --owner-name=*) OWNER_NAME="${1#*=}"; shift ;;
    --owner-email) need_value "$1" "$#"; OWNER_EMAIL="$2"; shift 2 ;;
    --owner-email=*) OWNER_EMAIL="${1#*=}"; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

# --------------------------------------------------------------------------
# refusals, before anything is read or written
# --------------------------------------------------------------------------

if [ "$(id -u)" = "0" ]; then
  die "refuses to run as root. The containers run as the installing account's uid so they can read the workspace; a root install leaves files nothing else can open. Run it as the account that will own the instance."
fi

case "${SUBSTRATE:-}" in
  ""|compose|pipx) ;;
  *) die "unknown substrate: ${SUBSTRATE} (compose or pipx)" ;;
esac

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

# Print a command the way a reader could retype it, then run it — unless this
# is a plan, in which case printing IS the work.
run() {
  local rendered
  rendered="$(printf ' %q' "$@")"
  printf '  $%s\n' "$rendered"
  if [ "$DRY_RUN" -eq 1 ]; then
    return 0
  fi
  "$@"
}

# Same, for a command whose output this script consumes.
show() {
  local rendered
  rendered="$(printf ' %q' "$@")"
  printf '  $%s\n' "$rendered"
}

has_compose_v2() {
  command -v docker >/dev/null 2>&1 || return 1
  docker compose version >/dev/null 2>&1 || return 1
  return 0
}

docker_major() {
  local raw
  raw="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
  [ -n "$raw" ] || raw="$(docker --version 2>/dev/null || true)"
  printf '%s' "$raw" | sed -n 's/[^0-9]*\([0-9][0-9]*\).*/\1/p' | head -n1
}

# The latest release tag, or a non-zero exit.
#
# Bounded TWICE on purpose. `--max-time` asks curl to stop; `timeout` makes the
# bound hold whatever is on PATH under the name `curl`, because a metadata
# refresh that can block is the reason an install hangs. `timeout` is coreutils
# and is not on every macOS, so its absence falls back to the flag alone.
latest_release_tag() {
  local body tag
  if command -v timeout >/dev/null 2>&1; then
    body="$(timeout "$API_TIMEOUT_S" curl -fsSL --max-time "$API_TIMEOUT_S" \
      -H 'accept: application/vnd.github+json' "$RELEASES_API" 2>/dev/null)" || return 1
  else
    body="$(curl -fsSL --max-time "$API_TIMEOUT_S" \
      -H 'accept: application/vnd.github+json' "$RELEASES_API" 2>/dev/null)" || return 1
  fi
  tag="$(printf '%s' "$body" | tr ',' '\n' \
    | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)"
  [ -n "$tag" ] || return 1
  printf '%s' "$tag"
}

# A value quoted so compose's dotenv parser and a POSIX shell both read it
# back unchanged, and neither runs anything: inside double quotes only \ " $
# and ` are special.
#
# It RETURNS 1 rather than calling `die`, because every call site is a command
# substitution and `die` there exits only the subshell — its status discarded,
# `set -e` none the wiser, and the value written as empty while the install
# reports success. That is exactly what this used to do.
env_quote() {
  local value="$1"
  case "$value" in
    *$'\n'*) return 1 ;;
  esac
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//\$/\\\$}"
  value="${value//\`/\\\`}"
  printf '"%s"' "$value"
}

# --------------------------------------------------------------------------
# resolve: substrate, version, mode, owner
# --------------------------------------------------------------------------

if [ -z "$SUBSTRATE" ]; then
  if has_compose_v2; then SUBSTRATE="compose"; else SUBSTRATE="pipx"; fi
fi

VERSION_SOURCE=""
if [ -n "$VERSION" ]; then
  VERSION_SOURCE="requested"
else
  if VERSION="$(latest_release_tag)"; then
    VERSION_SOURCE="the GitHub releases API"
  else
    VERSION=""
    warn "the GitHub releases API did not answer within ${API_TIMEOUT_S}s"
    if [ -z "$INSTALL_SH_DEFAULT_VERSION" ]; then
      die "and this copy of the script carries no default version — pass --version vX.Y.Z"
    fi
    VERSION="$INSTALL_SH_DEFAULT_VERSION"
    VERSION_SOURCE="the version this script shipped with"
  fi
fi

if ! [[ "$VERSION" =~ $VERSION_RE ]]; then
  die "not a release version: ${VERSION} — expected a release tag of the form vMAJOR.MINOR.PATCH"
fi
PEP440_VERSION="${VERSION#v}"

# A preview is the honest answer to `curl … | bash` with no --yes: there is no
# terminal to confirm at, so nothing may be written.
PREVIEW=0
if [ "$DRY_RUN" -eq 1 ]; then
  PREVIEW=1
elif [ "$ASSUME_YES" -eq 0 ]; then
  if [ -t 0 ]; then
    PREVIEW=0
  else
    PREVIEW=1
  fi
fi
[ "$PREVIEW" -eq 1 ] && DRY_RUN=1

if [ -z "$DIR" ]; then
  DIR="${HOME}/genus"
fi

if [ "$ASSUME_YES" -eq 1 ]; then
  [ -n "$OWNER_NAME" ] || die "--yes needs an operator identity: pass --owner-name (or set GENUS_OWNER_NAME)"
  [ -n "$OWNER_EMAIL" ] || die "--yes needs an operator identity: pass --owner-email (or set GENUS_OWNER_EMAIL)"
fi

FOUND_PROVIDER_VARS=()
for name in "${PROVIDER_VARS[@]}"; do
  if [ -n "${!name:-}" ]; then
    FOUND_PROVIDER_VARS+=("$name")
  fi
done

# A newline in a credential is a paste that picked up a line break, and no env
# file can carry one. Refused HERE, at the top level, before a directory
# exists: the wizard would otherwise be handed a key that cannot work, and the
# failure would surface as a provider error three minutes later.
for name in ${FOUND_PROVIDER_VARS[@]+"${FOUND_PROVIDER_VARS[@]}"}; do
  case "${!name}" in
    *$'\n'*)
      die "${name} contains a newline — it was probably pasted with a line break. Fix the variable and run again."
      ;;
  esac
done

# --------------------------------------------------------------------------
# the plan — printed before anything happens, every time
# --------------------------------------------------------------------------

say ""
say "Genus OS installer"
say "  version     ${VERSION}  (${VERSION_SOURCE})"
say "  substrate   ${SUBSTRATE}"
if [ "$SUBSTRATE" = "compose" ]; then
  say "  directory   ${DIR}"
fi
if [ "${#FOUND_PROVIDER_VARS[@]}" -gt 0 ]; then
  say "  provider    ${FOUND_PROVIDER_VARS[*]+${FOUND_PROVIDER_VARS[*]}} (from the environment)"
else
  say "  provider    none in the environment — the wizard expects one of:"
  say "              ${PROVIDER_VARS[*]}"
fi
say ""

if [ "$SUBSTRATE" = "compose" ]; then
  say "Plan:"
  say "  1. check Docker ${MINIMUM_DOCKER_MAJOR}+ and the Compose v2 plugin"
  say "  2. create ${DIR}"
  say "  3. download ${BASE_FILE} and ${APPS_FILE}, pinned to ${VERSION}"
  say "  4. parse both compose files before anything is created"
  say "  5. write ${DIR}/.env — the image tag, and deliberately no credential"
  say "  6. install the CLI into ${DIR}/.venv"
  say "  7. genus init --substrate compose --yes --workspace ${DIR}"
  say "  8. genus doctor --json, then print the first-run URL"
else
  say "Plan:"
  say "  1. check pipx"
  say "  2. pipx install genusos==${PEP440_VERSION}"
  say "  3. genus init --substrate local --yes"
  say "  4. genus doctor --json, then print the first-run URL"
fi
say ""

# An interactive run that was not given --yes asks once, here, with the plan
# still on screen. A piped run never reaches this: it is a preview.
if [ "$PREVIEW" -eq 0 ] && [ "$ASSUME_YES" -eq 0 ]; then
  printf 'Proceed? [y/N] '
  read -r reply
  case "$reply" in
    y|Y|yes|YES) ;;
    *) die "nothing was done" ;;
  esac
  if [ -z "$OWNER_NAME" ]; then
    printf "Operator's full name: "
    read -r OWNER_NAME
  fi
  if [ -z "$OWNER_EMAIL" ]; then
    printf "Operator's email: "
    read -r OWNER_EMAIL
  fi
  if [ -z "$OWNER_NAME" ] || [ -z "$OWNER_EMAIL" ]; then
    die "an operator name and email are required"
  fi
fi

# --------------------------------------------------------------------------
# compose
# --------------------------------------------------------------------------

install_compose() {
  if ! has_compose_v2; then
    die "needs Docker with the Compose v2 plugin. Install Docker Engine ${MINIMUM_DOCKER_MAJOR}+ and try again; \`docker compose version\` must answer."
  fi
  local major
  major="$(docker_major)"
  if [ -n "$major" ] && [ "$major" -lt "$MINIMUM_DOCKER_MAJOR" ] 2>/dev/null; then
    die "Docker ${major} is older than the ${MINIMUM_DOCKER_MAJOR}+ the stack needs"
  fi

  # Everything is fetched and proved in a staging directory first, so a tag
  # that does not exist, or a compose file this Docker cannot read, leaves no
  # half-made install directory behind for the next run to resume into.
  local staging
  if [ "$DRY_RUN" -eq 0 ]; then
    staging="$(mktemp -d "${TMPDIR:-/tmp}/genus-install.XXXXXX")"
    # shellcheck disable=SC2064  # expand $staging now: that is the point
    trap "rm -rf -- '$staging'" EXIT
  else
    staging="<a temporary directory>"
  fi

  local name
  for name in "$BASE_FILE" "$APPS_FILE"; do
    say "→ downloading ${name} pinned to ${VERSION}"
    if ! run curl -fsSL --max-time "$DOWNLOAD_TIMEOUT_S" \
      -o "${staging}/${name}" "${RAW_BASE}/${VERSION}/infra/${name}"; then
      die "could not download ${name} for ${VERSION} — does that release exist? Nothing was created."
    fi
  done

  # The tag pins the content, so there is nothing to checksum against; what is
  # worth proving is that what arrived is a compose file this Docker can read,
  # before a single directory or container exists. The variables the files
  # require are supplied on the command line rather than through --env-file, so
  # no parser but this script's own is in the path. ROBOTHOR_DB_PASSWORD is
  # minted by the wizard, so the check supplies a throwaway of its own.
  say "→ checking both compose files parse"
  if ! run env "GENUS_IMAGE_TAG=${VERSION}" \
    "ROBOTHOR_DB_PASSWORD=${ROBOTHOR_DB_PASSWORD:-install-sh-parse-check}" \
    docker compose -f "${staging}/${BASE_FILE}" -f "${staging}/${APPS_FILE}" config -q; then
    die "the compose files for ${VERSION} did not parse with this Docker (see the error above). Nothing was created."
  fi

  say "→ creating ${DIR}"
  run mkdir -p "$DIR"
  for name in "$BASE_FILE" "$APPS_FILE"; do
    run mv "${staging}/${name}" "${DIR}/${name}"
  done

  # The image tag, and NOTHING else. `genus.env`, which the wizard writes next,
  # is the instance's only copy of a credential on disk and says so in its own
  # header; a second copy here would make that false and would be read by
  # nothing — compose runs with `--env-file genus.env`. This file exists so a
  # bare `docker compose ps` in the directory can still resolve the tag.
  say "→ writing ${DIR}/.env (the image tag; no credential)"
  local quoted_tag
  if ! quoted_tag="$(env_quote "$VERSION")"; then
    die "the release tag ${VERSION} cannot be written to an env file"
  fi
  if [ "$DRY_RUN" -eq 0 ]; then
    {
      printf '# Written by the Genus OS installer, for docker compose run by hand in\n'
      printf '# this directory. Credentials live in genus.env, which the wizard writes\n'
      printf '# 0600 and which every container reads through env_file.\n'
      printf 'GENUS_IMAGE_TAG=%s\n' "$quoted_tag"
    } > "${DIR}/.env"
  else
    say "     GENUS_IMAGE_TAG=${quoted_tag}"
  fi

  if [ "${#FOUND_PROVIDER_VARS[@]}" -gt 0 ]; then
    say "→ the wizard will take the provider key from ${FOUND_PROVIDER_VARS[*]+${FOUND_PROVIDER_VARS[*]}} in this environment"
  fi

  say "→ installing the CLI into ${DIR}/.venv"
  run python3 -m venv "${DIR}/.venv"
  local pip="${DIR}/.venv/bin/pip"
  local genus="${DIR}/.venv/bin/genus"
  if ! run "$pip" install --disable-pip-version-check "genusos==${PEP440_VERSION}"; then
    warn "PyPI has no genusos==${PEP440_VERSION} — installing from the ${VERSION} git tag instead"
    run "$pip" install --disable-pip-version-check "git+${GIT_REMOTE}@${VERSION}"
  fi

  say "→ genus init --substrate compose"
  local init_json url
  local -a init_args
  init_args=(init --substrate compose --yes --json --workspace "$DIR")
  # `if`, not `A && B`: an AND-list whose left side fails is the one shape
  # `set -e` is documented to ignore, and relying on that for control flow is
  # how a guard stops guarding.
  if [ -n "$OWNER_NAME" ]; then init_args+=(--owner-name "$OWNER_NAME"); fi
  if [ -n "$OWNER_EMAIL" ]; then init_args+=(--owner-email "$OWNER_EMAIL"); fi
  show "$genus" "${init_args[@]}"
  if [ "$DRY_RUN" -eq 0 ]; then
    init_json="$("$genus" "${init_args[@]}")"
  else
    init_json=""
  fi

  say "→ genus doctor --json"
  if [ "$DRY_RUN" -eq 0 ]; then
    if ! ROBOTHOR_WORKSPACE="$DIR" "$genus" doctor --json; then
      warn "the doctor reported a required failure — read the report above before signing in"
    fi
  else
    show env "ROBOTHOR_WORKSPACE=${DIR}" "$genus" doctor --json
  fi

  url="$(printf '%s' "$init_json" | tr ',' '\n' \
    | sed -n 's/.*"first_run_url"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)"
  epilogue "$url"
}

# --------------------------------------------------------------------------
# pipx
# --------------------------------------------------------------------------

install_pipx() {
  if ! command -v pipx >/dev/null 2>&1; then
    die "needs pipx. Install it with \`python3 -m pip install --user pipx && python3 -m pipx ensurepath\`, or re-run with --substrate compose."
  fi

  say "→ installing genusos ${VERSION}"
  if ! run pipx install "genusos==${PEP440_VERSION}"; then
    warn "PyPI has no genusos==${PEP440_VERSION} — installing from the ${VERSION} git tag instead"
    run pipx install "git+${GIT_REMOTE}@${VERSION}"
  fi

  say "→ genus init --substrate local"
  local init_json url
  local -a init_args
  init_args=(init --substrate local --yes --json)
  # `if`, not `A && B`: an AND-list whose left side fails is the one shape
  # `set -e` is documented to ignore, and relying on that for control flow is
  # how a guard stops guarding.
  if [ -n "$OWNER_NAME" ]; then init_args+=(--owner-name "$OWNER_NAME"); fi
  if [ -n "$OWNER_EMAIL" ]; then init_args+=(--owner-email "$OWNER_EMAIL"); fi
  show genus "${init_args[@]}"
  if [ "$DRY_RUN" -eq 0 ]; then
    init_json="$(genus "${init_args[@]}")"
  else
    init_json=""
  fi

  say "→ genus doctor --json"
  if [ "$DRY_RUN" -eq 0 ]; then
    if ! genus doctor --json; then
      warn "the doctor reported a required failure — read the report above before signing in"
    fi
  else
    show genus doctor --json
  fi

  url="$(printf '%s' "$init_json" | tr ',' '\n' \
    | sed -n 's/.*"first_run_url"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)"
  epilogue "$url"
}

epilogue() {
  local url="$1"
  say ""
  if [ "$PREVIEW" -eq 1 ]; then
    say "Nothing was written: this was a preview."
    say "Run it for real by adding --yes and an operator identity:"
    say "  … | bash -s -- --substrate ${SUBSTRATE} --yes \\"
    say "        --owner-name \"Ada Lovelace\" --owner-email ada@example.com"
    return 0
  fi
  say "Done."
  if [ -n "$url" ]; then
    say ""
    say "  Open the setup wizard (the link works once, for 30 minutes):"
    say "    ${url}"
  else
    say "  The wizard printed no first-run URL; run \`genus auth setup-link\` to mint one."
  fi

  if [ "$SUBSTRATE" != "compose" ]; then
    return 0
  fi

  # Neither of these is guessable, and both are needed by the next command an
  # operator types. The CLI is in a private virtualenv that is not on PATH, and
  # the workspace is not the platform's default (~/robothor) — a `genus doctor`
  # without both answers about a different, empty instance.
  say ""
  say "  This install's CLI, and the workspace it serves:"
  say "    ${DIR}/.venv/bin/genus"
  say "    export ROBOTHOR_WORKSPACE=${DIR}"
  say "    export PATH=\"${DIR}/.venv/bin:\$PATH\""
  say ""
  say "  The stack, for logs, ps and restarts:"
  say "    docker compose --env-file ${DIR}/genus.env \\"
  say "      -f ${DIR}/${BASE_FILE} -f ${DIR}/${APPS_FILE} ps"
}

case "$SUBSTRATE" in
  compose) install_compose ;;
  pipx) install_pipx ;;
esac
