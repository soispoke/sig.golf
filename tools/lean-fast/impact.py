#!/usr/bin/env python3
"""Read-only dependency impact report for a candidate edit; never a proof receipt."""
from __future__ import annotations

import argparse
from collections import deque
import json
from pathlib import Path
import subprocess
import sys

import check

PINS = {'lean-toolchain', 'lakefile.lean', 'lake-manifest.json'}


def module_name(path):
    if not path.endswith('.lean'):
        return None
    if path.startswith('submission/'):
        return path[len('submission/'):-len('.lean')].replace('/', '.')
    if path == 'SigGolf.lean' or path.startswith('SigGolf/'):
        return path[:-len('.lean')].replace('/', '.')
    return None


def graph(sources, policy):
    return {module_name(path): set(policy.imports(data.decode('utf-8')))
            for path, data in sources.items() if module_name(path)}


def closure(edges, roots):
    seen, pending = set(), list(roots)
    while pending:
        name = pending.pop()
        if name not in seen:
            seen.add(name)
            pending.extend(edges.get(name, ()))
    return seen


def route(edges, start, goals):
    """One shortest import path explaining an affected module, including cycles."""
    parents = {start: None}
    pending = deque([start])
    while pending:
        name = pending.popleft()
        if name in goals:
            path = []
            while name is not None:
                path.append(name)
                name = parents[name]
            return path[::-1]
        for child in sorted(edges.get(name, ())):
            if child not in parents:
                parents[child] = name
                pending.append(child)
    return []


def report(before, after, policy, targets, watches):
    old, new = graph(before, policy), graph(after, policy)
    missing = set(targets + watches) - set(new)
    if missing:
        raise ValueError('Unknown candidate/contract modules: ' + ', '.join(sorted(missing)))
    changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
    changed_modules = {m for p in changed if (m := module_name(p))}
    merged = {m: old.get(m, set()) | new.get(m, set()) for m in set(old) | set(new)}
    reverse = {}
    for name, imports in merged.items():
        for dependency in imports:
            reverse.setdefault(dependency, set()).add(name)
    invalidated = closure(reverse, changed_modules)
    pin_changes = sorted(changed & PINS)
    if pin_changes:
        invalidated = set(new)
    reachable = closure(new, targets) & new.keys()
    affected = sorted(invalidated & reachable)
    return {
        'mode': 'dependency_impact', 'official_verified': False,
        'changed_files': sorted(changed), 'project_pin_changes': pin_changes,
        'targets': targets, 'reachable_module_count': len(reachable),
        'potential_rebuild_count': len(affected), 'potential_rebuild_modules': affected,
        'watched_modules': [{
            'module': name, 'potential_rebuild': name in invalidated,
            'reason': 'project pins changed' if pin_changes else 'source/import dependency',
            'import_path_to_change': route(merged, name, changed_modules),
        } for name in watches],
        'note': 'Source-level estimate; Lake decides actual cache validity. No build or certificate check was run.',
    }


def base_sources(repo, revision):
    commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', '--verify',
                                      '--end-of-options', revision + '^{commit}'], text=True).strip()
    # Read a fixed commit, never interpolate a revision into shell code.
    paths = subprocess.check_output(['git', '-C', str(repo), 'ls-tree', '-r', '--name-only', '-z',
                                     commit, '--', 'submission', 'SigGolf', 'SigGolf.lean', *sorted(PINS)])
    names = [name.decode('utf-8') for name in paths.split(b'\0') if name]
    specs = [commit + ':' + name for name in names]
    # NUL input/output is not supported by every installed Git, so refuse newlines.
    if any('\n' in name or '\r' in name for name in names):
        raise ValueError('Source paths containing newlines are unsupported')
    raw = subprocess.check_output(['git', '-C', str(repo), 'cat-file', '--batch'],
                                  input=('\n'.join(specs) + '\n').encode()) if specs else b''
    result, offset = {}, 0
    for name in names:
        end = raw.index(b'\n', offset)
        header = raw[offset:end].split()
        if len(header) != 3 or header[1] != b'blob':
            raise ValueError('Base source is not a regular Git blob: ' + name)
        size = int(header[2])
        offset = end + 1
        if size < 0 or offset + size >= len(raw) or raw[offset + size:offset + size + 1] != b'\n':
            raise ValueError('Incomplete Git blob: ' + name)
        result[name] = raw[offset:offset + size]
        offset += size + 1
    return commit, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=check.REPO)
    parser.add_argument('--base', required=True, help='Compare this Git commit/ref to current source bytes')
    parser.add_argument('--target', action='append', help='Default: Solution')
    parser.add_argument('--watch', action='append', default=[], help='Explain invalidation of an expensive module')
    args = parser.parse_args()
    repo = args.repo.resolve()
    check.REPO = repo
    policy = check.load_module('submission_policy', repo / 'verifier/check_submission.py')
    errors = check.filesystem_preflight(repo / 'submission', policy)
    if errors:
        raise ValueError('; '.join(errors))
    snapshot = check.inputs()
    before_commit, before = base_sources(repo, args.base)
    after = {p: data for p, data in snapshot.items()
             if p.startswith('submission/') or module_name(p) or p in PINS}
    result = report(before, after, policy, args.target or ['Solution'], args.watch)
    result['base_commit'] = before_commit
    result['source_sha256'] = check.digest(snapshot)
    # Avoid reporting an internally mixed source snapshot as current.
    if check.digest(check.inputs()) != result['source_sha256']:
        raise RuntimeError('Source changed during impact analysis; rerun')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'Impact analysis failed: {error}', file=sys.stderr)
        sys.exit(1)
