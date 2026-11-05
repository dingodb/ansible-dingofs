"""Cache/Client CPU ownership is based on all live threads, not only leaders."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('cpu_fixture', Path(__file__).with_name('test_deployment_safety.py'))
fixtures = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(fixtures)
REPO = Path(__file__).resolve().parents[1]


class ThreadOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CpuTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.code = self.fixture.code
        self.args = self.fixture.args

    def process(self, pid=900003, exe='/opt/other/dingo-cache', argv=None, threads=None, state='S'):
        pid = str(pid)
        directory = self.root / 'proc' / pid
        directory.mkdir(parents=True)
        (directory / 'exe').symlink_to(exe)
        (directory / 'cmdline').write_bytes(b'\0'.join(os.fsencode(v) for v in (argv or [exe])) + b'\0')
        (directory / 'status').write_text('State:\t%s\nCpus_allowed_list:\t0-7\n' % state)
        for tid, cpus in (threads or {pid: '0-7', str(int(pid) + 1): '0-1'}).items():
            status = directory / 'task' / str(tid) / 'status'
            status.parent.mkdir(parents=True)
            status.write_text('State:\tS\nCpus_allowed_list:\t%s\n' % cpus)
        return directory

    def collect(self, code=None):
        code = code or self.code
        with patch.dict(code, Path=self.fixture.mapped_path):
            return code['collect'](self.args)

    def run_main(self, argv, code=None):
        code = code or self.code
        out = io.StringIO()
        with patch.dict(code, Path=self.fixture.mapped_path), patch.object(sys, 'argv', ['cpuset-detect'] + argv), contextlib.redirect_stdout(out):
            rc = code['main']()
        return rc, out.getvalue()

    def test_flagfile_process_reserves_worker_cpus_in_both_detectors(self):
        config = self.root / 'cache.conf'
        config.write_text('--pin_cpu=true\n--cpuset=0-1\n')
        self.process(argv=['/opt/other/dingo-cache', '--flagfile', str(config)])
        for component in ['dingo_cache', 'dingo_client']:
            with self.subTest(component=component):
                code = fixtures.embedded('roles/%s/files/cpuset-detect.sh' % component, 'PYCPU')
                online, nodes, siblings, claims, _ = self.collect(code)
                self.assertEqual(set(claims), {0, 1})
                self.assertTrue(any('900004' in reason for reason in claims[0]))
                node, selected, _ = code['choose'](nodes, siblings, set(claims), 2, 0)
                self.assertEqual((node, selected), (1, [4, 6]))

    def test_flagfile_edits_and_removal_do_not_replace_running_affinity(self):
        config = self.root / 'cache.conf'; config.write_text('--cpuset=6-7\n')
        self.process(argv=['/opt/other/dingo-cache', '--flagfile=' + str(config)])
        self.assertEqual(set(self.collect()[3]), {0, 1})
        config.unlink()
        self.assertEqual(set(self.collect()[3]), {0, 1})

    def test_live_affinity_and_explicit_argv_are_combined(self):
        self.process(exe='/opt/other/dingo-client', argv=['/opt/other/dingo-client', 'mds://example/fs', '/mount', '--cpuset', '4-5'])
        self.assertEqual(set(self.collect()[3]), {0, 1, 4, 5})

    def test_client_self_reuse_does_not_hide_another_client_threads(self):
        self.args.self_exe = '/opt/current/dingo-client'
        self.process(exe='/opt/other/dingo-client', argv=['/opt/other/dingo-client', 'mds://example/fs', '/mount'])
        self.process(pid=900010, exe=self.args.self_exe, threads={900010: '0-7', 900011: '4-5'})
        self.assertEqual(set(self.collect()[3]), {0, 1})
        rc, output = self.run_main(['--no-busy', '-c', '4-5', '--self-exe', self.args.self_exe])
        self.assertEqual(rc, 0, output)

    def test_program_text_and_arguments_after_double_dash_are_not_flags(self):
        self.process(argv=['/opt/other/app', 'print("--cpuset=4-5")', '--', '--cpuset=6-7'])
        self.assertEqual(set(self.collect()[3]), {0, 1})

    def test_all_explicit_reservations_are_conservative(self):
        self.assertEqual(self.code['declared_cpus'](['cache', '-cpuset=0-1', '--cpuset', '4-5']), {0, 1, 4, 5})

    def test_manual_validation_rejects_the_claimed_smt_sibling(self):
        self.process(threads={900003: '0-7', 900004: '0'})
        rc, output = self.run_main(['--no-busy', '-c', '1', '--self-exe', '/opt/self/dingo-cache'])
        self.assertEqual(rc, 1)
        self.assertIn('CONFLICT 1 SMT_CPUS=0', output)
        self.assertIn('tid', output)
        self.assertIn('NODES=0', output)

    def test_same_instance_can_reuse_its_threads_but_duplicate_self_is_unknown(self):
        self.process(exe=self.args.self_exe + ' (deleted)')
        rc, output = self.run_main(['--no-busy', '-c', '0-1', '--self-exe', self.args.self_exe])
        self.assertEqual(rc, 0, output)
        self.process(pid=900010, exe=self.args.self_exe)
        with self.assertRaisesRegex(ValueError, 'multiple instances'):
            self.collect()

    def test_thread_exit_keeps_other_threads_claims(self):
        process = self.process(threads={900003: '0-7', 900004: '0', 900005: '2'})
        original = Path.read_text
        def racing_read(path, *args, **kwargs):
            if path == process / 'task/900005/status':
                shutil.rmtree(path.parent)
                raise FileNotFoundError(str(path))
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', racing_read):
            self.assertEqual(set(self.collect()[3]), {0})

    def test_process_exit_is_tolerated_but_missing_live_task_status_is_not(self):
        process = self.process()
        shutil.rmtree(process / 'task')
        with self.assertRaisesRegex(ValueError, 'live pid'):
            self.collect()
        (process / 'status').write_text('State:\tZ\nCpus_allowed_list:\t0-7\n')
        self.assertEqual(self.collect()[3], {})

    def test_exited_leader_with_remaining_workers_is_not_treated_as_free(self):
        process = self.process()
        (process / 'cmdline').write_bytes(b'')
        (process / 'status').write_text('State:\tZ\nThreads:\t2\n')
        with self.assertRaisesRegex(ValueError, 'live worker threads'):
            self.collect()
        (process / 'status').write_text('State:\tZ\nThreads:\t1\n')
        self.assertEqual(self.collect()[3], {})

    def test_unreadable_or_malformed_live_thread_is_unknown(self):
        process = self.process()
        status = process / 'task/900004/status'
        original = Path.read_text
        def denied(path, *args, **kwargs):
            if path == status: raise PermissionError('fixture affinity unavailable')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', denied), self.assertRaises(PermissionError):
            self.collect()
        status.write_text('State:\tS\n')
        with self.assertRaisesRegex(ValueError, 'missing CPU affinity'):
            self.collect()
        status.write_text('Cpus_allowed_list:\t\n')
        with self.assertRaisesRegex(ValueError, 'empty online affinity'):
            self.collect()

    def test_live_ownership_failure_exits_three_even_with_force(self):
        process = self.process()
        shutil.rmtree(process / 'task')
        source = (REPO / 'roles/dingo_cache/files/cpuset-detect.sh').read_text()
        entry = "if __name__ == '__main__':" + source.split("if __name__ == '__main__':", 1)[1].rsplit('\nPYCPU', 1)[0]
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(self.code, Path=self.fixture.mapped_path, __name__='__main__'), \
                patch.object(sys, 'argv', ['cpuset-detect', '--no-busy', '--force']), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
                self.assertRaises(SystemExit) as result:
            exec(compile(entry, 'cpuset-entry', 'exec'), self.code)
        self.assertEqual(result.exception.code, 3)
        self.assertIn('CPU detection failed', errors.getvalue())
        self.assertNotIn('SUGGEST=', output.getvalue())
        self.assertNotIn('FORCED=', output.getvalue())

    def test_malformed_kubelet_structure_is_unknown_not_a_forceable_conflict(self):
        self.fixture.write('/var/lib/kubelet/cpu_manager_state', '{"entries": []}')
        source = (REPO / 'roles/dingo_cache/files/cpuset-detect.sh').read_text()
        entry = "if __name__ == '__main__':" + source.split("if __name__ == '__main__':", 1)[1].rsplit('\nPYCPU', 1)[0]
        with patch.dict(self.code, Path=self.fixture.mapped_path, __name__='__main__'), \
                patch.object(sys, 'argv', ['cpuset-detect', '--no-busy', '--force']), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
            exec(compile(entry, 'cpuset-entry', 'exec'), self.code)
        self.assertEqual(result.exception.code, 3)

    def test_force_is_opt_in_and_excludes_smt_siblings_of_assigned_cores(self):
        self.process(threads={900003: '0-7', 900004: '0', 900005: '2', 900006: '4', 900007: '6'})
        for force in [False, True]:
            argv = ['--no-busy', '-s', '1', '-n', '0', '-x', '0', '--self-exe', self.args.self_exe]
            rc, output = self.run_main(argv + (['--force'] if force else []))
            self.assertEqual(rc, 2)
            self.assertIn('SUGGEST=NONE', output)
            self.assertIn('FORCED=' + ('2' if force else 'NONE'), output)

    def test_thread_diagnostics_are_bounded(self):
        self.process(threads=dict([(900003, '0-7')] + [(tid, '0') for tid in range(900004, 900104)]))
        claims = self.collect()[3]
        self.assertEqual(len(claims[0]), 1)
        reason = next(iter(claims[0]))
        self.assertIn('(+96 more)', reason)
        self.assertLess(len(reason), 140)


@unittest.skipUnless(sys.platform.startswith('linux') and hasattr(os, 'sched_setaffinity'), 'requires Linux thread affinity')
class LinuxThreadIntegrationTests(unittest.TestCase):
    def test_real_worker_affinity_is_seen_without_a_cpuset_argument(self):
        code = fixtures.embedded('roles/dingo_cache/files/cpuset-detect.sh', 'PYCPU')
        allowed = sorted(os.sched_getaffinity(0))
        if len(allowed) < 2: self.skipTest('needs at least two allowed CPUs')
        child = subprocess.Popen([sys.executable, '-u', '-c', '''import os,threading,json,sys
cpu=min(os.sched_getaffinity(0))
def worker():
 os.sched_setaffinity(0,{cpu})
 print(json.dumps({'pid':os.getpid(),'tid':threading.get_native_id(),'cpu':cpu}),flush=True)
 sys.stdin.read()
t=threading.Thread(target=worker);t.start();t.join()
'''], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            identity = json.loads(child.stdout.readline())
            process = Path('/proc') / str(identity['pid'])
            owners = code['thread_claims'](process, set(allowed), len(allowed))
            self.assertIn(identity['cpu'], owners)
            self.assertIn(str(identity['tid']), owners[identity['cpu']][1])
            command = (process / 'cmdline').read_bytes()
            self.assertNotIn(b'--cpuset', command)
        finally:
            child.communicate('', timeout=10)
            self.assertEqual(child.returncode, 0)


if __name__ == '__main__': unittest.main()
