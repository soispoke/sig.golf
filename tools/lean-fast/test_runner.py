"""Safety and lifecycle tests; these do not certify a candidate's Lean proofs."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import audit
import check
import resources


class MemoryTests(unittest.TestCase):
    def test_linux_available_includes_reclaimable_cache(self):
        scope, used, capacity = resources.linux_memory(
            'MemTotal:       8000000 kB\nMemFree:        500000 kB\nMemAvailable:  3000000 kB\n')
        self.assertEqual(scope, 'system')
        self.assertEqual(used, 5.12)
        self.assertEqual(capacity, 8.192)

    def test_mac_footprint(self):
        raw = ('Mach Virtual Memory Statistics: (page size of 16384 bytes)\n'
               'Anonymous pages: 100.\nPages wired down: 20.\n'
               'Pages occupied by compressor: 5.\nPages purgeable: 10.\n')
        self.assertEqual(resources.mac_memory(raw, 8_000_000_000),
                         ('system', 115 * 16384 / 1e9, 8.0))

    def test_invalid_counters_fail_closed(self):
        with self.assertRaises(ValueError):
            resources.linux_memory('MemTotal: 100 kB\nMemAvailable: 101 kB\n')
        with patch.object(resources, 'memory_constraints', return_value=[('system', 1, 0)]):
            with self.assertRaises(RuntimeError):
                resources.pressure_reason(98)

    def test_small_host_limit_overrides_large_cli_budget(self):
        with patch.object(resources, 'memory_constraints', return_value=[('system', 7, 8)]):
            self.assertIn('6.80 GB', resources.pressure_reason(98))

    def test_container_limit_overrides_large_host(self):
        samples = [('system', 20, 128), ('cgroup:job', 3.5, 4)]
        with patch.object(resources, 'memory_constraints', return_value=samples):
            self.assertIn('cgroup:job', resources.pressure_reason(98))

    def test_user_limit_tightens_and_admission_reserves_headroom(self):
        with patch.object(resources, 'memory_constraints', return_value=[('system', 4.6, 128)]):
            self.assertIsNone(resources.pressure_reason(5))
            self.assertIsNotNone(resources.pressure_reason(5, admission=True))
        with patch.object(resources, 'memory_constraints', return_value=[('system', 5.1, 128)]):
            self.assertIsNotNone(resources.pressure_reason(5))

    def test_nested_cgroup_checks_parent_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = root / 'job'
            child.mkdir()
            (root / 'memory.max').write_text('4000000000')
            (root / 'memory.current').write_text('3500000000')
            (child / 'memory.max').write_text('max')
            limits = resources.cgroup_memory(root, child)
            self.assertEqual(limits, [('cgroup:.', 3.5, 4.0)])

    def test_unreadable_finite_cgroup_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'memory.max').write_text('4000000000')
            with self.assertRaises(OSError):
                resources.cgroup_memory(root, root)


class ProcessTests(unittest.TestCase):
    def run_command(self, code, **kwargs):
        return resources.run_guarded([sys.executable, '-c', code],
                                     cwd=HERE, env=os.environ.copy(), log=io.StringIO(), **kwargs)

    def test_pressure_prevents_spawn(self):
        with patch.object(resources, 'pressure_reason', return_value='full'), \
             patch.object(subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(RuntimeError, 'not started'):
                self.run_command('raise SystemExit(0)')
            spawn.assert_not_called()

    def test_failure_exit_preserved(self):
        with patch.object(resources, 'pressure_reason', return_value=None):
            self.assertEqual(self.run_command('raise SystemExit(7)'), 7)

    def test_memory_loss_stops_silent_owned_process(self):
        with patch.object(resources, 'pressure_reason', side_effect=[None, 'full']):
            with self.assertRaisesRegex(RuntimeError, 'Compiler stopped'):
                self.run_command('import time; time.sleep(30)')

    def test_timeout_stops_silent_owned_process(self):
        with patch.object(resources, 'pressure_reason', return_value=None):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.run_command('import time; time.sleep(30)', timeout=0.1)

    def test_callback_exception_reaps_process(self):
        def fail(line):
            raise ValueError('callback')
        with patch.object(resources, 'pressure_reason', return_value=None):
            with self.assertRaisesRegex(ValueError, 'callback'):
                self.run_command('import time; print("ready", flush=True); time.sleep(30)', on_line=fail)


class SourceTests(unittest.TestCase):
    def test_workspace_cannot_overwrite_repo_or_submission(self):
        repo = Path('/tmp/repo')
        for workspace in [repo, repo.parent, repo / 'submission/cache', repo / 'verifier/cache',
                          repo / '.git/cache', repo / 'tools/cache']:
            with self.subTest(workspace=workspace), self.assertRaises(ValueError):
                check.validate_workspace(repo, workspace)
        check.validate_workspace(repo, repo / '.work/lean-fast')
        check.validate_workspace(repo, Path('/tmp/separate-state'))

    def test_project_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'state').mkdir()
            (root / 'repo').mkdir()
            (root / 'state/project').symlink_to(root / 'repo')
            with self.assertRaises(ValueError):
                check.validate_workspace(root / 'repo', root / 'state')

    def test_source_hash_binds_claim_toolchain_and_runner(self):
        original = {'submission/claim.json': b'old', 'lean-toolchain': b'pin',
                    'local-runner/check.py': b'runner'}
        for key in original:
            changed = dict(original)
            changed[key] += b'changed'
            self.assertNotEqual(check.digest(original), check.digest(changed))
        self.assertEqual(check.digest(original), check.digest(dict(reversed(list(original.items())))))

    def test_sync_preserves_unchanged_times_and_removes_deleted_modules(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(check, 'PROJECT', Path(tmp)):
            snapshot = {'submission/SigGolfCandidate/A.lean': b'def a := 1\n',
                        'lakefile.lean': b'import Lake\n', 'lean-toolchain': b'pinned\n'}
            check.sync(snapshot)
            a = Path(tmp) / 'SigGolfCandidate/A.lean'
            stamp = a.stat().st_mtime_ns
            self.assertEqual(check.sync(snapshot), [])
            self.assertEqual(a.stat().st_mtime_ns, stamp)
            del snapshot['submission/SigGolfCandidate/A.lean']
            self.assertIn('removed SigGolfCandidate/A.lean', check.sync(snapshot))
            self.assertFalse(a.exists())

    def test_sync_does_not_follow_source_directory_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / 'project'
            project.mkdir()
            outside = root / 'outside'
            outside.mkdir()
            (project / 'SigGolfCandidate').symlink_to(outside)
            snapshot = {'submission/SigGolfCandidate/A.lean': b'def a := 1',
                        'lakefile.lean': b'import Lake'}
            with patch.object(check, 'PROJECT', project), self.assertRaises(ValueError):
                check.sync(snapshot)
            self.assertEqual(list(outside.iterdir()), [])

    def test_changed_policy_invalidates_import_cache(self):
        class Policy:
            import re
            MODULE = re.compile(r'[A-Za-z]+')
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'imports.json'
            p.write_text(json.dumps({'version': 1, 'policy_sha256': 'old',
                                     'imports': {'a'*64: ['Lean']}}))
            self.assertEqual(check.read_import_cache(p, 'new', Policy), {})
            self.assertEqual(check.read_import_cache(p, 'old', Policy), {'a'*64: ['Lean']})
            p.write_text('broken')
            self.assertEqual(check.read_import_cache(p, 'old', Policy), {})


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.claim = {'S': 16, 'W': 0, 'K': 0, 'C': 1,
                      'layout': dict(zip(('message', 'secret_key', 'public_key', 'cache', 'signature', 'witness'),
                                         (0, 32, 64, 80, 80, 96)))}
        self.comparator = {'theorem_names': audit.THEOREMS, 'definition_names': audit.DEFINITIONS,
                           'permitted_axioms': ['propext', 'Quot.sound', 'Classical.choice']}

    def test_audit_requires_exact_types_safe_definition_and_axioms(self):
        source = audit.render_audit(self.claim, self.comparator, 'TEST_MARKER')
        self.assertIn('SigGolf.Certificate SigGolf.Challenge.submission 1', source)
        self.assertIn('actual.type == expected.type', source)
        self.assertIn('submission.safety == .safe', source)
        self.assertIn('collectAxioms target', source)
        self.assertIn('Disallowed axiom', source)

    def test_bad_claim_and_comparator_rejected(self):
        for value in [True, -1, '1']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                audit.render_audit(dict(self.claim, C=value), self.comparator, 'TEST_MARKER')
        with self.assertRaises(ValueError):
            audit.render_audit(self.claim, dict(self.comparator, theorem_names=[]), 'TEST_MARKER')

    def test_success_needs_exit_zero_and_one_unpredictable_marker(self):
        for exit_code, marker_count, expected in [(0, 0, False), (0, 1, True), (1, 1, False), (0, 2, False)]:
            with self.subTest(exit_code=exit_code, markers=marker_count), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                def fake_run(command, **kwargs):
                    source = (root / 'LocalCertificateAudit.lean').read_text()
                    marker = source.split('logInfo "')[-1].split('"')[0]
                    for _ in range(marker_count):
                        kwargs['log'].write(json.dumps({'severity': 'information', 'data': marker}) + '\n')
                    return exit_code
                with patch.object(audit, 'run_guarded', side_effect=fake_run):
                    result = audit.audit(root, self.claim, self.comparator, root / 'audit.log')
                self.assertEqual(result['ok'], expected)
                self.assertFalse(result['official_verified'])


class ReceiptLifecycleTests(unittest.TestCase):
    """Exercise the CLI control flow with a simulated compiler, not a proof verifier."""
    def run_case(self, mode):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, workspace = root / 'repo', root / 'state'
            (repo / 'submission/SigGolfCandidate').mkdir(parents=True)
            (repo / 'verifier').mkdir()
            (repo / 'SigGolf').mkdir()
            (workspace / 'project/.lake/build/lib/lean').mkdir(parents=True)
            for name in ('lean-toolchain', 'lakefile.lean', 'lake-manifest.json', 'SigGolf.lean'):
                (repo / name).write_text('pinned fixture\n')
            policy_source = (HERE.parents[1] / 'verifier/check_submission.py').read_bytes()
            (repo / 'verifier/check_submission.py').write_bytes(policy_source)
            comparator = {'theorem_names': audit.THEOREMS, 'definition_names': audit.DEFINITIONS,
                          'permitted_axioms': ['propext', 'Quot.sound', 'Classical.choice']}
            (repo / 'verifier/comparator.json').write_text(json.dumps(comparator))
            claim = {'S': 16, 'W': 0, 'K': 0, 'C': 1,
                     'layout': dict(zip(('message', 'secret_key', 'public_key', 'cache', 'signature', 'witness'),
                                        (0, 32, 64, 80, 80, 96)))}
            (repo / 'submission/claim.json').write_text(json.dumps(claim))
            source = repo / 'submission/Solution.lean'
            source.write_text('import SigGolf\n')
            (repo / 'submission/SigGolfCandidate/A.lean').write_text('import SigGolf\n')
            receipt = workspace / 'receipt.json'
            receipt.write_text('old receipt')
            original_load = check.load_module

            def load(name, path):
                if name == 'local_certificate_audit':
                    return SimpleNamespace(audit=lambda *a, **kw: {'ok': mode != 'audit-fails'})
                return original_load(name, path)

            def build(*args):
                if mode == 'build-fails':
                    raise RuntimeError('compile failed')
                if mode == 'source-changes':
                    source.write_text('import SigGolf\n-- changed during build\n')
                output = workspace / 'project/.lake/build/lib/lean/Solution.olean'
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b'simulated artifact')
                return {'seconds': 0.1, 'rebuilt_modules': 1}

            argv = ['check.py', '--repo', str(repo), '--workspace', str(workspace)]
            if mode.startswith('init-'):
                import shutil
                shutil.rmtree(workspace / 'project/.lake')
                argv += ['--init']
            if mode == 'focused':
                argv += ['--target', 'SigGolfCandidate.A']
            with patch.object(sys, 'argv', argv), patch.object(check, 'run_build', side_effect=build) as builder, \
                 patch.object(resources, 'run_guarded', return_value=1 if mode == 'init-fails' else 0) as initializer, \
                 patch.object(check, 'load_module', side_effect=load), \
                 patch.object(check.subprocess, 'check_output', return_value='Lean fixture'), \
                 patch.object(check, 'REPO', repo), patch.object(check, 'WORKSPACE', workspace), \
                 patch.object(check, 'PROJECT', workspace / 'project'), contextlib.redirect_stdout(io.StringIO()):
                if mode in ('build-fails', 'audit-fails', 'source-changes', 'init-fails'):
                    with self.assertRaises(RuntimeError):
                        check.main()
                else:
                    check.main()
            if mode.startswith('init-'):
                self.assertEqual(initializer.call_args.args[0], ['lake', 'exe', 'cache', 'get'])
                self.assertEqual(builder.call_count, 0 if mode == 'init-fails' else 1)
            else:
                initializer.assert_not_called()
            if mode in ('success', 'init-success'):
                result = json.loads(receipt.read_text())
                self.assertTrue(result['local_certificate_checked'])
                self.assertFalse(result['official_verified'])
                self.assertEqual(result['claim'], claim)
            else:
                self.assertFalse(receipt.exists(), 'failed or focused checks must not leave a stale receipt')

    def test_init_failure_never_builds_or_issues_receipt(self):
        self.run_case('init-fails')

    def test_init_success_builds_and_audits(self):
        self.run_case('init-success')

    def test_cli_success_emits_only_local_receipt(self):
        self.run_case('success')

    def test_failed_build_removes_old_receipt(self):
        self.run_case('build-fails')

    def test_failed_audit_removes_old_receipt(self):
        self.run_case('audit-fails')

    def test_source_mutation_prevents_receipt(self):
        self.run_case('source-changes')

    def test_focused_check_cannot_masquerade_as_full_certificate(self):
        self.run_case('focused')


if __name__ == '__main__':
    unittest.main()
