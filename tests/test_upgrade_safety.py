"""Exercise real per-instance upgrade tasks with local binaries and fake services."""
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
class UpgradeTests(unittest.TestCase):
    def test_changed_instances_restart_once_and_identical_retry_does_not_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / 'library'
            library.mkdir()
            events = root / 'events'
            (library / 'systemd.py').write_text('''from ansible.module_utils.basic import AnsibleModule
import os
m = AnsibleModule(argument_spec=dict(name=dict(), state=dict()))
with open(os.environ['UPGRADE_EVENTS'], 'a') as f:
    f.write(m.params['name'] + '\\n')
m.exit_json(changed=True)
''')
            fakebin = root / 'fakebin'
            fakebin.mkdir()
            checker = root / 'check'
            checker.write_text('#!/bin/sh\nexit 0\n')
            checker.chmod(0o755)
            mountpoint = fakebin / 'mountpoint'
            mountpoint.write_text('#!/bin/sh\nexit 0\n')
            mountpoint.chmod(0o755)
            candidate = root / 'candidate'
            candidate.write_text('new')
            clients = []
            for name in ['a', 'b']:
                base = root / name
                (base / 'bin').mkdir(parents=True)
                (base / 'bin/dingo-client').write_text('old')
                clients.append(dict(fsname=name, base_dir=str(base), mount_point=str(base / 'mount')))
            variables = dict(ansible_connection='local', ansible_become=False,
                             ansible_python_interpreter=sys.executable,
                             dingo_client_upgrade_bin_src=str(candidate),
                             dingo_client_mount_check_script=str(checker),
                             dingo_client_upgrade_dry_run=False)
            tasks = [{'include_tasks': str(REPO / 'roles/dingo_client_upgrade/tasks/upgrade_instance.yml'),
                      'loop': clients, 'loop_control': {'loop_var': 'client_instance'}}]
            play = [{'hosts': 'localhost', 'gather_facts': False, 'vars': variables,
                     'environment': {'PATH': str(fakebin) + ':' + os.environ['PATH'], 'UPGRADE_EVENTS': str(events)},
                     'tasks': tasks}]
            play_path = root / 'play.yml'
            play_path.write_text(yaml.safe_dump(play))
            env = dict(os.environ, ANSIBLE_CONFIG=str(REPO / 'ansible.cfg'), ANSIBLE_LIBRARY=str(library))
            for attempt in range(2):
                result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(play_path)],
                                        cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(events.read_text().splitlines(), ['dingofs-mount-a', 'dingofs-mount-b'])
            # A busy instance must retain its current binary.
            checker.write_text('#!/bin/sh\nexit 1\n')
            candidate.write_text('next')
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(play_path)],
                                    cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((root / 'a/bin/dingo-client').read_text(), 'new')
            self.assertEqual(events.read_text().splitlines(), ['dingofs-mount-a', 'dingofs-mount-b'])
            # If usage becomes busy after the copy, a retry must still restart
            # the already-copied binary even though copy will report unchanged.
            counter = root / 'checks'
            checker.write_text('#!/bin/sh\nn=$(cat "' + str(counter) + '" 2>/dev/null || echo 0)\n'
                               + 'n=$((n+1)); echo "$n" > "' + str(counter) + '"\n'
                               + 'test "$n" -ne 2\n')
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(play_path)],
                                    cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((root / 'a/bin/dingo-client').read_text(), 'next')
            self.assertTrue((root / 'a/.restart-required').exists())
            checker.write_text('#!/bin/sh\nexit 0\n')
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(play_path)],
                                    cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(events.read_text().splitlines(), ['dingofs-mount-a', 'dingofs-mount-b'] * 2)
            self.assertFalse((root / 'a/.restart-required').exists())

@unittest.skipUnless(shutil.which('ansible-playbook'), 'requires Ansible')
class StartupTests(unittest.TestCase):
    def test_busy_or_failed_unmount_prevents_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'fakebin'
            fake.mkdir()
            events = root / 'unmounts'
            checker = root / 'check'
            checker.write_text('#!/bin/sh\nexit 1\n')
            checker.chmod(0o755)
            for name, body in {'mountpoint': 'exit 0', 'umount': 'echo "$*" >> "' + str(events) + '"; exit 1'}.items():
                path = fake / name
                path.write_text('#!/bin/sh\n' + body + '\n')
                path.chmod(0o755)
            variables = yaml.safe_load((REPO / 'roles/dingo_client/defaults/main.yml').read_text())
            variables.update(ansible_connection='local', ansible_become=False,
                             ansible_python_interpreter=sys.executable,
                             dingo_client_mount_check_script=str(checker),
                             dingo_client_base_dir=str(root), dingo_client_mount_point=str(root),
                             dingo_client_log_dir=str(root), dingo_client_pin_cpu=False)
            output = root / 'mount.sh'
            play = [{'hosts': 'localhost', 'gather_facts': False, 'vars': variables,
                     'tasks': [{'template': {'src': str(REPO / 'roles/dingo_client/templates/dingofs-mount.sh.j2'),
                                             'dest': str(output)}}]}]
            play_path = root / 'play.yml'
            play_path.write_text(yaml.safe_dump(play))
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(play_path)],
                                    cwd=REPO, capture_output=True, text=True,
                                    env=dict(os.environ, ANSIBLE_CONFIG=str(REPO / 'ansible.cfg')))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            subprocess.run(['/bin/bash', '-n', str(output)], check=True)
            env = dict(os.environ, PATH=str(fake) + ':' + os.environ['PATH'])
            result = subprocess.run(['/bin/bash', str(output)], env=env, capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(events.exists())
            checker.write_text('#!/bin/sh\nexit 0\n')
            result = subprocess.run(['/bin/bash', str(output)], env=env, capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(events.read_text().strip(), str(root))


if __name__ == '__main__':
    unittest.main()
