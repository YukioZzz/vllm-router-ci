# Shared GPU admission (opt-in)

Single-host functional jobs can share GPUs. Existing paired RDMA and performance
lanes stay exclusive. Nothing installs or enables this path automatically.

```text
Buildkite bootstrap -> pre-command -> lease -> Docker plugin
Actions service     -> lease -> Listener --once -> container hook
                       |
          host lock SH + selected GPU locks EX
          legacy whole-host jobs take host lock EX
```

## Contract

`gpu_lease.py` is the only allocator. A root-owned inventory names individual
render devices and sysfs VRAM counters. Allocation never holds partial grants
while waiting. Containers receive `/dev/kfd`, only granted render nodes,
proportional CPU/RAM limits and private IPC/PID/network namespaces. Tests use
runtime-local device numbers. Runner names and workspaces must be unique.

All clients use the same host lock and inventory. Claims live under
`<host_lock>.claims`. Normal completion removes only job-labelled containers,
waits for the allocated GPU counters to return to measured idle baselines, then
removes claims. Cleanup uses `com.buildkite.job-id` for the Docker plugin and
`amd-ci-lease` for Actions. Buildkite also requires its pre-exit receipt before
bootstrap termination. PID disappearance or low VRAM alone is insufficient.

Missing telemetry, failed cleanup, interrupted bootstrap or supervisor SIGKILL
retains claims for operator inspection. There is no GPU reset or stale-claim
reaper. This is cooperative scheduling for trusted CI, not a security boundary
against jobs with Docker access. Fair queueing, multi-node sharing and shared
performance measurements are outside this change.

## Operator setup

1. Drain the host. Reconcile installed admission with this source: some SMG
   deployments use a separate rollout package. **Every exclusive path must
   reject remaining `*.claim` files after obtaining the host lock**, as the
   holder scripts here do. Updating this repo does not update that package.
2. Install `shared/`, `buildkite/` and `github-actions/` under
   `/usr/local/lib/amd-ci`, root-owned and not writable by jobs. Create root-owned
   mode-0644 `/etc/amd-ci/gpu-pool.json` from the example. Inventory the actual
   render-node mappings, sysfs counters and idle baselines; budget CPU/RAM with
   host headroom. The example is not a machine inventory. Use local files and
   one operator-managed group for lock/claim access. Never unlink live locks.
3. Use separate runner names/workdirs and new registrations. Do not inherit
   whole-host reset/cleanup hooks, privileged mode or host networking. Existing
   registrations retain their legacy labels.

### Buildkite

Create queue `router_rocm_mi300_shared`. Install `shared-gpu-pre-command` and
`shared-gpu-pre-exit` as its agent `pre-command` and `pre-exit` hooks. Leave
paired-agent hooks alone. Jobs use Docker plugin **v5.14.0**, declare
`AMD_CI_GPU_COUNT`, and omit device, privilege, IPC and resource overrides.
The hook validates `BUILDKITE_PLUGINS` because plugin variables are applied only
when the plugin hook runs. The existing plugin receives the assigned devices
and limits; its `environment` list must include `AMD_CI_GPU_COUNT` and
`AMD_CI_GPU_LEASE_ID`.

The resource wait is bounded at six hours; callers must preserve a separate
test execution timeout. Inventory lease duration must cover image startup,
tests, artifacts and teardown (example: 90 minutes). Holder logs stay in the
temporary directory printed by the hook, including after quarantine.

### Actions

Register a dedicated `1-gpu-mi300x-shared` runner (or the configured alternative)
without legacy labels. Launch through `shared-gpu-runner.sh 1 /absolute/runner`
from a service with `KillMode=control-group`. It acquires before
`Runner.Listener run --once`, before the job container exists. Each restart
must repeat admission; allocation in a workflow step would be too late.

Set `AMD_CI_DOCKER_HOOK` to the built, operator-owned Docker hook from
[`actions/runner-container-hooks`](https://github.com/actions/runner-container-hooks/tree/d4731a43a2fe1a4d4f7ea24dc515a1a120a065c8),
commit `d4731a43a2fe1a4d4f7ea24dc515a1a120a065c8`. Build hooklib then docker using
their lockfiles. The adapter translates the runner's `jobContainer` field to
the delegate's `container` field and supplies options from the grant. The
delegate retains container/network creation, execution and cleanup. Its prune
label derives from `RUNNER_NAME`; concurrent listeners must have unique names.

Jobs supply an image and `AMD_CI_GPU_COUNT`, without Docker options, ports or
user mounts. Inventory supplies `/models`. Docker actions and service containers
are rejected on this lane. An unassigned listener stops after 15 minutes. SMG
admission requires the entire lease plus cleanup to fit outside the existing
05:00–06:30 UTC Router reservation. Waiting listeners can reserve idle capacity;
a queue-aware dispatcher is outside this change.

## Qualification, quarantine and rollback

Before enabling caller flags, run one job from each CI concurrently. Check
disjoint devices, exact `torch.cuda.device_count()`, CPU/RAM limits and private
ports/workdirs. Cancel one; confirm the other survives. Check an exclusive job
waits and a killed supervisor's quarantine blocks exclusive admission. These
require a controlled maintenance window; CPU tests do not qualify live ROCm.

For quarantine: stop admission, obtain the exclusive host lock, inspect the
manifest, exact container label, processes and GPU telemetry. Clean only
confirmed owned leftovers. Remove only inspected claims/manifests after cleanup.
Do not clear claims based on an idle memory counter alone.

Rollback disables caller flags first, then drains shared services/registrations.
Keep claim guards until all quarantines are resolved; never delete lock files
or claims to force a queued job through.

## CPU tests

```bash
python3 -m unittest discover -s tests -p test_gpu_lease.py -v
./scripts/validate-repository.sh
./tests/test-ci-lease.sh
./tests/test-nightly-monitor.sh
```

Tests use real Linux locks/processes, temporary telemetry and a stub Docker CLI.
They allocate no GPUs and do not modify host admission.

To check the actual client argument/protocol boundary as well, put Node on PATH,
set `AMD_CI_TEST_DOCKER_PLUGIN` to a v5.14.0 plugin checkout and
`AMD_CI_TEST_ACTIONS_HOOK` to the pinned built Docker hook, then run:

```bash
python3 -m unittest discover -s tests -p test_container_clients.py -v
```

These tests execute the unmodified clients with a stub Docker CLI; they still
do not replace the live concurrent-container canary.
