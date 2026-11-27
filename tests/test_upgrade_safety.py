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
            findmnt = fakebin / 'findmnt'
            findmnt.write_text('#!' + sys.executable + '\n' + '''import json, sys
from pathlib import Path
name = Path(sys.argv[-1]).parent.name
print(json.dumps({'filesystems': [{'fstype': 'fuse.dingofs', 'source': 'DingoFS:' + name}]}))
''')
            findmnt.chmod(0o755)
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
            env = dict(os.environ, ANSIBLE_CONFIG=str(REPO / 'ansible.cfg'),
                       ANSIBLE_LIBRARY=os.pathsep.join((str(library), str(REPO / 'library'))))
            for attempt in range(2):
                result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(play_path)],
                                        cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(events.read_text().splitlines(), ['dingofs-mount-a', 'dingofs-mount-b'])
            # With dingo_client_stage_when_busy=false, a busy instance must retain its current binary.
            checker.write_text('#!/bin/sh\nexit 1\n')
            candidate.write_text('next')
            result = subprocess.run(
                ['ansible-playbook', '-i', 'localhost,', str(play_path), '-e', 'dingo_client_stage_when_busy=false'],
                cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((root / 'a/bin/dingo-client').read_text(), 'new')
            self.assertFalse((root / 'a/.restart-required').exists())
            self.assertEqual(events.read_text().splitlines(), ['dingofs-mount-a', 'dingofs-mount-b'])
            # By default (dingo_client_stage_when_busy=true), a busy instance stages the new
            # binary on disk, preserves the original .bak, marks .restart-required, and defers restart.
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(play_path)],
                                    cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((root / 'a/bin/dingo-client').read_text(), 'next')
            self.assertEqual((root / 'a/bin/dingo-client.bak').read_text(), 'old')
            self.assertTrue((root / 'a/.restart-required').exists())
            self.assertEqual(events.read_text().splitlines(), ['dingofs-mount-a', 'dingofs-mount-b'])
            # Once idle, a retry restarts the staged binary and clears .restart-required.
            checker.write_text('#!/bin/sh\nexit 0\n')
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(play_path)],
                                    cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(events.read_text().splitlines(),
                             ['dingofs-mount-a', 'dingofs-mount-b', 'dingofs-mount-a', 'dingofs-mount-b'])
            self.assertFalse((root / 'a/.restart-required').exists())
            self.assertFalse((root / 'b/.restart-required').exists())
            # If usage becomes busy after the copy, a retry must still restart
            # the already-copied binary even though copy will report unchanged.
            candidate.write_text('final')
            counter = root / 'checks'
            checker.write_text('#!/bin/sh\nn=$(cat "' + str(counter) + '" 2>/dev/null || echo 0)\n'
                               + 'n=$((n+1)); echo "$n" > "' + str(counter) + '"\n'
                               + 'test "$n" -ne 2\n')
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(play_path)],
                                    cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            # The busy recheck defers a; b still upgrades in the same run.
            self.assertEqual(events.read_text().splitlines(),
                             ['dingofs-mount-a', 'dingofs-mount-b', 'dingofs-mount-a', 'dingofs-mount-b', 'dingofs-mount-b'])
            self.assertEqual((root / 'a/bin/dingo-client').read_text(), 'final')
            self.assertEqual((root / 'a/bin/dingo-client.bak').read_text(), 'old')
            self.assertTrue((root / 'a/.restart-required').exists())
            checker.write_text('#!/bin/sh\nexit 0\n')
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(play_path)],
                                    cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(events.read_text().splitlines(),
                             ['dingofs-mount-a', 'dingofs-mount-b', 'dingofs-mount-a', 'dingofs-mount-b',
                              'dingofs-mount-b', 'dingofs-mount-a'])
            self.assertFalse((root / 'a/.restart-required').exists())
    def test_upgrade_forwards_ignore_stale_mounts_flag_only_when_enabled(self):
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
            calls = root / 'checker_calls'
            # Simulate a mount with only STALE container refs: BUSY unless --ignore-stale-mounts is passed.
            checker.write_text(
                '#!/bin/sh\n'
                f'printf "%s\\n" "$*" >> "{calls}"\n'
                'for arg in "$@"; do\n'
                '  [ "$arg" = "--ignore-stale-mounts" ] && exit 0\n'
                'done\n'
                'exit 1\n'
            )
            checker.chmod(0o755)
            findmnt = fakebin / 'findmnt'
            findmnt.write_text('#!' + sys.executable + '\n' + '''import json
print(json.dumps({'filesystems': [{'fstype': 'fuse.dingofs', 'source': 'DingoFS:pub'}]}))
''')
            findmnt.chmod(0o755)
            candidate = root / 'candidate'
            candidate.write_text('new')
            base = root / 'pub'
            (base / 'bin').mkdir(parents=True)
            (base / 'bin/dingo-client').write_text('old')
            client = dict(fsname='pub', base_dir=str(base), mount_point=str(base / 'mount'))
            variables = dict(ansible_connection='local', ansible_become=False,
                             ansible_python_interpreter=sys.executable,
                             dingo_client_upgrade_bin_src=str(candidate),
                             dingo_client_mount_check_script=str(checker),
                             dingo_client_upgrade_dry_run=False,
                             dingo_client_stage_when_busy=False)
            play = [{'hosts': 'localhost', 'gather_facts': False, 'vars': variables,
                     'environment': {'PATH': str(fakebin) + ':' + os.environ['PATH'], 'UPGRADE_EVENTS': str(events)},
                     'tasks': [{'include_tasks': str(REPO / 'roles/dingo_client_upgrade/tasks/upgrade_instance.yml'),
                                'vars': {'client_instance': client}}]}]
            play_path = root / 'play.yml'
            play_path.write_text(yaml.safe_dump(play))
            env = dict(os.environ, ANSIBLE_CONFIG=str(REPO / 'ansible.cfg'),
                       ANSIBLE_LIBRARY=os.pathsep.join((str(library), str(REPO / 'library'))))
            # Default (dingo_client_ignore_stale_mounts=false): checker gets no flag and skips.
            res_default = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(play_path)],
                                         cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(res_default.returncode, 0, res_default.stdout + res_default.stderr)
            self.assertFalse(events.exists())
            self.assertEqual((base / 'bin/dingo-client').read_text(), 'old')
            self.assertNotIn('--ignore-stale-mounts', calls.read_text())
            # Opt-in (dingo_client_ignore_stale_mounts=true): both precheck and recheck pass the flag.
            calls.unlink()
            res_optin = subprocess.run(
                ['ansible-playbook', '-i', 'localhost,', str(play_path), '-e', 'dingo_client_ignore_stale_mounts=true'],
                cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(res_optin.returncode, 0, res_optin.stdout + res_optin.stderr)
            self.assertEqual(events.read_text().splitlines(), ['dingofs-mount-pub'])
            self.assertEqual((base / 'bin/dingo-client').read_text(), 'new')
            recorded = calls.read_text().splitlines()
            self.assertEqual(len(recorded), 2)
            self.assertTrue(all('--ignore-stale-mounts' in line for line in recorded))

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
            # Unattended mount script stays conservative even if dingo_client_ignore_stale_mounts is set.
            res_mount_optin = subprocess.run(
                ['ansible-playbook', '-i', 'localhost,', str(play_path), '-e', 'dingo_client_ignore_stale_mounts=true'],
                cwd=REPO, capture_output=True, text=True,
                env=dict(os.environ, ANSIBLE_CONFIG=str(REPO / 'ansible.cfg')))
            self.assertEqual(res_mount_optin.returncode, 0, res_mount_optin.stdout + res_mount_optin.stderr)
            self.assertNotIn('--ignore-stale-mounts', output.read_text())


if __name__ == '__main__':
    unittest.main()
