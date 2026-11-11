#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

while IFS= read -r script; do
  bash -n "${script}"
done < <(find "${ROOT_DIR}" -type f \( -name '*.sh' -o -name '*pre-command' -o -name '*pre-exit' \) -print | sort)

if grep -RIl --exclude-dir=.git -E 'bkct_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+' "${ROOT_DIR}" >/dev/null; then
  echo "ERROR: a Buildkite agent token pattern is present" >&2
  exit 1
fi

echo "Repository shell syntax and secret-pattern checks passed"
