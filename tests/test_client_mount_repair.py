"""Disconnected mount recovery and its read-only entry point."""
import contextlib
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml


REPO = Path(__file__).resolve().parents[1]
SCRIPT = Path('roles/common/files/dingofs_repair_stale_mounts.py')
PLAYBOOK = Path('playbooks/client_09_repair_stale_mounts.yml')
spec = importlib.util.spec_from_file_location('mount_repair', REPO / SCRIPT)
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class RepairTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.proc = Path(directory.name)
        self.process = self.proc / '123'
        (self.process / 'ns').mkdir(parents=True)
        (self.process / 'stat').write_text('123 (trainer (main)) S ' + '0 ' * 18 + '4321\n')
        (self.process / 'ns/mnt').symlink_to('mnt:[1234]')
        (self.process / 'ns/user').symlink_to('user:[12]')
        (self.proc / 'self/ns').mkdir(parents=True)
        (self.proc / 'self/ns/user').symlink_to('user:[12]')
        (self.proc / 'self/ns/mnt').symlink_to('mnt:[host]')
        self.host_path = '/mnt/public-fs'
        self.source = self.host_path + '/my models'
        self.destination = '/root/my models'
        self.host = repair.Mount(42, 1, '0:156', '/', self.host_path, frozenset(('rw', 'nodev')),
                                 ('shared:4',), 'fuse.dingofs', 'DingoFS:public', frozenset(('rw',)))
        self.root_record = '2 1 8:1 / / rw - ext4 /dev/root rw\n'
        self.old_record = ('20 2 0:222 /my\\040models /root/my\\040models ro,nosuid - '
                           'fuse.dingofs DingoFS:public rw\n')
        self.write_mounts()

    def write_mounts(self, old=None, extra=''):
        (self.process / 'mountinfo').write_text(self.root_record + (old or self.old_record) + extra)

    def inspect(self, error=errno.ENOTCONN, users=()):
        with patch.object(repair, 'PROC', self.proc), \
                patch.object(repair, 'probe_path', return_value={'errno': error}), \
                patch.object(repair, 'require_readable'), \
                patch.object(repair, 'handle_users', return_value=list(users)):
            return repair.inspect_reference(self.host, [self.host], 123, self.source, self.destination)

    def test_disconnected_read_only_subdirectory_is_repairable(self):
        report, plan = self.inspect()
        self.assertEqual((report['state'], report['result']), ('STALE', 'repairable'))
        self.assertEqual(plan.old.root, '/my models')
        self.assertEqual(plan.old.id, 20)
        self.assertEqual(plan.source, '/mnt/public-fs/my models')
        self.assertEqual(plan.destination, '/root/my models')
        attrs = repair.mount_attr(plan.host, plan.old)
        self.assertEqual(attrs.attr_set, 0x1 | 0x2 | 0x4)
        self.assertEqual(attrs.attr_clr, 0)
        self.assertEqual(attrs.propagation, 1 << 18)
        self.write_mounts(self.old_record.replace('ro,nosuid', 'rw,nosuid'))
        report, plan = self.inspect()
        self.assertFalse(report['readonly'])
        self.assertEqual(repair.mount_attr(plan.host, plan.old).attr_set, 0x2 | 0x4)

    def test_source_probe_rejects_an_intermediate_symlink(self):
        root = self.proc.resolve()
        original = root / 'export/data'
        original.mkdir(parents=True)
        (root / 'alias').symlink_to(original.parent)
        info = original.stat()
        device = '{}:{}'.format(os.major(info.st_dev), os.minor(info.st_dev))
        repair.require_readable(original, device, source_path=True)
        with self.assertRaisesRegex(ValueError, 'resolves through a symlink'):
            repair.require_readable(root / 'alias/data', device, source_path=True)

    def test_live_and_unconfirmed_disconnect_are_never_repairable(self):
        self.write_mounts(self.old_record.replace('0:222', '0:156'))
        report, plan = self.inspect()
        self.assertEqual((report['state'], report['result']), ('LIVE', 'current'))
        self.assertIsNone(plan)
        self.write_mounts()
        for error in (None, errno.EIO, errno.ENOENT):
            with self.subTest(error=error):
                report, plan = self.inspect(error)
                self.assertEqual(report['state'], 'UNKNOWN')
                self.assertIsNone(plan)

    def test_wrong_root_stacked_or_nested_mount_rejects_repair(self):
        self.write_mounts(self.old_record.replace('/my\\040models ', '/wrong '))
        report, plan = self.inspect()
        self.assertEqual(report['state'], 'UNKNOWN')
        self.assertIsNone(plan)
        self.write_mounts(extra=self.old_record.replace('20 2', '21 20'))
        report, plan = self.inspect()
        self.assertIn('stacked', report['reason'])
        self.assertIsNone(plan)
        self.write_mounts(extra='21 20 0:33 / /root/my\\040models/child rw - tmpfs tmpfs rw\n')
        report, plan = self.inspect()
        self.assertEqual((report['state'], report['result']), ('STALE', 'skipped'))
        self.assertIn('child mounts', report['reason'])
        self.assertIsNone(plan)

    def test_shared_target_or_parent_and_retained_handles_are_skipped(self):
        self.write_mounts(self.old_record.replace(' - ', ' master:41 - '))
        report, plan = self.inspect()
        self.assertIn('private propagation', report['reason'])
        self.assertIsNone(plan)
        root_record = self.root_record
        self.root_record = root_record.replace(' - ', ' shared:400 - ')
        self.write_mounts()
        report, plan = self.inspect()
        self.assertIn('private propagation', report['reason'])
        self.assertIsNone(plan)
        self.root_record = root_record
        self.write_mounts()
        for kind in ('fd', 'cwd', 'mmap'):
            with self.subTest(kind=kind):
                user = {'pid': 321, 'kind': kind}
                report, plan = self.inspect(users=[user])
                self.assertEqual((report['state'], report['result']), ('STALE', 'skipped'))
                self.assertEqual(report['users'], [user])
                self.assertIsNone(plan)

    def test_scan_finds_mount_ids_for_cwd_fds_and_alias_mappings(self):
        task = self.process / 'task/123'
        (task / 'ns').mkdir(parents=True)
        (task / 'ns/mnt').symlink_to('mnt:[1234]')
        (task / 'cwd').mkdir()
        (task / 'root').mkdir()
        (task / 'fdinfo').mkdir()
        (task / 'fdinfo/4').write_text('pos:\t0\nmnt_id:\t21\n')
        (self.process / 'maps').write_text('1-2 r--p 0000 00:de 42 /alias/mapped\n')
        cwd_inode = (task / 'cwd').stat().st_ino
        alias = self.old_record.replace('20 2', '21 2').replace('/root/my\\040models', '/alias')
        self.write_mounts(extra=alias)
        with patch.object(repair, 'PROC', self.proc), \
                patch.object(os, 'O_PATH', os.O_RDONLY, create=True), \
                patch.object(repair, 'fd_mount_id', side_effect=lambda fd: 21 if os.fstat(fd).st_ino == cwd_inode else 2):
            users = repair.handle_users(123, 'mnt:[1234]', '0:222', repair.mountinfo(self.process / 'mountinfo'))
        self.assertEqual({(user['pid'], user['kind']) for user in users},
                         {(123, 'cwd'), (123, 'fd'), (123, 'mmap')})

    def test_explicit_apply_is_scoped_and_unresolved_pod_never_mutates(self):
        _, plan = self.inspect()
        args = SimpleNamespace(mount_point=self.host_path, fsname='public',
                               pod='cci-job/example', all_namespaces=False, apply=False)
        mount = (self.source, self.destination)
        matches = [('runtime-id', args.pod, 'worker')]
        with patch.object(repair, 'current_mount', return_value=(self.host, [self.host])), \
                patch.object(repair, 'require_readable'), \
                patch.object(repair, 'container_targets', return_value=matches) as targets, \
                patch.object(repair, 'container_mounts', return_value=(123, [mount])) as mounts, \
                patch.object(repair, 'inspect_reference', return_value=(dict(state='STALE', result='repairable'), plan)) as inspect, \
                patch.object(repair, 'apply_repair', return_value=555) as apply:
            report = dict(changed=False, findings=[])
            repair.scan(args, report)
            self.assertEqual(report['findings'][0]['result'], 'repairable')
            self.assertFalse(report['changed'])
            targets.assert_called_once_with('cci-job/example', False)
            apply.assert_not_called()
            args.apply = True
            report = dict(changed=False, findings=[])
            repair.scan(args, report)
            apply.assert_called_once_with('runtime-id', plan)
            self.assertTrue(report['changed'])
            self.assertEqual(report['findings'][0]['new_mount_id'], 555)
            apply.reset_mock()
            mounts.return_value = (123, [mount, (self.host_path, '/uncertain')])
            inspect.side_effect = [inspect.return_value, (dict(state='UNKNOWN', result='skipped'), None)]
            with self.assertRaisesRegex(ValueError, 'unresolved mount reference'):
                repair.scan(args, dict(changed=False, findings=[]))
            apply.assert_not_called()
        out = io.StringIO()
        with patch.object(sys, 'platform', 'linux'), patch.object(os, 'geteuid', return_value=0), \
                contextlib.redirect_stdout(out):
            code = repair.main(['--mount-point', self.host_path, '--apply', '--json'])
        self.assertEqual(code, 2)
        self.assertIn('--apply requires --pod', json.loads(out.getvalue())['error'])


