#!/usr/bin/env bash
# Dedicated shared runner service entry point; never run as a workflow step.
set -Eeuo pipefail
[[ $# == 2 && $1 =~ ^[1-8]$ ]] || {
  echo 'usage: shared-gpu-runner.sh GPU_COUNT ABSOLUTE_RUNNER_ROOT' >&2
  exit 2
}
[[ $2 == /* && -x $2/bin/Runner.Listener ]]
: "${AMD_CI_DOCKER_HOOK:?pin and install the official Actions Docker hook first}"
[[ -f ${AMD_CI_DOCKER_HOOK} ]]
# Every registration needs a unique name, work root and 1/2/4-GPU-only label.
# No existing SMG mixed runner registration is reused or modified.
export ACTIONS_RUNNER_CONTAINER_HOOKS=/usr/local/lib/amd-ci/github-actions/gpu-container-hook.js
export ACTIONS_RUNNER_REQUIRE_JOB_CONTAINER=true
cd "$2"
exec python3 /usr/local/lib/amd-ci/shared/gpu_lease.py \
  --count "$1" --avoid-router-window --idle-seconds 900 \
  -- ./bin/Runner.Listener run --once
