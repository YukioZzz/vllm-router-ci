# vLLM Router ROCm CI

Private operations repository for the vLLM Router ROCm Buildkite runner on the
two-node DigitalOcean MI300X cluster.

The live CI topology is coordinator-only: Buildkite assigns work to node A,
and node A controls the node B vLLM containers over a dedicated RoCE rail. The
worker does not run a Buildkite agent.

| Role | Public management IP | Private management IP | RoCE addresses |
| --- | --- | --- | --- |
| Coordinator / agent | `165.245.139.69` | `10.128.0.17` | `192.168.200.1`, `.5`, `.9`, `.13`, `.17`, `.21`, `.25`, `.29` |
| Worker | `134.199.206.254` | `10.128.0.16` | `192.168.200.2`, `.6`, `.10`, `.14`, `.18`, `.22`, `.26`, `.30` |

Each host has eight MI300X GPUs and eight `mlx5` RoCE interfaces. Every rail
uses an MTU-9000 `/30` network.

## Live status

Infrastructure verified on 2026-08-10; live schedule updated on 2026-10-26:

- Buildkite queue: `router_rocm_mi300_2`
- Coordinator agent: `vllm-router-rocm-do-a`, enabled and active, `spawn=1`
- Worker agent: disabled and inactive
- Kernel on both hosts: `6.8.0-134-generic`, selected persistently through an
  exact GRUB menu entry
- Nightly schedule: 05:00 UTC daily on `main` (22:00 PDT or 21:00 PST on the
  preceding local calendar day)
- Reserved nightly window: 05:00-06:30 UTC; exact-head Buildkite build
  [#1020](https://buildkite.com/vllm/router/builds/1020) completed in about
  49 minutes
- Full manual schedule validation: [Buildkite build #642](https://buildkite.com/vllm/router/builds/642/table)
- Result: XGMI `read_mode=true/false` and two-node RDMA
  `read_mode=true/false` all passed

The RDMA modes each completed 500 GSM8K requests and measured `0.4780`
accuracy against the required `0.4100` threshold.

## Repository layout

The [shared GPU admission proposal](shared/README.md) adds an opt-in single-host
functional lane. It is not deployed by the existing installation scripts.

- `buildkite/`: agent configuration, exact-digest image restoration,
  Docker-plugin validation, shared-host lease, and per-job readiness hook
- `github-actions/` and `shared/`: symmetric SMG runner hooks and the host-local
  lease helpers used by both CI systems
- `netplan/`: the eight persistent jumbo-frame RoCE rails per host
- `kernel/`: persistent kernel selection and verification helpers
- `docs/operations.md`: installation, reboot, and maintenance runbook
- `docs/schedule.md`: Buildkite schedule and manual-run environment
- `docs/burn-in.md`: nightly alerting and seven-run acceptance criteria
- `docs/validation.md`: validated CI jobs and known unrelated test flake
- `docs/upstream.md`: upstream issue and pull-request map

## Safety

No Buildkite token, SSH private key, or Hugging Face credential belongs in this
repository. `buildkite/install-coordinator-agent.sh` accepts the registration
token through a protected file, environment variable, or standard input and
writes it only to `/etc/buildkite-agent/buildkite-agent.cfg` on the coordinator.

See [SECURITY.md](SECURITY.md) before provisioning or rotating credentials.
