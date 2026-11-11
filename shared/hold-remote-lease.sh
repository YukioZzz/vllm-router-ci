#!/usr/bin/env bash

set -Eeuo pipefail

lock_path=/run/lock/amd-ci-gpu.lock
if [[ ${1:-} == --lock-path ]]; then
  [[ $# -eq 2 ]] || {
    echo "ERROR: --lock-path requires exactly one path" >&2
    exit 2
  }
  lock_path=$2
elif [[ $# -ne 0 ]]; then
  echo "usage: $0 [--lock-path PATH]" >&2
  exit 2
fi

exec 9>>"${lock_path}"
if ! flock -n 9; then
  echo busy
  exit 75
fi

if compgen -G "${lock_path}.claims/*.claim" >/dev/null; then
  echo quarantined
  exit 1
fi
echo acquired

# The coordinator keeps this SSH stream open for the lifetime of its Buildkite
# bootstrap. EOF, cancellation, or a broken connection releases fd 9.
cat >/dev/null
