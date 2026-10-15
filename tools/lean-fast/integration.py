#!/usr/bin/env python3
"""Exercise pinned Lean compilation, export and diagnostic replay on a small fixture."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

import profile_replay
from resources import run_guarded

TOOLS = Path(__file__).resolve().parent


def profile(arguments):
    previous = sys.argv
    try:
        sys.argv = ['profile_replay.py', *arguments]
        profile_replay.main()
    finally:
        sys.argv = previous


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-memory-gb', type=float, default=6)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='lean-fast-integration-') as tmp:
        workspace = Path(tmp) / 'replay'
        options = ['--workspace', str(workspace), '--max-memory-gb', str(args.max_memory_gb),
                   '--timeout', '600']
        try:
            profile(['--init', *options])
            project = workspace / 'project'
            env = os.environ.copy()
            for key in ('LEAN_PATH', 'LEAN_SRC_PATH', 'LEAN_SYSROOT', 'ELAN_TOOLCHAIN',
                        'LEAN_GITHASH', 'LAKE_OVERRIDE_LEAN', 'LAKE_HOME'):
                env.pop(key, None)
            env['LEAN_NUM_THREADS'] = '1'
            # Lake adds its dependency paths; this path supplies the fixture's .olean.
            env['LEAN_PATH'] = str(project)

            def run(command, name):
                log = workspace / name
                status = run_guarded(command, cwd=project, env=env, log=log,
                                     max_memory_gb=args.max_memory_gb, timeout=600)
                if status:
                    raise RuntimeError(f'{command[0]} failed: {log.read_text()[-4000:]}')

            run(['lake', '--no-cache', 'build', 'lean4export'], 'exporter-build.log')
            for name in ('BitVecLists', 'FastLists'):
                shutil.copyfile(TOOLS / 'examples' / f'{name}.lean', project / f'{name}.lean')
                run(['lake', 'env', 'lean', '-o', f'{name}.olean', f'{name}.lean'], f'{name}.log')
            exporter = project / '.lake/packages/lean4export/.lake/build/bin/lean4export'
            export = workspace / 'fixture.ndjson'
            run(['lake', 'env', str(exporter), 'FastLists', '--',
                 'FastList.dropK_eq', 'FastList.appendK_eq', 'FastList.flattenK_eq',
                 'FastList.getK_eq'], 'fixture.ndjson')
            profile([str(export), *options])
            reports = list((workspace / 'runs').glob('*/result.json'))
            if len(reports) != 1:
                raise RuntimeError('Expected exactly one diagnostic report')
            result = json.loads(reports[0].read_text())
            if (result['status'] != 'complete' or result['official_verified'] is not False
                    or result['declarations'] <= 0):
                raise RuntimeError('Fixture replay did not complete')
            print(f"Lean helpers compiled; {result['declarations']} fixture declarations replayed. "
                  'This is a tooling test, not candidate verification.')
        except BaseException:
            # Temporary workspaces are cleaned even on failure. Preserve diagnostics
            # in CI output before cleanup rather than pointing to deleted log files.
            for log in sorted(workspace.rglob('*.log')):
                print(f'\n{log.relative_to(workspace)}:\n{log.read_text(errors="replace")[-4000:]}',
                      file=sys.stderr)
            raise



if __name__ == '__main__':
    main()
