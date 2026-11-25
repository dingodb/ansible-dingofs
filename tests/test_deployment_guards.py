"""Run preflight and final health assertions without contacting remote hosts."""
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


@unittest.skipUnless(shutil.which('ansible-playbook'), 'requires Ansible')
class GuardTests(unittest.TestCase):
    def run_tasks(self, tasks, variables):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'play.yml'
            variables = dict(variables, ansible_connection='local', ansible_become=False,
                             ansible_python_interpreter=sys.executable)
            path.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False,
                                             'vars': variables, 'tasks': tasks}]))
            return subprocess.run(['ansible-playbook', '-i', 'localhost,', str(path)], cwd=REPO,
                                  env=dict(os.environ, ANSIBLE_CONFIG=str(REPO / 'ansible.cfg'),
                                           ANSIBLE_ROLES_PATH=str(REPO / 'roles')),
                                  capture_output=True, text=True, timeout=30)

    def test_image_version_requires_matching_tag_or_explicit_build_metadata(self):
        tasks = [{'import_role': {'name': 'dingofs_cluster', 'tasks_from': 'validate_image'}}]
        for image, metadata, success in [('registry/cache:v5.1-build', '', False),
                                         ('registry/cache:v5.2-build', '', True),
                                         ('registry/cache@sha256:abc', '', False),
                                         ('registry/cache@sha256:abc', 'v5.2', True)]:
            with self.subTest(image=image, metadata=metadata):
                result = self.run_tasks(tasks, {'dingofs_site_version': 'v5.2',
                                                'dingofs_container_image': image,
                                                'dingofs_container_version': metadata})
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)

    def test_rqlite_blocks_relative_and_mismatched_existing_data_paths(self):
        source = (REPO / 'roles/rqlite/tasks/main.yml').read_text().split('# --- Download')[0]
        with tempfile.TemporaryDirectory() as directory:
            unit = Path(directory) / 'rqlite.service'
            source = source.replace('/etc/systemd/system/rqlite.service', str(unit))
            tasks = yaml.safe_load(source)
            for old_path, wanted, success in [(None, '/root/rqlite/node', True),
                                               ('~/rqlite/node', '/root/rqlite/node', False),
                                               ('/actual/data', '/root/rqlite/node', False),
                                               ('/actual/data', '/actual/data', True),
                                               (None, '~/rqlite/node', False)]:
                with self.subTest(old_path=old_path, wanted=wanted):
                    unit.unlink(missing_ok=True)
                    if old_path:
                        unit.write_text('ExecStart=/usr/local/bin/rqlited -node-id 1 ' + old_path + '\n')
                    result = self.run_tasks(tasks, {'rqlite_node_dir': wanted, 'rqlite_download_checksum': 'sha256:' + 'a' * 64})
                    self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)

    def test_cache_health_requires_service_port_address_and_clean_journal(self):
        task = yaml.safe_load((REPO / 'playbooks/cache_99_status.yml').read_text())[0]['tasks'][-1]
        for active, failed, address, errors, success in [('active', False, '127.0.0.1', 0, True),
                                                        ('inactive', False, '127.0.0.1', 0, False),
                                                        ('active', True, '127.0.0.1', 0, False),
                                                        ('active', False, '', 0, False),
                                                        ('active', False, '127.0.0.1', 28, False)]:
            with self.subTest(active=active, failed=failed, address=address, errors=errors):
                result = self.run_tasks([task], {
                    'dingo_cache_service_name': 'dingo-cache-v5.2', 'dingo_cache_listen_port': 11100,
                    'service_info': {'status': {'ActiveState': active}}, 'listen_ip_conf': {'stdout': address},
                    'port_check': {'failed': failed}, 'cache_journal': {
                        'lookback_hours': 72, 'error_count': errors,
                        'samples': ['queue pair to rtr', 'another error'] if errors else []}})
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                self.assertIn('{} journal error(s)'.format(errors), result.stdout)
                if errors:
                    self.assertRegex(result.stdout, r'queue pair to rtr\n\s*another error')

    def test_cache_journal_scan_scopes_to_current_start_and_supports_minutes(self):
        tasks = yaml.safe_load((REPO / 'playbooks/cache_99_status.yml').read_text())[0]['tasks'][-2:]
        invocation = '0123456789abcdef0123456789abcdef'
        cases = [
            (
                'active_clean_default',
                {'ActiveState': 'active', 'NRestarts': '0', 'InvocationID': invocation},
                {},
                ['--no-pager', '--output=json', '--all', '--since', '72 hours ago',
                 '--output-fields=MESSAGE,PRIORITY',
                 '_SYSTEMD_INVOCATION_ID=' + invocation, '+', 'INVOCATION_ID=' + invocation],
                '0 journal error(s) (current start, last 72h).',
            ),
            (
                'active_clean_minutes',
                {'ActiveState': 'active', 'NRestarts': '0', 'InvocationID': invocation},
                {'dingo_cache_status_log_minutes': '15'},
                ['--no-pager', '--output=json', '--all', '--since', '15 minutes ago',
                 '--output-fields=MESSAGE,PRIORITY',
                 '_SYSTEMD_INVOCATION_ID=' + invocation, '+', 'INVOCATION_ID=' + invocation],
                '0 journal error(s) (current start, last 15m).',
            ),
            (
                'auto_restarted_falls_back_to_unit',
                {'ActiveState': 'active', 'NRestarts': '1', 'InvocationID': invocation},
                {'dingo_cache_status_log_minutes': '30'},
                ['--no-pager', '--output=json', '--all', '--unit', 'dingo-cache-v5.2.service',
                 '--since', '30 minutes ago', '--output-fields=MESSAGE,PRIORITY'],
                '0 journal error(s) (last 30m).',
            ),
            (
                'current_start_only_disabled',
                {'ActiveState': 'active', 'NRestarts': '0', 'InvocationID': invocation},
                {'dingo_cache_status_current_start_only': 'false', 'dingo_cache_status_log_hours': '2'},
                ['--no-pager', '--output=json', '--all', '--unit', 'dingo-cache-v5.2.service',
                 '--since', '2 hours ago', '--output-fields=MESSAGE,PRIORITY'],
                '0 journal error(s) (last 2h).',
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory) / 'bin'
            bin_dir.mkdir()
            recorded = Path(directory) / 'args.txt'
            journalctl = bin_dir / 'journalctl'
            journalctl.write_text('''#!/bin/sh
if [ "$1" = --help ]; then echo --output-fields; exit 0; fi
printf '%s\\n' "$@" > "{}"
'''.format(recorded))
            journalctl.chmod(0o755)
            old_path = os.environ.get('PATH', '')
            os.environ['PATH'] = '{}:{}'.format(bin_dir, old_path)
            try:
                for name, status, extra_vars, expected_args, expected_msg in cases:
                    with self.subTest(case=name):
                        recorded.unlink(missing_ok=True)
                        variables = {
                            'dingo_cache_service_name': 'dingo-cache-v5.2',
                            'dingo_cache_listen_port': 11100,
                            'service_info': {'status': dict(status, Id='dingo-cache-v5.2.service')},
                            'listen_ip_conf': {'stdout': '127.0.0.1'},
                            'port_check': {'failed': False},
                        }
                        variables.update(extra_vars)
                        result = self.run_tasks(tasks, variables)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(recorded.read_text().splitlines(), expected_args)
                        self.assertIn(expected_msg, result.stdout)
            finally:
                os.environ['PATH'] = old_path

    def test_client_health_requires_the_intended_fuse_filesystem(self):
        task = yaml.safe_load((REPO / 'playbooks/client_99_status.yml').read_text())[0]['tasks'][-1]
        for kind, source, success in [('fuse.dingofs', 'DingoFS:work', True),
                                      ('fuse', 'DingoFS:work', True),
                                      ('ext4', 'DingoFS:work', False),
                                      ('fuse.dingofs', 'DingoFS:other', False)]:
            value = {'rc': 0, 'stdout': json.dumps({'filesystems': [{'fstype': kind, 'source': source}]}),
                     'item': {'mount_point': '/fixture', 'fsname': 'work'}}
            result = self.run_tasks([task], {'_client_mount_types': {'results': [value]}})
            self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
