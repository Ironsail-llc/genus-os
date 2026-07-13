#!/usr/bin/env bash
set -euo pipefail

readonly VERSION="v3.4.2"
readonly SHA256="d4cb1ac8002baab8afaca2da3de597b613df8459074bc7c6d96dc95161c2a33f"
readonly DESTINATION="${1:?usage: install-argocd-cli.sh DESTINATION}"

temporary_file="$(mktemp)"
trap 'rm -f "$temporary_file"' EXIT

curl --fail --show-error --location \
  --output "$temporary_file" \
  "https://github.com/argoproj/argo-cd/releases/download/${VERSION}/argocd-linux-amd64"
echo "${SHA256}  ${temporary_file}" | sha256sum --check --strict
install -m 0755 "$temporary_file" "$DESTINATION"
