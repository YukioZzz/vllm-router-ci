#!/usr/bin/env bash

set -Eeuo pipefail

readonly DEFAULT_LOCK_PATH=/run/lock/amd-ci-gpu.lock

lock_path=${DEFAULT_LOCK_PATH}
if [[ ${1:-} == --lock-path ]]; then
  [[ $# -ge 3 ]] || {
    echo "ERROR: --lock-path requires a path" >&2
    exit 2
  }
  lock_path=$2
  shift 2
fi

if [[ $# -ne 4 ]]; then
  echo "usage: $0 [--lock-path PATH] OWNER_PID OWNER_STARTTIME READY_PATH WAIT_SECONDS" >&2
  exit 2
fi

readonly OWNER_PID=$1
readonly OWNER_STARTTIME=$2
readonly READY_PATH=$3
readonly WAIT_SECONDS=$4

[[ ${OWNER_PID} =~ ^[1-9][0-9]*$ ]] || {
  echo "ERROR: owner PID must be numeric" >&2
  exit 2
}
[[ ${OWNER_STARTTIME} =~ ^[1-9][0-9]*$ ]] || {
  echo "ERROR: owner start time must be numeric" >&2
  exit 2
}
[[ ${WAIT_SECONDS} =~ ^[0-9]+$ ]] || {
  echo "ERROR: wait time must be a non-negative integer" >&2
  exit 2
}

process_matches() {
  local stat_line stat_tail
  local -a fields

  [[ -r /proc/${OWNER_PID}/stat ]] || return 1
  stat_line=$(<"/proc/${OWNER_PID}/stat")
  stat_tail=${stat_line##*) }
  read -r -a fields <<<"${stat_tail}"
  [[ ${#fields[@]} -ge 20 && ${fields[19]} == "${OWNER_STARTTIME}" ]]
}

write_status() {
  local status=$1
  local temporary_path="${READY_PATH}.tmp.$$"

  printf '%s\n' "${status}" >"${temporary_path}"
  mv -f "${temporary_path}" "${READY_PATH}"
}

process_matches || {
  write_status owner-gone
  exit 1
}

exec 9>>"${lock_path}"
if ! flock -w "${WAIT_SECONDS}" 9; then
  write_status busy
  exit 75
fi

process_matches || {
  write_status owner-gone
  exit 1
}

# A killed shared supervisor leaves claims until its containers are inspected.
# Holding the exclusive lock means any remaining GPU claim is quarantined.
if compgen -G "${lock_path}.claims/*.claim" >/dev/null; then
  write_status quarantined
  exit 1
fi
write_status acquired

while process_matches; do
  sleep 2
done
