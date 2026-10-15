#!/usr/bin/env python3
"""Incremental local Lean certificate check; never produces an official score.

Run from anywhere: python3 tools/lean-fast/check.py
The separate project keeps ordinary Lake traces and fully invalidates dependents.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
WORKSPACE = REPO / ".work" / "lean-fast"
PROJECT = WORKSPACE / "project"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inputs():
    paths = [REPO / name for name in (
        "lean-toolchain", "lakefile.lean", "lake-manifest.json", "SigGolf.lean",
        "verifier/comparator.json", "verifier/check_submission.py")]
    paths += sorted((REPO / "SigGolf").rglob("*.lean"))
    paths += sorted(p for p in (REPO / "submission").rglob("*") if p.is_file())
    snapshot = {p.relative_to(REPO).as_posix(): p.read_bytes() for p in paths}
    for name in ("check.py", "audit.py", "resources.py"):
        snapshot["local-runner/" + name] = (HERE / name).read_bytes()
    return snapshot


def digest(snapshot):
    h = hashlib.sha256()
    for name, data in sorted(snapshot.items()):
        h.update(name.encode() + b"\0")
        h.update(hashlib.sha256(data).digest())
    return h.hexdigest()


def filesystem_preflight(root, policy):
    """Check filesystem properties that a byte-only snapshot cannot preserve."""
    if root.is_symlink() or not root.is_dir():
        return ["submission root is not a directory"]
    entries = sorted(root.rglob("*"))
    errors = []
    if len(entries) > policy.MAX_FILES:
        errors.append(f"more than {policy.MAX_FILES} entries")
    for path in entries:
        rel = path.relative_to(root)
        mode = path.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            errors.append(f"{rel}: symlinks and special files are forbidden")
        elif stat.S_ISDIR(mode) and rel.parts[0] != "SigGolfCandidate":
            errors.append(f"{rel}: only SigGolfCandidate/ may contain modules")
    return errors


def read_import_cache(path, policy_hash, policy):
    """Treat missing, obsolete, or malformed optimization data as a cache miss."""
    try:
        cached = json.loads(path.read_bytes())
        if (not isinstance(cached, dict) or cached.get("version") != 1
                or cached.get("policy_sha256") != policy_hash):
            return {}
        entries = cached.get("imports")
        if not isinstance(entries, dict):
            return {}
        for key, names in entries.items():
            if (not isinstance(key, str) or len(key) != 64
                    or any(c not in "0123456789abcdef" for c in key)
                    or not isinstance(names, list)
                    or any(not isinstance(name, str) or not policy.MODULE.fullmatch(name)
                           for name in names)):
                return {}
        return entries
    except (OSError, ValueError, UnicodeError):
        return {}


def check_snapshot(snapshot, policy, cache_path=None):
    """Validate and parse the exact submission bytes used for this build."""
    policy_hash = hashlib.sha256(snapshot["verifier/check_submission.py"]).hexdigest()
    cached = read_import_cache(cache_path, policy_hash, policy) if cache_path else {}
    used = {}
    original_imports = policy.imports

    def cached_imports(source):
        source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        names = cached.get(source_hash)
        if names is None:
            names = original_imports(source)
        used[source_hash] = names
        return names

    with tempfile.TemporaryDirectory(prefix="sig-golf-local-policy-") as temporary:
        root = Path(temporary) / "submission"
        root.mkdir()
        for name, data in snapshot.items():
            if name.startswith("submission/"):
                target = root / name.removeprefix("submission/")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        # Only comment stripping/import parsing is memoized. The unmodified
        # checker revalidates claims, limits, names, and allowed import targets.
        policy.imports = cached_imports
        try:
            result = policy.check(root)
        finally:
            policy.imports = original_imports
    if cache_path:
        output = {"version": 1, "policy_sha256": policy_hash, "imports": used}
        temporary = cache_path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(output) + "\n", encoding="utf-8")
            temporary.replace(cache_path)
        except OSError:
            # A cache write failure must not weaken or prevent policy validation.
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return result


def sync(snapshot):
    """Copy only changed sources; never edit submission or the pinned project."""
    expected = {}
    for name, data in snapshot.items():
        if name.startswith("submission/") and name.endswith(".lean"):
            expected[name.removeprefix("submission/")] = data
        elif name.startswith("SigGolf/") or name in (
                "SigGolf.lean", "lean-toolchain", "lake-manifest.json", "lakefile.lean"):
            expected[name] = data
    expected["lakefile.lean"] += b"\nlean_lib Solution\n"
    changed = []
    for name, data in expected.items():
        target = PROJECT / name
        if target.is_symlink() or not target.resolve().is_relative_to(PROJECT.resolve()):
            raise ValueError(f"Build source must stay inside the project: {name}")
        if not target.is_file() or target.read_bytes() != data:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            changed.append(name)
    for folder in ("SigGolfCandidate", "SigGolf"):
        for path in (PROJECT / folder).rglob("*.lean"):
            if path.relative_to(PROJECT).as_posix() not in expected:
                path.unlink()
                changed.append("removed " + path.relative_to(PROJECT).as_posix())
    return changed


def focused_inputs(snapshot, targets, policy):
    """Freeze the requested proof closure while unrelated proof work continues."""
    relevant = {k: v for k, v in snapshot.items() if not k.startswith("submission/")}
    cache = read_import_cache(WORKSPACE / "policy-imports.json",
                             hashlib.sha256(snapshot["verifier/check_submission.py"]).hexdigest(), policy)
    pending, visited = list(targets), set()
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        visited.add(module)
        key = "submission/" + module.replace(".", "/") + ".lean"
        if key not in snapshot:
            continue
        relevant[key] = snapshot[key]
        source = snapshot[key].decode("utf-8")
        imports = cache.get(hashlib.sha256(source.encode("utf-8")).hexdigest())
        pending.extend(policy.imports(source) if imports is None else imports)
    return relevant


def run_build(target, env, log, max_memory_gb=98.0):
    from resources import run_guarded
    targets = [target] if isinstance(target, str) else target
    started = time.monotonic()
    count = 0

    def report(line):
        nonlocal count
        if " Built " in line or "Failed " in line or line.startswith("error:"):
            print(line.rstrip(), flush=True)
        if " Built " in line:
            count += 1

    status = run_guarded(
        ["lake", "--no-cache", "build"] + ["+" + t + ":olean" for t in targets],
        cwd=PROJECT, env=env, log=log, on_line=report, max_memory_gb=max_memory_gb)
    if status:
        print("\n".join(log.read_text().splitlines()[-30:]), file=sys.stderr)
        raise RuntimeError(f"Lean build failed; see {log}")
    return {"seconds": round(time.monotonic() - started, 3), "rebuilt_modules": count,
            "targets": targets}



def validate_workspace(repo, workspace):
    """Keep synchronization away from source, metadata and a nested repository."""
    project = workspace / "project"
    protected = [repo / name for name in ("submission", "SigGolf", "verifier", ".git", "tools", "scripts")]
    if workspace == repo or workspace in repo.parents or project == repo or project in repo.parents:
        raise ValueError("--workspace must be separate from the source repository")
    if any(workspace == path or path in workspace.parents for path in protected):
        raise ValueError("--workspace must not overlap source or repository metadata")
    if project.is_symlink() or (project.exists() and project.resolve() != project):
        raise ValueError("Build project must not be a symlink")


def main():
    global REPO, WORKSPACE, PROJECT
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO,
                        help="Source repository to snapshot; it is never modified")
    parser.add_argument("--workspace", type=Path,
                        help="Separate persistent Lake project, logs and receipts directory")
    parser.add_argument("--target", action="append",
                        help="Focused module; repeat to build shared dependencies once; default Solution")
    parser.add_argument("--dependencies-only", action="store_true",
                        help="Build direct imports of each target; no certificate receipt")
    parser.add_argument("--init", action="store_true",
                        help="Initialize the separate project and fetch pinned library caches")
    parser.add_argument("--max-memory-gb", type=float, default=98.0,
                        help="Additional memory ceiling in GB; physical/cgroup headroom also applies")
    parser.add_argument("--threads", type=int, default=2,
                        help="Lean runtime worker setting (default: 2)")
    args = parser.parse_args()
    REPO = args.repo.resolve()
    WORKSPACE = (args.workspace or REPO / ".work" / "lean-fast").resolve()
    PROJECT = WORKSPACE / "project"
    validate_workspace(REPO, WORKSPACE)
    targets = [t.removesuffix(".lean").replace("/", ".") for t in (args.target or ["Solution"])]
    if not math.isfinite(args.max_memory_gb) or not 0 < args.max_memory_gb <= 100:
        parser.error("--max-memory-gb must be greater than 0 and at most 100")
    if args.threads < 1:
        parser.error("--threads must be positive")
    if not args.init and not PROJECT.joinpath(".lake").is_dir():
        parser.error(f"Persistent project missing: {PROJECT}; run once with --init")
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    policy = load_module("submission_policy", REPO / "verifier/check_submission.py")
    if not all(policy.MODULE.fullmatch(t) for t in targets):
        parser.error("--target must be an ordinary Lean module name")
    with WORKSPACE.joinpath("build.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another local certificate check is running")
        receipt_path = WORKSPACE / "receipt.json"
        receipt_path.unlink(missing_ok=True)
        filesystem_errors = filesystem_preflight(REPO / "submission", policy)
        if filesystem_errors:
            raise RuntimeError("Submission policy rejected source: " + "; ".join(filesystem_errors))
        snapshot = inputs()
        snapshot_id = digest(snapshot)
        # The live preflight rejects symlinks, special entries, and invalid directories.
        # The frozen check binds the claim/import policy to the bytes we actually build.
        result = check_snapshot(snapshot, policy, WORKSPACE / "policy-imports.json")
        if not result["ok"]:
            raise RuntimeError("Submission policy rejected snapshot: " + "; ".join(result["errors"]))
        if args.dependencies_only:
            dependencies = set()
            for target in targets:
                key = "submission/" + target.replace(".", "/") + ".lean"
                if key not in snapshot:
                    parser.error("--dependencies-only target must be a submitted module: " + target)
                dependencies.update(policy.imports(snapshot[key].decode("utf-8")))
            targets = sorted(dependencies)
            if not targets:
                print("No dependencies to build; no certificate receipt produced.")
                return
        focus = focused_inputs(snapshot, targets, policy)
        changed = sync(snapshot)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        log_dir = WORKSPACE / "logs" / stamp
        log_dir.mkdir(parents=True)
        env = os.environ.copy()
        env["LEAN_NUM_THREADS"] = str(args.threads)
        # Import paths and toolchain must come from the copied pinned project.
        for name in ("LEAN_PATH", "LEAN_SRC_PATH", "LEAN_SYSROOT", "ELAN_TOOLCHAIN",
                     "LEAN_GITHASH", "LAKE_OVERRIDE_LEAN", "LAKE_HOME"):
            env.pop(name, None)
        print(f"Local source {snapshot_id[:12]}; synchronized {len(changed)} files.", flush=True)
        if args.init:
            from resources import run_guarded
            status = run_guarded(
                ["lake", "exe", "cache", "get"], cwd=PROJECT, env=env,
                log=log_dir / "init.log", max_memory_gb=args.max_memory_gb,
            )
            if status:
                raise RuntimeError(f"Pinned cache initialization failed; see {log_dir / 'init.log'}")
        build = run_build(targets, env, log_dir / "build.log", args.max_memory_gb)
        if "Solution" not in targets or args.dependencies_only:
            current = inputs()
            if any(current.get(k) != v for k, v in focus.items()):
                raise RuntimeError("Requested proof or dependency changed during the build; rerun for the current source.")
            print(f"Focused check passed in {build['seconds']:.2f}s; no certificate receipt produced.")
            return
        audit_module = load_module("local_certificate_audit", HERE / "audit.py")
        audit = audit_module.audit(PROJECT, result["claim"],
                                   json.loads(snapshot["verifier/comparator.json"]),
                                   log_dir / "audit.log", env=env, max_memory_gb=args.max_memory_gb)
        if not audit.get("ok", False):
            raise RuntimeError(f"Certificate audit failed; see {log_dir / 'audit.log'}")
        lean_version = subprocess.check_output(["lake", "env", "lean", "--version"],
                                                 cwd=PROJECT, env=env, text=True).strip()
        if digest(inputs()) != snapshot_id:
            raise RuntimeError("Source changed during the check; rerun for the current source. No receipt issued.")
        receipt = {
            "source_repo": str(REPO), "workspace": str(WORKSPACE),
            "mode": "local_incremental", "local_certificate_checked": True,
            "official_verified": False, "source_sha256": snapshot_id,
            "claim": result["claim"], "build": build, "audit": audit,
            "solution_olean_sha256": hashlib.sha256(
                (PROJECT / ".lake/build/lib/lean/Solution.olean").read_bytes()).hexdigest(),
            "seconds": round(time.monotonic() - started, 3),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "lean_version": lean_version,
            "log_directory": str(log_dir),
        }
        output = json.dumps(receipt, indent=2) + "\n"
        (log_dir / "receipt.json").write_text(output)
        temporary = receipt_path.with_suffix(".tmp")
        temporary.write_text(output)
        temporary.replace(receipt_path)
        print(f"Local certificate passed in {receipt['seconds']:.2f}s "
              f"({build['rebuilt_modules']} rebuilt modules).", flush=True)
        print(f"Receipt: {receipt_path}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"Local check failed: {error}", file=sys.stderr)
        sys.exit(1)
