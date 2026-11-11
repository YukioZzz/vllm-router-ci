#!/usr/bin/env python3
"""Cooperative GPU leases for trusted CI runners; Linux, standard library only.

Shared jobs hold the existing host lock in shared mode and individual GPU locks
in exclusive mode. Legacy whole-host users continue to take the host lock
exclusively. A persistent claim survives supervisor SIGKILL: it is quarantined,
not reclaimed merely because a PID disappeared or VRAM happens to be idle.
"""

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid


def start_time(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        return None


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.write("\n")
    os.replace(temporary, path)


def lock(path, operation):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o660)
    try:
        fcntl.flock(fd, operation | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise


def validate_config(config):
    for key in (
        "cpus_per_gpu",
        "memory_bytes_per_gpu",
        "max_lease_seconds",
        "drain_seconds",
    ):
        if type(config.get(key)) is not int or config[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    for key in ("host_lock",):
        if not Path(config[key]).is_absolute():
            raise ValueError(f"{key} must be absolute")
    cache = Path(config["model_cache"])
    if (
        not cache.is_absolute()
        or cache == Path("/")
        or cache.parts[1] in ("dev", "proc", "sys", "etc", "run")
    ):
        raise ValueError("model_cache must be a dedicated absolute cache directory")
    devices = config.get("gpus", [])
    if not devices or len(devices) > 64:
        raise ValueError("inventory must contain 1..64 GPUs")
    names = []
    for device in devices:
        if not re.fullmatch(r"/dev/dri/renderD[0-9]+", device["device"]):
            raise ValueError("inventory must name individual render devices")
        if not Path(device["vram_path"]).is_absolute():
            raise ValueError("vram_path must be absolute")
        if type(device.get("idle_bytes")) is not int or device["idle_bytes"] < 0:
            raise ValueError("idle_bytes must be a nonnegative measured baseline")
        names.append(device["device"])
    if len(set(names)) != len(names):
        raise ValueError("duplicate GPU in inventory")


class Pool:
    def __init__(self, config):
        validate_config(config)
        self.config = config
        # Derived from the legacy lock so different clients/config files cannot
        # accidentally create independent allocation domains on the same host.
        self.state = Path(config["host_lock"] + ".claims")
        self.state.mkdir(mode=0o770, parents=True, exist_ok=True)

    def idle(self, gpu):
        # Missing telemetry is not an idle device.
        return 0 <= int(Path(gpu["vram_path"]).read_text()) <= gpu["idle_bytes"]

    def acquire(self, count):
        if type(count) is not int or not 1 <= count <= len(self.config["gpus"]):
            raise ValueError("GPU count is outside the configured inventory")
        host = lock(self.config["host_lock"], fcntl.LOCK_SH)
        if host is None:
            return None
        descriptors, selected = [host], []
        try:
            # Serialize selection, but never hold partial grants while waiting.
            allocation = lock(self.state / "allocation.lock", fcntl.LOCK_EX)
            if allocation is None:
                return None
            descriptors.append(allocation)
            for gpu in self.config["gpus"]:
                name = Path(gpu["device"]).name
                if (self.state / f"{name}.claim").exists():
                    continue
                fd = lock(self.state / f"{name}.lock", fcntl.LOCK_EX)
                if fd is None:
                    continue
                descriptors.append(fd)
                if not self.idle(gpu):
                    os.close(descriptors.pop())
                    continue
                selected.append(gpu)
                if len(selected) == count:
                    lease = Lease(self, selected, descriptors.copy())
                    # Publish claims while holding the allocation lock. On a
                    # partial write error, retain claims conservatively.
                    lease.publish()
                    os.close(allocation)
                    lease.descriptors.remove(allocation)
                    descriptors.clear()
                    return lease
            return None
        finally:
            for fd in descriptors:
                os.close(fd)


class Lease:
    def __init__(self, pool, gpus, descriptors):
        self.pool, self.gpus, self.descriptors = pool, gpus, descriptors
        self.token = uuid.uuid4().hex
        self.path = pool.state / f"{self.token}.json"
        self.claims = [pool.state / f"{Path(g['device']).name}.claim" for g in gpus]
        self.grant = {
            "version": 1,
            "id": self.token,
            "owner_pid": os.getpid(),
            "owner_start": start_time(os.getpid()),
            "devices": [g["device"] for g in gpus],
            "cpus": len(gpus) * pool.config["cpus_per_gpu"],
            "memory_bytes": len(gpus) * pool.config["memory_bytes_per_gpu"],
            "model_cache": pool.config["model_cache"],
            "docker_label": f"amd-ci-lease={self.token}",
        }

    def publish(self):
        atomic_json(self.path, self.grant)
        for claim in self.claims:
            # No replacement of another lease's claim, even if its PID is gone.
            with claim.open("x") as stream:
                stream.write(self.token + "\n")

    def release(self, clean):
        try:
            if clean:
                for claim in self.claims:
                    if claim.read_text().strip() != self.token:
                        raise RuntimeError("claim changed while lease was held")
                for claim in self.claims:
                    claim.unlink()
                self.path.unlink()
        finally:
            for fd in self.descriptors:
                os.close(fd)
            self.descriptors.clear()


def load_grant(path):
    grant = json.loads(Path(path).read_text())
    if grant.get("version") != 1 or not re.fullmatch(
        r"[0-9a-f]{32}", grant.get("id", "")
    ):
        raise ValueError("unsupported or malformed GPU lease")
    if (
        start_time(grant["owner_pid"]) != grant["owner_start"]
        or not grant["owner_start"]
    ):
        raise ValueError("GPU lease supervisor is no longer alive")
    devices = grant["devices"]
    if (
        not devices
        or len(set(devices)) != len(devices)
        or any(not re.fullmatch(r"/dev/dri/renderD[0-9]+", d) for d in devices)
    ):
        raise ValueError("invalid devices in GPU lease")
    for key in ("cpus", "memory_bytes"):
        if type(grant[key]) is not int or grant[key] <= 0:
            raise ValueError(f"invalid {key} in GPU lease")
    return grant


def cleanup_containers(lease):
    # Never prune by host, repository, shared runner name, or all containers.
    ids = subprocess.check_output(
        ["docker", "ps", "-aq", "--filter", f"label={lease.grant['docker_label']}"],
        text=True,
        timeout=30,
    ).split()
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], check=True, timeout=60)


