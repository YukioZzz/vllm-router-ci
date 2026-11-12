"""CPU tests use real Linux flock/proc/processes and temporary fake telemetry."""

import copy
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, call
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))
from gpu_lease import (
    Pool,
    cleanup_resources,
    fits_router_window,
    load_grant,
    lock,
    start_time,
)
from container_policy import buildkite_env, github_input


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = {
            "host_lock": str(self.root / "host.lock"),
            "model_cache": "/models",
            "cpus_per_gpu": 2,
            "memory_bytes_per_gpu": 1024**3,
            "max_lease_seconds": 5,
            "drain_seconds": 1,
            "gpus": [],
        }
        for n in range(4):
            telemetry = self.root / f"vram{n}"
            telemetry.write_text("0")
            self.config["gpus"].append(
                {
                    "device": f"/dev/dri/renderD{128 + n}",
                    "vram_path": str(telemetry),
                    "idle_bytes": 10,
                }
            )
        self.pool = Pool(self.config)

    def acquire(self, count):
        lease = self.pool.acquire(count)
        self.assertIsNotNone(lease)
        self.addCleanup(lambda: lease.release(True) if lease.descriptors else None)
        return lease

    def test_two_clients_get_disjoint_gpus_and_legacy_waits(self):
        first, second = self.acquire(1), self.acquire(2)
        self.assertFalse(set(first.grant["devices"]) & set(second.grant["devices"]))
        self.assertEqual(second.grant["cpus"], 4)
        self.assertIsNone(lock(self.config["host_lock"], fcntl.LOCK_EX))
        self.assertIsNone(self.pool.acquire(2))
        first.release(True)
        second.release(True)
        fd = lock(self.config["host_lock"], fcntl.LOCK_EX)
        self.assertIsNotNone(fd)
        os.close(fd)

    def test_legacy_excludes_all_shared_clients(self):
        fd = lock(self.config["host_lock"], fcntl.LOCK_EX)
        try:
            self.assertIsNone(self.pool.acquire(1))
        finally:
            os.close(fd)
        self.acquire(4)

    def test_partial_allocation_rollback(self):
        first = self.acquire(3)
        self.assertIsNone(self.pool.acquire(2))
        second = self.acquire(1)
        self.assertEqual(len(set(first.grant["devices"] + second.grant["devices"])), 4)

    def test_busy_and_missing_telemetry(self):
        Path(self.config["gpus"][0]["vram_path"]).write_text("999")
        lease = self.acquire(3)
        self.assertNotIn("/dev/dri/renderD128", lease.grant["devices"])
        lease.release(True)
        Path(self.config["gpus"][1]["vram_path"]).unlink()
        with self.assertRaises(OSError):
            self.pool.acquire(4)
        fd = lock(self.config["host_lock"], fcntl.LOCK_EX)
        self.assertIsNotNone(fd)
        os.close(fd)

    def test_quarantine_is_not_reclaimed_by_idle_memory(self):
        first = self.acquire(2)
        first.release(False)
        self.assertTrue(first.path.exists())
        second = self.acquire(2)
        self.assertFalse(set(first.grant["devices"]) & set(second.grant["devices"]))
        self.assertIsNone(self.pool.acquire(1))

    def test_invalid_requests_and_duplicate_inventory(self):
        for count in (0, -1, 5, True, "2"):
            with self.subTest(count=count), self.assertRaises(ValueError):
                self.pool.acquire(count)
        bad = copy.deepcopy(self.config)
        bad["gpus"].append(bad["gpus"][0])
        with self.assertRaises(ValueError):
            Pool(bad)

    def test_stale_owner_is_not_a_valid_grant(self):
        lease = self.acquire(1)
        grant = load_grant(lease.path)
        grant["owner_start"] = "wrong-starttime"
        lease.path.write_text(json.dumps(grant))
        with self.assertRaises(ValueError):
            load_grant(lease.path)

    def test_process_death_retains_claims(self):
        config = self.root / "pool.json"
        config.write_text(json.dumps(self.config))
        code = "from gpu_lease import Pool; import json,sys,time; p=Pool(json.load(open(sys.argv[1]))); l=p.acquire(2); print(l.path,flush=True); time.sleep(30)"
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(config)],
            env=dict(os.environ, PYTHONPATH=sys.path[0]),
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            path = Path(child.stdout.readline().strip())
            self.assertTrue(path.exists())
            child.kill()
            child.wait(timeout=5)
            self.assertIsNone(self.pool.acquire(3))
            self.acquire(2)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdout.close()

    def test_supervisor_cancellation_cleans_only_its_label(self):
        docker = self.root / "docker"
        log = self.root / "docker.log"
        docker.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {log}\nexit 0\n')
        docker.chmod(0o755)
        config = self.root / "pool.json"
        config.write_text(json.dumps(self.config))
        marker = self.root / "child.json"
        code = 'from gpu_lease import Pool,supervise; import json,sys; sys.exit(supervise(Pool(json.load(open(sys.argv[1]))),1,2,[sys.executable,"-c",sys.argv[3]],None,None))'
        child_code = f"import os,time,pathlib; pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)"
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(config), "unused", child_code],
            env=dict(
                os.environ,
                PYTHONPATH=sys.path[0],
                PATH=str(self.root) + ":" + os.environ["PATH"],
            ),
        )
        try:
            until = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < until:
                time.sleep(0.05)
            self.assertTrue(marker.exists())
            child.terminate()
            self.assertEqual(child.wait(timeout=12), 130)
            self.assertIsNone(start_time(int(marker.read_text())))
            self.assertRegex(
                log.read_text(), r"ps -aq --filter label=amd-ci-lease=[0-9a-f]{32}"
            )
            self.assertFalse(list(self.pool.state.glob("*.claim")))
            self.acquire(4)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_github_policy_scopes_devices_and_preserves_input(self):
        grant = self.acquire(2).grant
        message = {
            "command": "prepare_job",
            "args": {
                "jobContainer": {
                    "image": "rocm@sha256:example",
                    "createOptions": "",
                    "environmentVariables": {
                        "AMD_CI_GPU_COUNT": "2",
                        "HIP_VISIBLE_DEVICES": "7",
                    },
                    "systemMountVolumes": [
                        {
                            "sourceVolumePath": "/runner/_work",
                            "targetVolumePath": "/__w",
                        }
                    ],
                }
            },
        }
        transformed = github_input(message, grant)["args"]["container"]
        self.assertNotIn("HIP_VISIBLE_DEVICES", transformed["environmentVariables"])
        self.assertIn("--device=/dev/dri/renderD129", transformed["createOptions"])
        self.assertNotIn("--device=/dev/dri ", transformed["createOptions"])
        self.assertIn("--cpus=4", transformed["createOptions"])
        self.assertEqual(message["args"]["jobContainer"]["createOptions"], "")
        for key, value in (
            ("createOptions", "--privileged"),
            ("userMountVolumes", [{"sourceVolumePath": "/dev"}]),
            ("portMappings", {"80": "80"}),
        ):
            bad = copy.deepcopy(message)
            bad["args"]["jobContainer"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                github_input(bad, grant)
        message["args"]["services"] = [{"image": "redis"}]
        with self.assertRaises(ValueError):
            github_input(message, grant)

    def test_buildkite_plugin_contract(self):
        grant = self.acquire(2).grant
        env = {"AMD_CI_GPU_COUNT": "2", "BUILDKITE_PLUGINS": '[{"docker#v5.14.0": {}}]'}
        result = buildkite_env(grant, env)
        self.assertEqual(result["BUILDKITE_PLUGIN_DOCKER_DEVICES_0"], "/dev/kfd")
        self.assertEqual(
            result["BUILDKITE_PLUGIN_DOCKER_DEVICES_2"], "/dev/dri/renderD129"
        )
        for name in (
            "DEVICES_0",
            "PRIVILEGED",
            "NETWORK",
            "IPC",
            "PUBLISH_0",
            "VOLUMES_0",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                buildkite_env(
                    grant,
                    {
                        **env,
                        "AMD_CI_GPU_COUNT": "2",
                        "BUILDKITE_PLUGIN_DOCKER_" + name: "/dev",
                    },
                )
        with self.assertRaises(ValueError):
            buildkite_env(grant, {**env, "AMD_CI_GPU_COUNT": "1"})
        # Plugin variables arrive after the agent hook; inspect job JSON too.
        for key, value in (
            ("devices", ["/dev/dri"]),
            ("privileged", True),
            ("memory", "200g"),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                buildkite_env(
                    grant,
                    {
                        **env,
                        "BUILDKITE_PLUGINS": json.dumps(
                            [{"docker#v5.14.0": {key: value}}]
                        ),
                    },
                )

    def test_legacy_holder_rejects_quarantine(self):
        lease = self.acquire(1)
        lease.release(False)
        ready = self.root / "ready"
        script = Path(__file__).resolve().parents[1] / "shared/hold-host-lease.sh"
        result = subprocess.run(
            [
                "bash",
                str(script),
                "--lock-path",
                self.config["host_lock"],
                str(os.getpid()),
                start_time(os.getpid()),
                str(ready),
                "0",
            ],
            timeout=5,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(ready.read_text().strip(), "quarantined")
        remote = script.with_name("hold-remote-lease.sh")
        result = subprocess.run(
            ["bash", str(remote), "--lock-path", self.config["host_lock"]],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.strip(), "quarantined")

    def test_cleanup_failure_quarantines(self):
        docker = self.root / "docker"
        docker.write_text("#!/bin/sh\nexit 1\n")
        docker.chmod(0o755)
        code = 'from gpu_lease import Pool,supervise; import json,sys; sys.exit(supervise(Pool(json.loads(sys.argv[1])),1,2,["true"],None,None))'
        result = subprocess.run(
            [sys.executable, "-c", code, json.dumps(self.config)],
            env=dict(
                os.environ,
                PYTHONPATH=sys.path[0],
                PATH=str(self.root) + ":" + os.environ["PATH"],
            ),
            timeout=8,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("quarantined", result.stderr)
        self.assertEqual(len(list(self.pool.state.glob("*.claim"))), 1)

    def test_cleanup_scopes_networks_to_the_lease(self):
        lease = self.acquire(1)
        with (
            patch(
                "gpu_lease.subprocess.check_output",
                side_effect=["owned-container\n", "owned-network\n"],
            ) as query,
            patch("gpu_lease.subprocess.run") as remove,
        ):
            cleanup_resources(lease)
        label = ("amd-ci-lease-" + lease.token).encode().hex()
        self.assertEqual(
            query.call_args_list,
            [
                call(
                    [
                        "docker",
                        "ps",
                        "-aq",
                        "--filter",
                        "label=amd-ci-lease=" + lease.token,
                    ],
                    text=True,
                    timeout=30,
                ),
                call(
                    ["docker", "network", "ls", "-q", "--filter", "label=" + label],
                    text=True,
                    timeout=30,
                ),
            ],
        )
        self.assertEqual(
            remove.call_args_list,
            [
                call(["docker", "rm", "-f", "owned-container"], check=True, timeout=60),
                call(
                    ["docker", "network", "rm", "owned-network"], check=True, timeout=30
                ),
            ],
        )

    def test_owner_exit_requires_completion_receipt(self):
        docker = self.root / "docker"
        docker.write_text("#!/bin/sh\nexit 0\n")
        docker.chmod(0o755)
        code = 'from gpu_lease import Pool,supervise; import json,sys; sys.exit(supervise(Pool(json.loads(sys.argv[1])),1,2,None,(int(sys.argv[2]),sys.argv[3]),sys.argv[4],buildkite_job_id="12345678-1234-1234-1234-123456789012"))'
        for finished in (True, False):
            with self.subTest(finished=finished):
                owner = subprocess.Popen(["sleep", "20"])
                ready = self.root / ("ready-" + str(finished))
                holder = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        code,
                        json.dumps(self.config),
                        str(owner.pid),
                        start_time(owner.pid),
                        str(ready),
                    ],
                    env=dict(
                        os.environ,
                        PYTHONPATH=sys.path[0],
                        PATH=str(self.root) + ":" + os.environ["PATH"],
                    ),
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    deadline = time.monotonic() + 4
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertTrue(ready.exists())
                    path = Path(json.loads(ready.read_text())["lease"])
                    self.assertEqual(
                        load_grant(path)["docker_label"],
                        "com.buildkite.job-id=12345678-1234-1234-1234-123456789012",
                    )
                    if finished:
                        path.with_suffix(".finished").touch()
                    owner.terminate()
                    owner.wait(timeout=3)
                    _, error = holder.communicate(timeout=5)
                    self.assertEqual(holder.returncode, 0 if finished else 1, error)
                    self.assertEqual(
                        len(list(self.pool.state.glob("*.claim"))), 0 if finished else 1
                    )
                finally:
                    for child in (owner, holder):
                        if child.poll() is None:
                            child.kill()
                            child.wait()
                    holder.stderr.close()

    def test_reserved_window_boundaries(self):
        def at(hour, minute=0):
            return datetime(2026, 11, 12, hour, minute, tzinfo=timezone.utc)

        self.assertTrue(fits_router_window(at(4), 3600))
        self.assertFalse(fits_router_window(at(4), 3601))
        self.assertFalse(fits_router_window(at(5), 1))
        self.assertTrue(fits_router_window(at(6, 30), 3600))
        self.assertFalse(fits_router_window(at(23), 7 * 3600))


if __name__ == "__main__":
    unittest.main()
