"""Run both Cache playbooks with local inventory and service fixtures."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


REPO = Path(__file__).resolve().parents[1]
DEPLOY = 'cache_05_deploy.yml'
UPGRADE = 'cache_06_upgrade.yml'


@unittest.skipUnless(shutil.which('ansible-playbook'), 'requires Ansible')
class CacheRolloutTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='cache-rollout-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.inventory = self.root / 'inventory' / 'hosts.yml'
        self.inventory.parent.mkdir()
        self.hosts = {'node{}'.format(index): {} for index in range(1, 7)}
        self.inventory_vars = dict(ansible_connection='local', ansible_become=False,
                                   ansible_python_interpreter=sys.executable)

    def run_playbook(self, name, *, extra=(), environment=None, group_vars=None,
                     directory_inventory=False, limit=None, preview=True):
        self.inventory.write_text(yaml.safe_dump({'cache_servers': {'hosts': self.hosts,
                                                                    'vars': self.inventory_vars}},
                                                         sort_keys=False))
        if group_vars is not None:
            shared = self.inventory.parent / 'group_vars' / 'all.yml'
            shared.parent.mkdir(exist_ok=True)
            shared.write_text(yaml.safe_dump(group_vars))
        inventory = self.inventory.parent if directory_inventory else self.inventory
        command = ['ansible-playbook', '-i', str(inventory), 'playbooks/' + name]
        if preview:
            command += ['--check', '--tags', 'cache_rollout_preflight']
        if limit:
            command += ['--limit', limit]
        for setting in extra:
            command += ['-e', setting]
        env = dict(os.environ, ANSIBLE_CONFIG=str(REPO / 'ansible.cfg'),
                   ANSIBLE_GATHERING='explicit', ANSIBLE_STDOUT_CALLBACK='default', ANSIBLE_NOCOLOR='1')
        for key in ('DINGO_CACHE_DEPLOY_SERIAL', 'DINGO_CACHE_UPGRADE_SERIAL'):
            env.pop(key, None)
        env.update(environment or {})
        return subprocess.run(command, cwd=REPO, env=env, capture_output=True, text=True, timeout=20)

    def batches(self, name, **kwargs):
        result = self.run_playbook(name, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return [int(size) for size in re.findall(r'Cache batch size=(\d+) hosts=', result.stdout)]

    def test_default_deployment_and_binary_upgrade_batch_sizes(self):
        self.assertEqual(self.batches(DEPLOY), [6])
        self.assertEqual(self.batches(UPGRADE), [1] * 6)

    def test_deploy_resolves_lists_and_override_precedence(self):
        site = {'dingo_cache_deploy_batch': [2, '50%']}
        self.assertEqual(self.batches(DEPLOY, group_vars=site, directory_inventory=True), [2, 3, 1])
        environment = {'DINGO_CACHE_DEPLOY_SERIAL': '[1, "50%", "100%"]'}
        self.assertEqual(self.batches(DEPLOY, environment=environment, group_vars=site), [1, 3, 2])
        self.assertEqual(self.batches(DEPLOY, extra=('dingo_cache_deploy_batch=50%',),
                                      environment=environment, group_vars=site, limit='node1,node2,node3,node4'), [2, 2])

    def test_upgrade_prefers_upgrade_override_within_each_precedence_level(self):
        self.assertEqual(self.batches(UPGRADE, group_vars={'dingo_cache_deploy_batch': 2}), [2, 2, 2])
        site = {'dingo_cache_upgrade_batch': '50%', 'dingo_cache_deploy_batch': 2}
        self.assertEqual(self.batches(UPGRADE, group_vars=site), [3, 3])
        environment = {'DINGO_CACHE_DEPLOY_SERIAL': '2', 'DINGO_CACHE_UPGRADE_SERIAL': '100%'}
        self.assertEqual(self.batches(UPGRADE, environment=environment, group_vars=site), [6])
        extra = ('{"dingo_cache_deploy_batch": [1, "100%"]}',)
        self.assertEqual(self.batches(UPGRADE, extra=extra, environment=environment, group_vars=site), [1, 5])
        self.assertEqual(self.batches(UPGRADE, extra=extra + ('dingo_cache_upgrade_batch=3',),
                                      environment=environment, group_vars=site), [3, 3])

    def test_inventory_serial_alias_warns_when_ignored(self):
        result = self.run_playbook(DEPLOY, group_vars={'dingo_cache_deploy_serial': 1})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('dingo_cache_deploy_serial and dingo_cache_upgrade_serial are ignored',
                      ' '.join(result.stdout.split()))
        self.assertIn('Cache batch size=6', result.stdout)

    def test_zero_batch_is_rejected_before_play_tasks(self):
        result = self.run_playbook(DEPLOY, extra=('dingo_cache_deploy_batch=0',))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Cache batch size must be a positive host count', result.stderr)
        self.assertNotIn('Cache batch size=6', result.stdout)

    def test_existing_only_checks_the_configured_binary_and_unit(self):
        self.hosts = {
            name: {'dingo_cache_base_dir': str(self.root / name), 'dingo_cache_service_name': name}
            for name in ('installed', 'missing_unit', 'fresh')
        }
        self.inventory_vars.update(dingo_cache_bin_name='dingo-cache',
                                   dingo_cache_bin_local_src=str(self.root / 'no-controller-package'))
        for name in ('installed', 'missing_unit'):
            binary = self.root / name / 'dingo-cache'
            binary.parent.mkdir()
            binary.write_text('existing Cache binary\n')
            binary.chmod(0o755)
        fakebin = self.root / 'bin'
        fakebin.mkdir()
        systemctl = fakebin / 'systemctl'
        systemctl.write_text('''#!/bin/sh
case "$*" in
  'show --property=LoadState --value -- installed.service') echo loaded ;;
  'show --property=LoadState --value -- missing_unit.service') echo not-found; exit 1 ;;
  *) exit 2 ;;
esac
''')
        systemctl.chmod(0o755)
        environment = {'PATH': str(fakebin) + os.pathsep + os.environ['PATH']}
        extra = ('dingo_cache_existing_only=true',)
        for playbook in (DEPLOY, UPGRADE):
            with self.subTest(playbook=playbook):
                preview = self.run_playbook(playbook, extra=extra, environment=environment)
                self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
                self.assertIn('EXISTING installed binary=present unit=loaded', preview.stdout)
                self.assertIn('SKIP missing_unit binary=present unit=not-found', preview.stdout)
                self.assertIn('SKIP fresh binary=missing_or_unusable unit=not_checked', preview.stdout)

                result = self.run_playbook(playbook, extra=extra, environment=environment,
                                           limit='missing_unit,fresh', preview=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse((self.root / 'fresh').exists())
                self.assertEqual(sorted(path.name for path in (self.root / 'missing_unit').iterdir()),
                                 ['dingo-cache'])


if __name__ == '__main__':
    unittest.main()