def fits_router_window(now, seconds):
    """SMG's complete allocation must fit outside the daily Router reservation."""
    end = now + timedelta(seconds=seconds)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        if now < day + timedelta(hours=6, minutes=30) and end > day + timedelta(
            hours=5
        ):
            return False
        day += timedelta(days=1)
    return True


def terminate_group(child):
    try:
        os.killpg(child.pid, signal.SIGTERM)
        # wait() alone is insufficient when the leader exited before its children.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            child.poll()
            os.killpg(child.pid, 0)
            time.sleep(0.1)
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


def supervise(
    pool,
    count,
    wait_seconds,
    command,
    owner,
    ready,
    avoid_window=False,
    idle_seconds=0,
    buildkite_job_id=None,
):
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)

    def alive():
        return not stopping and (owner is None or start_time(owner[0]) == owner[1])

    if buildkite_job_id and not re.fullmatch(r"[0-9a-fA-F-]{36}", buildkite_job_id):
        raise ValueError("invalid Buildkite job ID")
    deadline = time.monotonic() + wait_seconds
    lease = None
    while alive() and time.monotonic() < deadline:
        if avoid_window and not fits_router_window(
            datetime.now(timezone.utc),
            pool.config["max_lease_seconds"] + pool.config["drain_seconds"] + 90,
        ):
            time.sleep(1)
            continue
        lease = pool.acquire(count)
        if lease:
            break
        time.sleep(0.2)
    if lease is None:
        return 130 if stopping else 75
    child = None
    rc, clean = 1, False
    try:
        if buildkite_job_id:
            # Docker plugin v5.14.0 supplies this label itself; it has no
            # arbitrary LABELS option. Reuse the actual plugin contract.
            lease.grant["docker_label"] = f"com.buildkite.job-id={buildkite_job_id}"
            atomic_json(lease.path, lease.grant)
        if ready:
            atomic_json(Path(ready), {"lease": str(lease.path)})
        env = dict(os.environ, AMD_CI_GPU_LEASE=str(lease.path))
        if command:
            child = subprocess.Popen(command, env=env, start_new_session=True)
        deadline = time.monotonic() + pool.config["max_lease_seconds"]
        idle_deadline = time.monotonic() + idle_seconds if idle_seconds else deadline
        while alive() and time.monotonic() < deadline:
            if child and child.poll() is not None:
                rc = child.returncode
                break
            if (
                time.monotonic() >= idle_deadline
                and not lease.path.with_suffix(".started").exists()
            ):
                rc = 124
                break
            time.sleep(0.2)
        else:
            rc = 130 if stopping else (0 if owner and not alive() else 124)
    finally:
        try:
            if child:
                terminate_group(child)
            # A bootstrap PID disappearing alone cannot prove that its plugin
            # children have finished. Require its pre-exit receipt as well.
            owner_finished = owner is None or (
                start_time(owner[0]) != owner[1]
                and lease.path.with_suffix(".finished").exists()
            )
            if owner and start_time(owner[0]) == owner[1]:
                try:
                    os.kill(owner[0], signal.SIGTERM)
                    until = time.monotonic() + 10
                    while start_time(owner[0]) == owner[1] and time.monotonic() < until:
                        time.sleep(0.1)
                    if start_time(owner[0]) == owner[1]:
                        os.kill(owner[0], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            cleanup_containers(lease)
            deadline = time.monotonic() + pool.config["drain_seconds"]
            while time.monotonic() < deadline:
                if all(pool.idle(gpu) for gpu in lease.gpus):
                    clean = owner_finished
                    break
                time.sleep(0.2)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f"GPU lease cleanup failed: {type(exc).__name__}", file=sys.stderr)
        finally:
            lease.release(clean)
            lease.path.with_suffix(".started").unlink(missing_ok=True)
            lease.path.with_suffix(".finished").unlink(missing_ok=True)
            if not clean:
                print(
                    f"GPU lease {lease.token} quarantined; operator cleanup required",
                    file=sys.stderr,
                )
    return rc if clean else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/amd-ci/gpu-pool.json")
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--wait-seconds", type=int, default=21600)
    parser.add_argument("--owner-pid", type=int)
    parser.add_argument("--owner-start")
    parser.add_argument("--ready")
    parser.add_argument("--avoid-router-window", action="store_true")
    parser.add_argument("--idle-seconds", type=int, default=0)
    parser.add_argument("--buildkite-job-id")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not 1 <= args.wait_seconds <= 21600 or bool(command) == bool(args.owner_pid):
        parser.error("provide a command OR an owner PID; wait must be 1..21600 seconds")
    if args.owner_pid and (not args.owner_start or not args.ready):
        parser.error("owner mode requires --owner-start and --ready")
    if not 0 <= args.idle_seconds <= 900:
        parser.error("idle-seconds must be 0..900")
    config_path = Path(args.config)
    if config_path.stat().st_uid != 0 or config_path.stat().st_mode & 0o022:
        parser.error("inventory must be root-owned and not group/world writable")
    os.umask(0o007)
    pool = Pool(json.loads(config_path.read_text()))
    owner = (args.owner_pid, args.owner_start) if args.owner_pid else None
    return supervise(
        pool,
        args.count,
        args.wait_seconds,
        command,
        owner,
        args.ready,
        args.avoid_router_window,
        args.idle_seconds,
        args.buildkite_job_id,
    )


if __name__ == "__main__":
    sys.exit(main())
