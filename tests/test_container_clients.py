"""Optional contract tests against pinned clients; Docker itself is a stub.

Set AMD_CI_TEST_DOCKER_PLUGIN to a Docker plugin v5.14.0 checkout and
AMD_CI_TEST_ACTIONS_HOOK to the built official hook described in shared/README.md.
"""

import json
import os
from pathlib import Path
import subprocess
import unittest

import test_gpu_lease as fixtures
from container_policy import buildkite_env


class ContainerClientsTest(unittest.TestCase):
    setUp = fixtures.LeaseTest.setUp
    acquire = fixtures.LeaseTest.acquire

    def docker_stub(self):
        log = self.root / "docker.jsonl"
        docker = self.root / "docker"
        docker.write_text(
            "#!/usr/bin/python3\nimport json,sys\n"
            f"with open({str(log)!r},'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
            "if sys.argv[1]=='create': print('owned-container')\n"
            "if sys.argv[1:3]==['network','create']: print('owned-network')\n"
        )
        docker.chmod(0o755)
        return log, dict(os.environ, PATH=str(self.root) + ":" + os.environ["PATH"])

    @unittest.skipUnless(
        os.environ.get("AMD_CI_TEST_DOCKER_PLUGIN"), "pinned Docker plugin not supplied"
    )
    def test_real_buildkite_plugin_arguments(self):
        grant = self.acquire(2).grant
        log, env = self.docker_stub()
        env.update(
            {
                "AMD_CI_GPU_COUNT": "2",
                "BUILDKITE_PLUGINS": '[{"docker#v5.14.0":{}}]',
                "BUILDKITE_JOB_ID": "12345678-1234-1234-1234-123456789012",
                "BUILDKITE_COMMAND": "echo test",
                "BUILDKITE_PLUGIN_DOCKER_IMAGE": "test-image",
                "BUILDKITE_PLUGIN_DOCKER_RUN_LABELS": "false",
                "BUILDKITE_PLUGIN_DOCKER_TTY": "false",
                "BUILDKITE_PLUGIN_DOCKER_ENVIRONMENT_0": "AMD_CI_GPU_LEASE_ID",
            }
        )
        env.update(buildkite_env(grant, env))
        result = subprocess.run(
            ["bash", str(Path(env["AMD_CI_TEST_DOCKER_PLUGIN"]) / "hooks/command")],
            env=env,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = json.loads(log.read_text().splitlines()[-1])
        self.assertEqual(args[0], "run")
        self.assertIn("/dev/dri/renderD129", args)
        self.assertNotIn("/dev/dri", args)
        self.assertIn("--cpus=4", args)
        self.assertIn(f"--memory={grant['memory_bytes']}", args)
        self.assertIn("com.buildkite.job-id=" + env["BUILDKITE_JOB_ID"], args)
        self.assertIn("AMD_CI_GPU_LEASE_ID", args)

    @unittest.skipUnless(
        os.environ.get("AMD_CI_TEST_ACTIONS_HOOK"),
        "built official Actions hook not supplied",
    )
    def test_real_actions_prepare_exec_cleanup(self):
        lease = self.acquire(1)
        log, env = self.docker_stub()
        env.update(
            AMD_CI_GPU_LEASE=str(lease.path),
            AMD_CI_DOCKER_HOOK=env["AMD_CI_TEST_ACTIONS_HOOK"],
            RUNNER_NAME="unique-test-runner",
            GITHUB_WORKSPACE=str(self.root),
        )
        response = self.root / "response.json"
        response.touch()
        wrapper = (
            Path(__file__).resolve().parents[1] / "github-actions/gpu-container-hook.js"
        )

        def call(message):
            result = subprocess.run(
                ["node", str(wrapper)],
                input=json.dumps(message),
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        call(
            {
                "command": "prepare_job",
                "responseFile": str(response),
                "args": {
                    "jobContainer": {
                        "image": "test-image",
                        "workingDirectory": "/__w",
                        "environmentVariables": {"AMD_CI_GPU_COUNT": "1"},
                        "systemMountVolumes": [],
                    }
                },
            }
        )
        prepared = json.loads(response.read_text())
        self.assertEqual(prepared["state"]["container"], "owned-container")
        self.assertIn("isAlpine", prepared)
        call(
            {
                "command": "run_script_step",
                "state": prepared["state"],
                "args": {
                    "entryPoint": "bash",
                    "entryPointArgs": ["-c", "true"],
                    "workingDirectory": "/__w",
                    "environmentVariables": {},
                },
            }
        )
        call({"command": "cleanup_job", "state": prepared["state"], "args": {}})
        commands = [json.loads(line) for line in log.read_text().splitlines()]
        create = next(args for args in commands if args[0] == "create")
        self.assertIn("--device=/dev/dri/renderD128", create)
        self.assertIn("--label=amd-ci-lease=" + lease.token, create)
        self.assertNotIn("--privileged", create)
        self.assertTrue(
            any(
                args[:2] == ["exec", "-i"] and "owned-container" in args
                for args in commands
            )
        )
        self.assertTrue(lease.path.with_suffix(".started").exists())


if __name__ == "__main__":
    unittest.main()