@unittest.skipUnless(shutil.which('ansible-playbook'), 'requires Ansible')
class RepairPlaybookTests(unittest.TestCase):
    def test_only_selected_mount_is_inspected_and_check_mode_prevents_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / PLAYBOOK).parent.mkdir()
            shutil.copy(REPO / PLAYBOOK, root / PLAYBOOK)
            (root / SCRIPT).parent.mkdir(parents=True)
            changes = root / 'mutated'
            (root / SCRIPT).write_text('''import json, pathlib, sys
args = sys.argv
path = args[args.index('--mount-point') + 1]
changed = '--apply' in args
if changed:
    pathlib.Path({!r}).write_text(path)
print(json.dumps({{'changed': changed, 'inspected_path': path}}))
'''.format(str(changes)))
            inv = root / 'inventory.yml'
            inv.write_text(yaml.safe_dump(dict(client_servers=dict(hosts=dict(fixture=dict(
                ansible_connection='local', ansible_python_interpreter=sys.executable,
                dingo_clients=[dict(fsname='public', mount_point='/fixture/public'),
                               dict(fsname='private', mount_point='/fixture/private')]))))))
            env = dict(os.environ, ANSIBLE_CONFIG=str(REPO / 'ansible.cfg'),
                       ANSIBLE_FILTER_PLUGINS=str(REPO / 'filter_plugins'), ANSIBLE_STDOUT_CALLBACK='default')
            command = ['ansible-playbook', '-i', str(inv), str(root / PLAYBOOK), '--limit', 'fixture',
                       '-e', 'dingo_client_only=public']
            for options in ([], ['--check', '-e', 'dingo_client_repair_apply=true']):
                with self.subTest(options=options):
                    result = subprocess.run(command + options, cwd=REPO, env=env, text=True,
                                            capture_output=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn('inspected_path: /fixture/public', result.stdout)
                    self.assertNotIn('/fixture/private', result.stdout)
                    self.assertIn('changed=0', result.stdout)
                    self.assertFalse(changes.exists())
            result = subprocess.run(command + ['-e', 'dingo_client_repair_apply=true'], cwd=REPO, env=env,
                                    text=True, capture_output=True, timeout=20)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Set dingo_client_repair_pod', result.stdout)
            self.assertFalse(changes.exists())


if __name__ == '__main__':
    unittest.main()
