#!/usr/bin/env python3
"""Adapt one live grant to existing Docker clients; no allocation logic here."""

import copy
import json
import os
from pathlib import Path
import shlex
import sys

from gpu_lease import load_grant


def options(grant):
    return [
        "--device=/dev/kfd",
        *(f"--device={d}" for d in grant["devices"]),
        f"--label=amd-ci-lease={grant['id']}",
        f"--cpus={grant['cpus']}",
        f"--memory={grant['memory_bytes']}",
        f"--memory-swap={grant['memory_bytes']}",
        f"--shm-size={min(32 * 1024**3, grant['memory_bytes'] // 2)}",
        "--cap-add=IPC_LOCK",
        "--security-opt=seccomp=unconfined",
        "--ulimit=memlock=-1:-1",
    ]


def github_input(message, grant):
    """The native Docker hook still owns networking, exec, and container cleanup."""
    result = copy.deepcopy(message)
    command = result.get("command")
    if command in ("cleanup_job", "run_script_step"):
        return result
    if command not in ("prepare_job", "run_container_step"):
        raise ValueError("unsupported Actions container command")
    args = result["args"]
    if command == "run_container_step":
        # Docker actions are unnecessary for the initial shared E2E lane. Do
        # not silently grant GPUs or host mounts to an additional container.
        raise ValueError("Docker actions are not enabled on shared GPU runners")
    if args.get("services"):
        raise ValueError("service containers are not enabled on shared GPU runners")
    container = args.get("jobContainer")
    if not isinstance(container, dict) or not container.get("image"):
        raise ValueError("shared GPU runners require a job container")
    # The pinned official Docker hook still calls this field `container`.
    # Current Actions runners send `jobContainer`; adapt that one boundary.
    args["container"] = args.pop("jobContainer")
    if container.get("createOptions") or container.get("portMappings"):
        raise ValueError("shared job must not supply Docker options or published ports")
    env = container.setdefault("environmentVariables", {})
    if env.get("AMD_CI_GPU_COUNT") != str(len(grant["devices"])):
        raise ValueError("job GPU request does not match the pre-admission lease")
    # User mounts can defeat device/socket isolation. Cache provisioning is an
    # operator concern; only the runner's own system mounts are accepted here.
    if container.get("userMountVolumes"):
        raise ValueError("shared job must not supply host mounts")
    for mount in container.get("systemMountVolumes", []):
        target = Path(mount["targetVolumePath"])
        if not (
            target.parts[1:2] in [("__w",), ("__e",)]
            or str(target) in ("/github/home", "/github/workflow")
        ):
            raise ValueError("unexpected runner system mount")
    for key in ("CUDA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES"):
        env.pop(key, None)
    env["AMD_CI_GPU_LEASE_ID"] = grant["id"]
    env["HF_HOME"] = "/models"
    if "HF_TOKEN" in os.environ:
        env["HF_TOKEN"] = os.environ["HF_TOKEN"]
    container["userMountVolumes"] = [
        {
            "sourceVolumePath": grant["model_cache"],
            "targetVolumePath": "/models",
            "readOnly": False,
        }
    ]
    container["createOptions"] = " ".join(options(grant))
    return result


def buildkite_env(grant, env):
    prefix = "BUILDKITE_PLUGIN_DOCKER_"
    # Agent hooks do not receive a plugin's configuration variables. Those are
    # overlaid only when its own hook runs, so validate the original job JSON.
    plugins = json.loads(env.get("BUILDKITE_PLUGINS", "[]"))
    if len(plugins) != 1 or len(plugins[0]) != 1:
        raise ValueError("shared lane requires exactly the Docker plugin")
    name, config = next(iter(plugins[0].items()))
    if name not in (
        "docker#v5.14.0",
        "github.com/buildkite-plugins/docker-buildkite-plugin#v5.14.0",
    ):
        raise ValueError("shared lane requires Docker plugin v5.14.0")
    env = dict(env)
    for key, value in config.items():
        name = prefix + key.replace("-", "_").upper()
        if isinstance(value, list):
            env.update({f"{name}_{i}": str(v) for i, v in enumerate(value)})
        else:
            env[name] = str(value).lower() if isinstance(value, bool) else str(value)
    if env.get("AMD_CI_GPU_COUNT") != str(len(grant["devices"])):
        raise ValueError("Buildkite GPU request does not match its lease")
    # These values must be absent in the shared preset. Reject, don't quietly
    # sanitize a job that expected privileged or whole-host execution.
    forbidden = (
        "DEVICES",
        "GPUS",
        "PRIVILEGED",
        "IPC",
        "PID",
        "NETWORK",
        "PUBLISH",
        "ADD_CAPS",
        "LABELS",
        "VOLUMES_FROM",
        "CPUS",
        "MEMORY",
        "MEMORY_SWAP",
        "SHM_SIZE",
    )
    for name in env:
        if any(
            name == prefix + key or name.startswith(prefix + key + "_")
            for key in forbidden
        ):
            if env[name] not in ("", "false"):
                raise ValueError(f"shared preset must not set {name}")
    for name, value in env.items():
        if name.startswith(prefix + "VOLUMES"):
            source = value.split(":", 1)[0]
            if source in ("/", "/dev", "/run", "/var/run") or "docker.sock" in source:
                raise ValueError("unsafe shared container volume")
    result = {
        prefix + "CPUS": str(grant["cpus"]),
        prefix + "MEMORY": str(grant["memory_bytes"]),
        prefix + "MEMORY_SWAP": str(grant["memory_bytes"]),
        prefix + "SHM_SIZE": str(min(32 * 1024**3, grant["memory_bytes"] // 2)),
        prefix + "ADD_CAPS_0": "IPC_LOCK",
        "AMD_CI_GPU_LEASE_ID": grant["id"],
    }
    for index, device in enumerate(["/dev/kfd", *grant["devices"]]):
        result[prefix + f"DEVICES_{index}"] = device
    return result


def main():
    grant = load_grant(os.environ["AMD_CI_GPU_LEASE"])
    if sys.argv[1:] == ["github"]:
        message = github_input(json.load(sys.stdin), grant)
        if message["command"] == "prepare_job":
            Path(os.environ["AMD_CI_GPU_LEASE"]).with_suffix(".started").touch()
        json.dump(message, sys.stdout)
    elif sys.argv[1:] == ["buildkite"]:
        for key, value in buildkite_env(grant, os.environ).items():
            print(f"export {key}={shlex.quote(value)}")
    else:
        raise ValueError("expected github or buildkite")


if __name__ == "__main__":
    main()
