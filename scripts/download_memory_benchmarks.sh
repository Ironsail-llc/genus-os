#!/usr/bin/env bash
# Download the public long-memory benchmarks the memory eval can run.
#
# usage: scripts/download_memory_benchmarks.sh <target-dir> [longmemeval|locomo|all]
#
# The files are never committed: keep <target-dir> outside the repository.
# Licences: LongMemEval code MIT, data built from ShareGPT (Apache 2.0) and
# UltraChat (MIT); LoCoMo data CC BY-NC 4.0 (non-commercial: check it applies).
set -euo pipefail

target="${1:?usage: $0 <target-dir> [longmemeval|locomo|all]}"
which="${2:-all}"
mkdir -p "$target"

lme="https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main"
locomo="https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"

fetch() {
  local url="$1" out="$2"
  echo "downloading $url"
  curl -fsSL --retry 3 -o "$out.part" "$url"
  mv "$out.part" "$out"
}

if [[ "$which" == "longmemeval" || "$which" == "all" ]]; then
  fetch "$lme/longmemeval_s_cleaned.json" "$target/longmemeval_s_cleaned.json"
  fetch "$lme/longmemeval_oracle.json" "$target/longmemeval_oracle.json"
fi
if [[ "$which" == "locomo" || "$which" == "all" ]]; then
  fetch "$locomo" "$target/locomo10.json"
fi

echo "sha256 (state these with any result):"
for f in "$target"/*.json; do
  shasum -a 256 "$f"
done
