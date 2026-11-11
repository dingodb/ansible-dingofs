"""Directory storage is explicit and the shared LVM skip never runs disk tools."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

REPO = Path(__file__).resolve().parents[1]
ROLE = REPO / 'roles/dingo_cache'


@unittest.skipUnless(shutil.which('ansible-playbook'), 'requires Ansible')
class DirectoryCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.marker = self.root / 'disk-tool-called'
        for name in ['vgs', 'pvcreate', 'vgcreate', 'lvcreate', 'mkfs.xfs', 'mount', 'wipefs', 'parted']:
            path = self.bin / name
            path.write_text('#!/bin/sh\nprintf "%s\\n" ' + name + ' >> ' + str(self.marker) + '\nexit 70\n')
            path.chmod(0o755)
        self.inv = self.root / 'hosts.yml'
        self.inv.write_text(yaml.safe_dump({'all': {'children': {
            'cache_servers': {'hosts': {'fixture': {}}},
            'client_servers': {'hosts': {'fixture': {}}},
        }}}))
        self.variables = {
            'ansible_connection': 'local', 'ansible_become': False,
            'ansible_python_interpreter': sys.executable,
            'dingo_cache_storage_mode': 'directory',
            'dingo_cache_cache_dirs': str(self.root / 'cache'),
            'dingo_cache_log_dir': str(self.root / 'logs'),
            'dingo_cache_cache_size_mb': 4,
            'dingo_cache_free_space_ratio': 0,
        }

    def run_play(self, tasks=None, overrides=None, playbook=None):
        variables = dict(self.variables, **(overrides or {}))
        varfile = self.root / 'vars.json'
        varfile.write_text(json.dumps(variables))
        if playbook is None:
            playbook = self.root / 'play.yml'
            playbook.write_text(yaml.safe_dump([{
                'hosts': 'fixture', 'gather_facts': False,
                'vars_files': [str(ROLE / 'defaults/main.yml')], 'tasks': tasks,
            }]))
        return subprocess.run(['ansible-playbook', '-i', str(self.inv), str(playbook), '-e', '@' + str(varfile)],
                              cwd=REPO, capture_output=True, text=True, timeout=40,
                              env=dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                                       ANSIBLE_ROLES_PATH=str(REPO / 'roles'),
                                       ANSIBLE_STDOUT_CALLBACK='default', ANSIBLE_NOCOLOR='1'))

    def storage_tasks(self):
        main = yaml.safe_load((ROLE / 'tasks/main.yml').read_text())
        start = next(i for i,t in enumerate(main) if t.get('name') == 'Resolve effective data/log prefixes')
        end = next(i for i,t in enumerate(main) if t.get('name') == 'Create dingo-cache base directory')
        return [{'include_role': {'name': 'dingo_cache', 'tasks_from': 'directory_storage'}}] + main[start:end]

    def test_directory_mode_preserves_capacity_and_paths_without_mount_discovery(self):
        output = self.root / 'result.json'
        tasks = self.storage_tasks() + [{'copy': {
            'content': "{{ {'dirs': dingo_cache_cache_dirs, 'size': dingo_cache_cache_size_mb, 'logs': dingo_cache_log_dir} | to_json }}",
            'dest': str(output)}}]
        result = self.run_play(tasks)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(output.read_text()), {'dirs': str(self.root / 'cache'), 'size': 4, 'logs': str(self.root / 'logs')})
        self.assertFalse(self.marker.exists(), 'directory mode invoked mount or an LVM tool')
        self.assertFalse((self.root / 'cache').exists(), 'validation created the cache directory')

    def test_directory_mode_rejects_multiple_paths_invalid_sizes_and_traversal(self):
        tasks = [{'include_role': {'name': 'dingo_cache', 'tasks_from': 'directory_storage'}}]
        for overrides in [{'dingo_cache_cache_dirs': '/a,/b'},
                          {'dingo_cache_cache_dirs': '/tmp/../root'},
                          {'dingo_cache_cache_size_mb': 0},
                          {'dingo_cache_cache_size_mb': '1.5'},
                          {'dingo_cache_free_space_ratio': 'invalid'}]:
            with self.subTest(overrides=overrides):
                result = self.run_play(tasks, overrides)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('assertion:', result.stdout + result.stderr)
                self.assertFalse(self.marker.exists())

    def test_default_mounted_mode_still_rejects_missing_mounts(self):
        tasks = self.storage_tasks()[1:]
        result = self.run_play(tasks, {'dingo_cache_storage_mode': 'mounted'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('No cache disks found', result.stdout + result.stderr)
        self.assertTrue(self.marker.exists())

    def test_skip_parameter_and_directory_mode_bypass_lvm_for_cache_and_clients(self):
        playbook = REPO / 'playbooks/cache_02_lvm.yml'
        for target in ['cache_servers', 'client_servers']:
            for skip,mode in [(True,'mounted'),(False,'directory')]:
                with self.subTest(target=target,skip=skip,mode=mode):
                    self.marker.unlink(missing_ok=True)
                    result = self.run_play(overrides={'target_hosts': target, 'dingofs_skip_disk_setup': skip,
                                                      'dingo_cache_storage_mode': mode}, playbook=playbook)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertFalse(self.marker.exists(), 'skip invoked the LVM role')
        result = self.run_play(overrides={'dingofs_skip_disk_setup': False, 'dingo_cache_storage_mode': 'mounted'},
                               playbook=playbook)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.marker.exists(), 'default behavior silently skipped disk preparation')
        self.assertIn('LVM toolchain', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
