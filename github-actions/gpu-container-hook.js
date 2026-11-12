// Install outside checkouts; ACTIONS_RUNNER_CONTAINER_HOOKS points here.
// Reuse the pinned official Docker hook instead of reimplementing Actions exec.
const { spawnSync } = require('node:child_process');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');

const delegate = process.env.AMD_CI_DOCKER_HOOK;
if (!delegate || !process.env.AMD_CI_GPU_LEASE) {
  throw new Error('shared GPU admission and a pinned Docker hook are required');
}
const policy = spawnSync('python3', [resolve(__dirname, '../shared/container_policy.py'), 'github'], {
  input: readFileSync(0), encoding: 'utf8', maxBuffer: 8 * 1024 * 1024,
});
if (policy.status !== 0) {
  // Never print the hook payload: it can contain registry credentials.
  process.stderr.write('Shared GPU container policy rejected the job\n');
  process.exit(1);
}
// The delegate labels networks by RUNNER_NAME. Scope that internal identity to
// this lease, including when the real registration is reused for another job.
const lease = JSON.parse(readFileSync(process.env.AMD_CI_GPU_LEASE, 'utf8'));
const result = spawnSync(process.execPath, [delegate], {
  input: policy.stdout, stdio: ['pipe', 'inherit', 'inherit'],
  env: { ...process.env, RUNNER_NAME: `amd-ci-lease-${lease.id}` },
});
process.exit(result.status === null ? 1 : result.status);
