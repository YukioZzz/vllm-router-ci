#!/usr/bin/env bash

set -Eeuo pipefail

readonly LOCK_PATH=/run/lock/amd-ci-gpu.lock
readonly WORKER_SSH_HOST=vllm-router-rocm-worker
readonly REMOTE_HOLDER_PATH=/usr/local/sbin/amd-ci-hold-remote-lease
readonly RETRY_SECONDS=10

if [[ $# -ne 3 ]]; then
  echo "usage: $0 OWNER_PID OWNER_STARTTIME READY_PATH" >&2
  exit 2
fi

readonly OWNER_PID=$1
readonly OWNER_STARTTIME=$2
readonly READY_PATH=$3

[[ ${OWNER_PID} =~ ^[1-9][0-9]*$ ]] || exit 2
[[ ${OWNER_STARTTIME} =~ ^[1-9][0-9]*$ ]] || exit 2

remote_pid=
remote_read_fd=
remote_write_fd=

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

cleanup_remote() {
  if [[ -n ${remote_write_fd} ]]; then
    eval "exec ${remote_write_fd}>&-" 2>/dev/null || true
    remote_write_fd=
  fi
  if [[ -n ${remote_read_fd} ]]; then
    eval "exec ${remote_read_fd}<&-" 2>/dev/null || true
    remote_read_fd=
  fi
  if [[ -n ${remote_pid} ]]; then
    kill "${remote_pid}" 2>/dev/null || true
    wait "${remote_pid}" 2>/dev/null || true
    remote_pid=
  fi
}

cleanup() {
  cleanup_remote
}
trap cleanup EXIT
trap 'exit 130' TERM INT HUP

process_matches || {
  write_status owner-gone
  exit 1
}

exec 9>>"${LOCK_PATH}"

while process_matches; do
  if ! flock -n 9; then
    sleep "${RETRY_SECONDS}"
    continue
  fi
  if compgen -G "${LOCK_PATH}.claims/*.claim" >/dev/null; then
    write_status quarantined
    exit 1
  fi

  coproc REMOTE_LEASE {
    ssh -T -o BatchMode=yes -o ConnectTimeout=10 \
      "${WORKER_SSH_HOST}" "${REMOTE_HOLDER_PATH}"
  }
  remote_read_fd=${REMOTE_LEASE[0]}
  remote_write_fd=${REMOTE_LEASE[1]}
  remote_pid=${REMOTE_LEASE_PID}

  remote_status=
  if IFS= read -r -t 20 -u "${remote_read_fd}" remote_status \
      && [[ ${remote_status} == acquired ]]; then
    process_matches || {
      write_status owner-gone
      exit 1
    }
    write_status acquired
    while process_matches; do
      if ! kill -0 "${remote_pid}" 2>/dev/null; then
        wait "${remote_pid}" 2>/dev/null || true
        remote_pid=
        write_status lease-lost
        # Losing the worker lock while a two-node job is active is unsafe.
        # Terminating the bootstrap lets Buildkite clean up the job rather than
        # continuing after another owner can claim the worker.
        kill -TERM "${OWNER_PID}" 2>/dev/null || true
        exit 1
      fi
      sleep 2
    done
    exit 0
  fi

  cleanup_remote
  if [[ ${remote_status} == quarantined ]]; then
    write_status worker-quarantined
    exit 1
  fi
  flock -u 9
  sleep "${RETRY_SECONDS}"
done

write_status owner-gone
exit 1
