"""Dependency impact and replay result regressions; no candidate proof claims."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check
import impact
import profile_replay

POLICY = check.load_module('policy', check.REPO / 'verifier/check_submission.py')


def source_graph(**values):
    return {'submission/' + name.replace('.', '/') + '.lean': text.encode()
            for name, text in values.items()}


class ImpactTests(unittest.TestCase):
    def test_dependency_edit_explains_heavy_rebuild(self):
        old = source_graph(Solution='import Heavy\n', Heavy='import Policy\n', Policy='def x := 1\n')
        new = dict(old, **{'submission/Policy.lean': b'def x := 2\n'})
        report = impact.report(old, new, POLICY, ['Solution'], ['Heavy'])
        self.assertEqual(report['potential_rebuild_count'], 3)
        self.assertEqual(report['watched_modules'][0]['import_path_to_change'], ['Heavy', 'Policy'])

    def test_decoupled_policy_preserves_heavy(self):
        old = source_graph(Solution='import Heavy\nimport Policy\n', Heavy='import Init\n',
                           Policy='import Heavy\ndef x := 1\n')
        new = dict(old, **{'submission/Policy.lean': b'import Heavy\ndef x := 2\n'})
        report = impact.report(old, new, POLICY, ['Solution'], ['Heavy'])
        self.assertEqual(report['potential_rebuild_modules'], ['Policy', 'Solution'])
        self.assertFalse(report['watched_modules'][0]['potential_rebuild'])

    def test_deleted_dependency_and_untracked_added_module(self):
        old = source_graph(Solution='import A\n', A='import Init\n')
        new = source_graph(Solution='import B\n', B='import Init\n')
        result = impact.report(old, new, POLICY, ['Solution'], [])
        self.assertEqual(result['potential_rebuild_modules'], ['B', 'Solution'])
        self.assertIn('submission/A.lean', result['changed_files'])

    def test_pin_change_invalidates_every_reachable_module(self):
        old = source_graph(Solution='import A\n', A='import Init\n', Unused='import Init\n')
        old['lean-toolchain'] = b'old'
        result = impact.report(old, dict(old, **{'lean-toolchain': b'new'}), POLICY, ['Solution'], ['A'])
        self.assertEqual(result['potential_rebuild_modules'], ['A', 'Solution'])
        self.assertEqual(result['watched_modules'][0]['reason'], 'project pins changed')

    def test_no_change_and_cycles_terminate(self):
        sources = source_graph(Solution='import A\n', A='import B\n', B='import A\n')
        result = impact.report(sources, sources, POLICY, ['Solution'], ['A'])
        self.assertEqual(result['potential_rebuild_count'], 0)
        self.assertEqual(impact.route(impact.graph(sources, POLICY), 'A', {'Missing'}), [])

    def test_unknown_watch_is_an_error_not_false_cache_safety(self):
        sources = source_graph(Solution='import Init\n')
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            impact.report(sources, sources, POLICY, ['Solution'], ['Typo'])

    def test_base_paths_with_spaces_and_blobs(self):
        raw = b'abc blob 3\none\ndef blob 3\ntwo\n'
        with patch.object(impact.subprocess, 'check_output', side_effect=[
            'a' * 40 + '\n', b'submission/A B.lean\0lean-toolchain\0', raw]):
            commit, values = impact.base_sources(Path('/test'), 'HEAD')
        self.assertEqual(commit, 'a' * 40)
        self.assertEqual(values, {'submission/A B.lean': b'one', 'lean-toolchain': b'two'})

    def test_truncated_base_blob_is_rejected(self):
        with patch.object(impact.subprocess, 'check_output', side_effect=[
            'a' * 40 + '\n', b'submission/A.lean\0', b'abc blob 100\none\n']):
            with self.assertRaisesRegex(ValueError, 'Incomplete Git blob'):
                impact.base_sources(Path('/test'), 'HEAD')


class ReplayTests(unittest.TestCase):
    def test_timing_format_and_invalid_durations(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'timings.tsv'
            p.write_text('1\tdefn\tOne\n5\tthm\tTwo\n')
            self.assertEqual(profile_replay.read_timings(p)[0]['declaration'], 'Two')
            p.write_text('-1\tdefn\tBad\n')
            with self.assertRaises(ValueError):
                profile_replay.read_timings(p)

    def exercise(self, outcome):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace, project = root / 'state', root / 'state/project'
            binary = project / '.lake/build/bin/replayprof'
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b'simulated executable')
            source = {p.name: p.read_bytes() for p in profile_replay.SOURCE.iterdir() if p.is_file()}
            (workspace / 'ready.json').write_text(json.dumps({
                'source_sha256': check.digest(source),
                'binary_sha256': profile_replay.file_hash(binary),
            }))
            export = root / 'input.ndjson';export.write_text('fixture')
            def run(command, **kwargs):
                table = Path(command[-1])
                table.write_text('2\tthm\tFixture\n')
                if outcome == 'changed':
                    export.write_text('different')
                if outcome == 'timeout':
                    raise RuntimeError('timeout fixture')
                return 1 if outcome == 'failed' else 0
            argv = ['profile_replay.py', str(export), '--workspace', str(workspace)]
            with patch.object(sys, 'argv', argv), patch.object(profile_replay, 'run_guarded', side_effect=run), \
                 contextlib.redirect_stdout(io.StringIO()):
                if outcome == 'ok':
                    profile_replay.main()
                else:
                    with self.assertRaises(RuntimeError):
                        profile_replay.main()
            reports = list(workspace.glob('runs/*/result.json'))
            self.assertEqual(len(reports), 1)
            result = json.loads(reports[0].read_text())
            self.assertEqual(result['status'], 'complete' if outcome == 'ok' else 'failed')
            self.assertFalse(result['official_verified'])
            if outcome != 'ok':
                self.assertNotIn('kernel_microseconds', result)

    def test_completed_profile_is_only_diagnostic(self):
        self.exercise('ok')

    def test_failed_profile_not_marked_complete(self):
        self.exercise('failed')

    def test_timeout_retains_failed_report(self):
        self.exercise('timeout')

    def test_export_mutation_invalidates_profile(self):
        self.exercise('changed')


if __name__ == '__main__':
    unittest.main()
