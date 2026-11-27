"""Client runtime evidence and restart-marker recovery against a procfs fixture."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from library import dingofs_client_runtime as runtime


class ClientRuntimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.base = root / 'client'
        self.binary = self.base / 'bin/dingo-client'
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(b'current client')
        self.launcher = self.base / 'dingofs-mount.sh'
        self.launcher.touch()
        self.marker = self.base / '.restart-required'
        self.marker.touch()
        self.fragment = root / 'client.service'
        self.fragment.touch()
        self.mount_point = '/fixture/public'
        self.proc = root / 'proc'
        self.proc.mkdir()
        boot = int(time.time())
        (self.proc / 'stat').write_text('btime {}\n'.format(boot))
        self.start_ns = (boot + 60) * 10**9
        ticks = 60 * os.sysconf('SC_CLK_TCK')
        main = self.make_process(101, '/bin/bash', ticks)
        children = main / 'task/101/children'
        children.parent.mkdir(parents=True)
        children.write_text('202\n')
        self.child = self.make_process(202, self.binary, ticks + 1)
        (self.child / 'cmdline').write_bytes(
            (str(self.binary) + '\0mds://127.0.0.1:6900/public\0' + self.mount_point + '\0').encode())
        self.unit = dict(ActiveState='active', SubState='running', LoadState='loaded', MainPID='101',
                         ExecStart='{ path=%s ; argv[]=%s ; }' % (self.launcher, self.launcher),
                         FragmentPath=str(self.fragment), DropInPaths='', NeedDaemonReload='no', InvocationID='a')
        self.mount = dict(id=991, fstype='fuse.dingofs', source='DingoFS:public')

    def make_process(self, pid, exe, ticks):
        process = self.proc / str(pid)
        process.mkdir()
        (process / 'exe').symlink_to(exe)
        (process / 'stat').write_text('{} (name) with space) S {} {}\n'.format(pid, '0 ' * 18, ticks))
        return process

    def run_probe(self, argv):
        if argv[0] == 'systemctl':
            output = ''.join('{}={}\n'.format(key, value) for key, value in self.unit.items())
            return subprocess.CompletedProcess(argv, 0, output, '')
        if argv[0] == 'findmnt':
            return subprocess.CompletedProcess(argv, 0, json.dumps({'filesystems': [self.mount]}), '')
        if argv[0] == 'cmp':
            equal = Path(argv[2]).read_bytes() == Path(argv[3]).read_bytes()
            return subprocess.CompletedProcess(argv, 0 if equal else 1, '', '')
        if argv == [self.child / 'exe', '--version']:
            return subprocess.CompletedProcess(argv, 1, 'v5.2, ci/cd build-v5.2:955d5b0 + release\n', '')
        raise AssertionError(argv)

    def inspect(self, reconcile=False):
        with patch.object(runtime, 'run', side_effect=self.run_probe):
            return runtime.inspect_client('client.service', self.base, self.mount_point, 'public',
                                          report_version=True, reconcile=reconcile, proc=self.proc)

    def test_status_reports_current_process_and_stale_marker_without_clearing(self):
        state = self.inspect()
        self.assertEqual(state['pid'], 202)
        self.assertIn('955d5b0', state['running_version'])
        self.assertEqual((state['binary_match'], state['config_match']), ('current', 'current'))
        self.assertEqual((state['restart_marker'], state['reconciliation']), ('present', 'clearable'))
        self.assertTrue(self.marker.exists())

    def test_reconcile_applied_marker_then_repeat(self):
        self.assertTrue(self.inspect(reconcile=True)['reconciled'])
        self.assertFalse(self.marker.exists())
        repeat = self.inspect(reconcile=True)
        self.assertEqual(repeat['reconciliation'], 'none')
        self.assertFalse(repeat['changed'])

    def test_old_deleted_executable_keeps_pending_marker(self):
        old = Path(str(self.binary) + ' (deleted)')
        old.write_bytes(b'old client')
        (self.child / 'exe').unlink()
        (self.child / 'exe').symlink_to(old)
        state = self.inspect(reconcile=True)
        self.assertEqual(state['binary_match'], 'stale')
        self.assertEqual(state['reconciliation'], 'pending')
        self.assertTrue(self.marker.exists())

    def test_newer_config_or_marker_stays_pending(self):
        newer = self.start_ns + 10**9
        os.utime(self.marker, ns=(newer, newer))
        state = self.inspect(reconcile=True)
        self.assertEqual(state['config_match'], 'current')
        self.assertEqual(state['reconciliation'], 'pending')
        self.assertTrue(self.marker.exists())
        os.utime(self.launcher, ns=(newer, newer))
        state = self.inspect(reconcile=True)
        self.assertEqual(state['config_match'], 'stale')
        self.assertTrue(self.marker.exists())

    def test_wrong_mount_or_pending_unit_reload_preserves_marker(self):
        self.mount['source'] = 'DingoFS:other'
        state = self.inspect(reconcile=True)
        self.assertEqual(state['mount_state'], 'different')
        self.assertTrue(self.marker.exists())
        self.mount['source'] = 'DingoFS:public'
        self.unit['NeedDaemonReload'] = 'yes'
        state = self.inspect(reconcile=True)
        self.assertEqual(state['config_match'], 'stale')
        self.assertTrue(self.marker.exists())

    def test_ambiguous_service_processes_preserve_marker(self):
        (self.proc / '101/task/101/children').write_text('202 203\n')
        self.make_process(203, self.binary, os.sysconf('SC_CLK_TCK') * 61)
        state = self.inspect(reconcile=True)
        self.assertEqual(state['binary_match'], 'unknown')
        self.assertTrue(self.marker.exists())

    def test_replaced_mount_during_reconciliation_preserves_marker(self):
        original = self.run_probe

        def probe(argv):
            result = original(argv)
            if argv[0] == 'findmnt':
                self.mount['id'] += 1
            return result

        self.run_probe = probe
        state = self.inspect(reconcile=True)
        self.assertEqual(state['reconciliation'], 'unknown')
        self.assertTrue(self.marker.exists())


if __name__ == '__main__':
    unittest.main()
