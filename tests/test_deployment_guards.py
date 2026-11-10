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
                    result = self.run_tasks(tasks, {'rqlite_node_dir': wanted, 'rqlite_download_checksum': 'sha256:' + 'a' * 64, 'rqlite_auth_users': [], 'rqlite_service_user': 'root', 'rqlite_allow_insecure': True})
                    self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)

    def test_cache_health_requires_service_port_and_address(self):
        task = yaml.safe_load((REPO / 'playbooks/cache_99_status.yml').read_text())[0]['tasks'][-1]
        for active, failed, address, success in [('active', False, '127.0.0.1', True),
                                                ('inactive', False, '127.0.0.1', False),
                                                ('active', True, '127.0.0.1', False),
                                                ('active', False, '', False)]:
            result = self.run_tasks([task], {'service_info': {'status': {'ActiveState': active}},
                                            'listen_ip_conf': {'stdout': address}, 'port_check': {'failed': failed}})
            self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)

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
