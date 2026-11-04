"""Failure injection for the remap controller using temporary paths only."""
import copy
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
class RemapTests(unittest.TestCase):
    def test_services_restart_only_after_original_mounts_are_verified(self):
        for restored in (True, False):
            with self.subTest(restored=restored), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fakebin = root / 'fakebin'
                fakebin.mkdir()
                events = root / 'events'
                events.write_text('')
                scripts = {
                    'systemctl': 'if [ "$1" = list-units ]; then echo "dingofs-mount-a.service loaded active running"; else echo "$*" >> "' + str(events) + '"; fi',
                    'findmnt': 'if [ "$4" != --mountpoint ]; then echo "/dev/original /mnt/disk1"; else echo "' + ('/dev/original' if restored else '/dev/wrong') + '"; fi',
                    'readlink': 'echo "$2"',
                }
                for name, content in scripts.items():
                    p = fakebin / name
                    p.write_text('#!/bin/sh\n' + content + '\n')
                    p.chmod(0o755)
                stub = root / 'source.sh'
                stub.write_text('#!/bin/sh\nif [ "$1" = --apply ]; then exit 1; else exit 2; fi\n')
                tasks = yaml.safe_load((REPO / 'playbooks/cache_02b_remap.yml').read_text())[0]['tasks']
                def adapt(value):
                    if isinstance(value, str):
                        return value.replace('/tmp/remap-', str(root / 'remap-')).replace('/run/lock/ansible-dingo-remap.lock', str(root / 'lock'))
                    if isinstance(value, list):
                        return [adapt(item) for item in value]
                    if isinstance(value, dict):
                        result = {key: adapt(item) for key, item in value.items()}
                        if result.get('name') == 'Record original mount sources before remapping':
                            result.pop('shell')
                            result['command'] = 'findmnt -rn -o SOURCE,TARGET'
                        if 'copy' in result:
                            result['copy']['src'] = str(stub)
                        if result.get('name', '').startswith('Stop dingo services'):
                            # macOS has Bash 3; provide the Linux mapfile operation.
                            result['shell'] = 'mapfile() { svcs=(); while IFS= read -r line; do svcs+=("$line"); done; }\n' + result['shell']
                        return result
                    return value
                play = [{'hosts': 'localhost', 'gather_facts': False,
                         'vars': {'ansible_connection': 'local', 'ansible_become': False,
                                  'ansible_python_interpreter': sys.executable, 'remap_apply': True},
                         'environment': {'PATH': str(fakebin) + ':' + os.environ['PATH']},
                         'tasks': adapt(tasks)}]
                path = root / 'play.yml'
                path.write_text(yaml.safe_dump(play))
                result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(path)], cwd=REPO,
                                        env=dict(os.environ, ANSIBLE_CONFIG=str(REPO / 'ansible.cfg')),
                                        capture_output=True, text=True, timeout=45)
                self.assertNotEqual(result.returncode, 0)
                expected = ['stop dingofs-mount-a.service']
                if restored:
                    expected.append('start dingofs-mount-a.service')
                self.assertEqual(events.read_text().splitlines(), expected, result.stdout + result.stderr)
                self.assertFalse((root / 'lock').exists())

    def test_partial_unmount_failure_calls_restore(self):
        version = subprocess.check_output(['/bin/bash', '-c', 'echo ${BASH_VERSINFO[0]}'], text=True)
        if int(version) < 4:
            self.skipTest('requires Bash 4 associative arrays; exercised by Linux CI')
        source = (REPO / 'roles/lvm_cache/files/remap-dingo-mounts-by-size.sh').read_text()
        segment = source[source.index('declare -A UNMOUNTED_MOUNTS'):source.index('echo "Installing updated /etc/fstab')]
        script = '''OLD_MOUNTS=(/fixture/one /fixture/two)
calls=0
umount() { calls=$((calls+1)); [ "$calls" -eq 1 ]; }
restore_original_layout() { echo RESTORED; }
''' + segment
        result = subprocess.run(['/bin/bash'], input=script, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('RESTORED', result.stdout)


if __name__ == '__main__':
    unittest.main()
