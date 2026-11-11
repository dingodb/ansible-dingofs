"""Exercise optional service budgets with real Ansible and shell parsing."""
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
class ServiceResourceTests(unittest.TestCase):
    def render(self, settings):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        variables = {'ansible_connection': 'local', 'ansible_become': False,
                     'ansible_python_interpreter': sys.executable}
        for role in ['dingo_client', 'dingo_cache']:
            variables.update(yaml.safe_load((REPO / f'roles/{role}/defaults/main.yml').read_text()))
        variables.update(settings)
        outputs = {
            'mount.sh': 'dingo_client/templates/dingofs-mount.sh.j2',
            'client.service': 'dingo_client/templates/dingofs-mount.service.j2',
            'cache.service': 'dingo_cache/templates/dingo-cache.service.j2',
        }
        play = root / 'render.yml'
        play.write_text(yaml.safe_dump([{
            'hosts': 'localhost', 'gather_facts': False, 'vars': variables,
            'tasks': [{'template': {'src': str(REPO / 'roles' / src), 'dest': str(root / dest)}}
                      for dest, src in outputs.items()],
        }]))
        result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(play)],
                                cwd=REPO, capture_output=True, text=True, timeout=45,
                                env=dict(os.environ, ANSIBLE_STDOUT_CALLBACK='default'))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        script = root / 'mount.sh'
        syntax = subprocess.run(['bash', '-n', str(script)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        # Run the generated launcher with fake commands: capture the argv that
        # actually reaches the client, detecting broken line continuations.
        base = root / 'client'
        (base / 'bin').mkdir(parents=True)
        (base / 'bin/dingo').write_text('#!/bin/sh\nexit 0\n')
        capture = root / 'argv.json'
        (base / 'bin/dingo-client').write_text(
            '#!' + sys.executable + '\nimport json,sys\n'
            + f'open({str(capture)!r}, "w").write(json.dumps(sys.argv[1:]))\n')
        for binary in (base / 'bin').iterdir():
            binary.chmod(0o755)
        content = script.read_text().replace('/root/dingofs/dingo-client/alayanew', str(base))
        content = content.replace('g_mnt="/dingofs/data"', f'g_mnt="{root / "mnt"}"')
        script.write_text(content)
        run = subprocess.run(['bash', str(script)], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return {name: (root / name).read_text() for name in outputs}, json.loads(capture.read_text())

    def test_defaults_keep_existing_budgets_and_flag_selection(self):
        outputs, argv = self.render({'dingo_client_numactl_enabled': False})
        for unit in ['client.service', 'cache.service']:
            for directive in ['CPUQuota=', 'MemoryHigh=', 'MemoryMax=']:
                self.assertNotIn(directive, outputs[unit])
        self.assertFalse(any(a.startswith('--poll_mode=') or a.startswith('--vfs_bthread_worker_num=') for a in argv))

    def test_small_tcp_profile_reaches_the_client_process_and_both_units(self):
        settings = {'dingo_client_numactl_enabled': False,
                    'dingo_client_use_rdma': False, 'dingo_client_poll_mode': False,
                    'dingo_client_shards': 2, 'dingo_client_vfs_bthread_worker_num': 4}
        for role in ['client', 'cache']:
            settings.update({f'dingo_{role}_cpu_quota': '100%',
                             f'dingo_{role}_memory_high': '1G',
                             f'dingo_{role}_memory_max': '1536M'})
        outputs, argv = self.render(settings)
        for unit in ['client.service', 'cache.service']:
            for directive in ['CPUQuota=100%', 'MemoryHigh=1G', 'MemoryMax=1536M']:
                self.assertIn(directive + '\n', outputs[unit])
        for flag in ['--poll_mode=false', '--vfs_bthread_worker_num=4', '--shards=2', '--use_rdma=false']:
            self.assertEqual(argv.count(flag), 1, argv)
        self.assertFalse(any(a.startswith('--rdma_') for a in argv))


if __name__ == '__main__':
    unittest.main()
