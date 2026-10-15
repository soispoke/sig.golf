#!/usr/bin/env python3
"""Build and run the pinned diagnostic replay profiler with the runner's memory guard."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import check
from resources import run_guarded

SOURCE = Path(__file__).resolve().parent / 'replay'


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read_timings(path):
    result = []
    for line in path.read_text().splitlines():
        if not line:
            continue
        micros, kind, name = line.split('\t', 2)
        value = int(micros)
        if value < 0 or not kind or not name:
            raise ValueError('Invalid replay timing row')
        result.append({'microseconds': value, 'kind': kind, 'declaration': name})
    return sorted(result, key=lambda item: item['microseconds'], reverse=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('export', nargs='?', type=Path, help='Existing lean4export NDJSON file')
    parser.add_argument('--workspace', type=Path, default=check.REPO / '.work/lean-fast-replay')
    parser.add_argument('--init', action='store_true', help='Fetch pinned dependencies and build the profiler')
    parser.add_argument('--max-memory-gb', type=float, default=98.0)
    parser.add_argument('--timeout', type=float, default=3600, help='Replay timeout in seconds')
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    check.validate_workspace(check.REPO, workspace)
    if args.export is None and not args.init:
        parser.error('provide an export or --init')
    export = args.export.resolve(strict=True) if args.export else None
    workspace.mkdir(parents=True, exist_ok=True)
    project = workspace / 'project'
    env = os.environ.copy()
    env['LEAN_NUM_THREADS'] = '1'
    for key in ('LEAN_PATH', 'LEAN_SRC_PATH', 'LEAN_SYSROOT', 'ELAN_TOOLCHAIN',
                'LEAN_GITHASH', 'LAKE_OVERRIDE_LEAN', 'LAKE_HOME'):
        env.pop(key, None)
    with (workspace / 'profile.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('Another replay profile is using this workspace')
        sources = {p.name: p.read_bytes() for p in SOURCE.iterdir() if p.is_file()}
        identity = check.digest(sources)
        ready = workspace / 'ready.json'
        binary = project / '.lake/build/bin/replayprof'
        if args.init:
            ready.unlink(missing_ok=True)
            project.mkdir(parents=True, exist_ok=True)
            for name, data in sources.items():
                target = project / name
                if target.is_symlink():
                    raise ValueError('Profiler source must not be a symlink')
                if not target.exists() or target.read_bytes() != data:
                    target.write_bytes(data)
            for command, log in [(['lake', '--no-cache', 'build', 'replayprof'], 'build.log'),
                                 ([str(binary), 'self-test'], 'self-test.log')]:
                if run_guarded(command, cwd=project, env=env, log=workspace / log,
                               max_memory_gb=args.max_memory_gb, timeout=args.timeout):
                    raise RuntimeError(f'Profiler initialization failed; see {workspace / log}')
            ready.write_text(json.dumps({'source_sha256': identity, 'binary_sha256': file_hash(binary)}) + '\n')
        try:
            initialized = json.loads(ready.read_text())
        except (OSError, ValueError) as error:
            raise RuntimeError('Profiler is not initialized; run with --init') from error
        if (initialized.get('source_sha256') != identity or not binary.is_file()
                or initialized.get('binary_sha256') != file_hash(binary)):
            raise RuntimeError('Profiler or its source changed; rerun with --init')
        if export is None:
            print('Pinned replay profiler built and self-tests passed.')
            return
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        output = workspace / 'runs' / stamp
        output.mkdir(parents=True)
        before = file_hash(export)
        table = output / 'timings.tsv'
        started = time.monotonic()
        result = {'mode': 'diagnostic_replay', 'official_verified': False, 'status': 'failed',
                  'export_sha256': before, 'profiler_source_sha256': identity,
                  'toolchain': sources['lean-toolchain'].decode().strip(),
                  'partial_timings': str(table) + '.partial.tsv'}
        try:
            status = run_guarded([str(binary), 'replay', str(export), str(table)],
                                 cwd=project, env=env, log=output / 'replay.log',
                                 max_memory_gb=args.max_memory_gb, timeout=args.timeout)
            if status:
                raise RuntimeError(f'Replay failed with exit {status}; see {output / "replay.log"}')
            if file_hash(export) != before:
                raise RuntimeError('Export changed during replay')
            rows = read_timings(table)
            result.update(status='complete', declarations=len(rows),
                          kernel_microseconds=sum(row['microseconds'] for row in rows),
                          slowest=rows[:40], timings=str(table))
        except BaseException as error:
            result['error'] = str(error)
            raise
        finally:
            result['elapsed_seconds'] = round(time.monotonic() - started, 3)
            (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
            print('Diagnostic report:', output / 'result.json', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f'Replay profiling failed: {error}', file=sys.stderr)
        sys.exit(1)
