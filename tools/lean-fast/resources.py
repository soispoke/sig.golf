"""Guard owned compiler process groups on macOS and Linux."""
from __future__ import annotations

from contextlib import ExitStack
import math
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time


POLL_SECONDS = 0.5
TERMINATE_SECONDS = 5.0


def mac_memory(raw, total_bytes):
    page_size = int(re.search(r"page size of (\d+)", raw).group(1))
    rows = {match.group(1): int(match.group(2)) for match in
            re.finditer(r"^([^\n:]+):\s+(\d+)\.", raw, re.M)}
    pages = (rows["Anonymous pages"] + rows["Pages wired down"]
             + rows["Pages occupied by compressor"] - rows["Pages purgeable"])
    return ("system", max(0, pages) * page_size / 1e9, int(total_bytes) / 1e9)


def linux_memory(raw):
    rows = {match.group(1): int(match.group(2)) * 1024 for match in
            re.finditer(r"^([^\n:]+):\s+(\d+) kB$", raw, re.M)}
    total, available = rows["MemTotal"], rows["MemAvailable"]
    if not 0 <= available <= total or total <= 0:
        raise ValueError("Invalid Linux memory counters")
    return ("system", (total - available) / 1e9, total / 1e9)


def cgroup_memory(root, current):
    """Read finite v2 limits at the current cgroup and each visible ancestor."""
    if not current.is_relative_to(root):
        raise ValueError("Cgroup must be inside its mount")
    constraints = []
    while True:
        maximum = current / "memory.max"
        if maximum.exists():
            value = maximum.read_text().strip()
            if value != "max":
                capacity = int(value)
                used = int((current / "memory.current").read_text().strip())
                if capacity <= 0 or used < 0:
                    raise ValueError("Invalid cgroup memory counters")
                constraints.append(("cgroup:" + str(current.relative_to(root)),
                                    used / 1e9, capacity / 1e9))
        if current == root:
            break
        current = current.parent
    return constraints


def memory_constraints():
    """Fail closed on unreadable counters instead of assuming free memory."""
    try:
        if sys.platform == "darwin":
            raw = subprocess.check_output(["vm_stat"], text=True, timeout=5)
            total = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True, timeout=5)
            return [mac_memory(raw, total)]
        if sys.platform == "linux":
            constraints = [linux_memory(Path("/proc/meminfo").read_text())]
            groups = Path("/proc/self/cgroup").read_text().splitlines()
            v2 = next((line[3:] for line in groups if line.startswith("0::")), None)
            # A unified mount is required when the process reports cgroup v2.
            if v2 is not None:
                mounts = Path("/proc/self/mountinfo").read_text().splitlines()
                for line in mounts:
                    left, separator, right = line.partition(" - ")
                    if separator and right.split()[0] == "cgroup2":
                        fields = left.split()
                        mount_root, mount_point = Path(fields[3]), Path(fields[4])
                        # Mountinfo escapes whitespace and backslashes as octal.
                        unescape = lambda value: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), str(value))
                        mount_root, mount_point = Path(unescape(mount_root)), Path(unescape(mount_point))
                        group = Path(v2)
                        relative = group.relative_to(mount_root)
                        if ".." in relative.parts:
                            raise ValueError("Cgroup path escapes its mount")
                        current = mount_point / relative
                        if not current.is_dir():
                            raise ValueError("Current cgroup is inaccessible")
                        constraints.extend(cgroup_memory(mount_point, current))
                        break
                else:
                    raise ValueError("Cgroup v2 mount is inaccessible")
            elif any("memory" in line.split(":", 2)[1].split(",") for line in groups):
                raise ValueError("Legacy memory cgroups are unsupported; use a cgroup v2 host")
            return constraints
        raise ValueError("Only macOS and Linux memory sampling is supported")
    except (OSError, subprocess.SubprocessError, AttributeError, KeyError, ValueError) as error:
        raise RuntimeError(f"Cannot sample system memory: {error}") from error


def pressure_reason(max_memory_gb, admission=False):
    for scope, used, capacity in memory_constraints():
        if not all(math.isfinite(x) for x in (used, capacity)) or capacity <= 0 or used < 0:
            raise RuntimeError("Invalid memory sample")
        # Leave headroom for the OS and services. The user's limit may only
        # tighten this bound, never relax a physical or container constraint.
        limit = min(max_memory_gb, capacity * 0.85)
        if admission:
            limit *= 0.9
        if used >= limit:
            return f"{scope} memory is {used:.2f} GB (limit {limit:.2f} GB)"
    return None


def _stop_group(process):
    """Reap the leader and kill remaining descendants even if the leader exits first."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    deadline = time.monotonic() + TERMINATE_SECONDS
    while time.monotonic() < deadline:
        process.poll()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            process.wait()
            return
        except PermissionError:
            # macOS sandboxes may deny signal 0; do not skip the final kill.
            pass
        time.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_guarded(command, cwd, env, log, max_memory_gb=98.0, on_line=None, timeout=None):
    """Stream output while independently monitoring RAM; never kill unrelated jobs.

    ``log`` accepts a path or an open text stream. The caller controls any build
    lock. A refused start or memory limit raises RuntimeError; an expired timeout
    raises subprocess.TimeoutExpired. Every exit path cleans up the owned group.
    """
    if not math.isfinite(max_memory_gb) or not 0 < max_memory_gb <= 100:
        raise ValueError("Memory ceiling must be greater than 0 and at most 100 GB")
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("Timeout must be positive")
    with ExitStack() as stack:
        if threading.current_thread() is threading.main_thread():
            def interrupted(number, frame):
                raise RuntimeError(f"Compiler interrupted by signal {number}")

            previous = signal.signal(signal.SIGTERM, interrupted)
            stack.callback(signal.signal, signal.SIGTERM, previous)
        if hasattr(log, "write"):
            stream = log
        else:
            path = Path(log)
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = stack.enter_context(path.open("w", encoding="utf-8"))
        pressure = pressure_reason(max_memory_gb, admission=True)
        if pressure:
            message = f"Compiler not started: {pressure}; wait for free memory and rerun."
            stream.write(message + "\n")
            stream.flush()
            raise RuntimeError(message)
        process = subprocess.Popen(
            command, cwd=cwd, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            start_new_session=True,
        )
        messages = queue.Queue()

        def read_output():
            try:
                for line in process.stdout:
                    messages.put(line)
            except BaseException as error:
                messages.put(error)
            finally:
                messages.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        try:
            reader.start()
        except BaseException:
            _stop_group(process)
            process.stdout.close()
            raise
        started = time.monotonic()
        next_sample = started
        output_done = False
        try:
            while not output_done or process.poll() is None:
                now = time.monotonic()
                if timeout is not None and now - started >= timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                if now >= next_sample:
                    pressure = pressure_reason(max_memory_gb)
                    if pressure:
                        raise RuntimeError(f"Compiler stopped: {pressure}. Rerun after memory is available.")
                    next_sample = time.monotonic() + POLL_SECONDS
                try:
                    item = messages.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item is None:
                    output_done = True
                elif isinstance(item, BaseException):
                    raise item
                else:
                    stream.write(item)
                    stream.flush()
                    if on_line is not None:
                        on_line(item)
            return process.wait()
        except BaseException as error:
            stream.write(f"\nLocal compiler stopped: {error}\n")
            stream.flush()
            raise
        finally:
            _stop_group(process)
            reader.join(timeout=1)
            if not reader.is_alive():
                process.stdout.close()
