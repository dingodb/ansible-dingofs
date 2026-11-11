"""Apply budgets only to registered monitor containers; never select by name."""
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
class MonitorResourceTests(unittest.TestCase):
    def test_only_budgeted_registered_container_is_updated_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / 'podman'
            binary.write_text('#!' + sys.executable + '\n' + '''
import json, os, sys
from pathlib import Path
root = Path(os.environ['MONITOR_FIXTURE'])
assert sys.argv[-1] == 'bbbbbb222222', sys.argv
state = root / 'limits.json'
if sys.argv[1] == 'inspect':
    limits = json.loads(state.read_text()) if state.exists() else {
        'Memory': 0, 'MemorySwap': 0, 'CpuQuota': 0, 'CpuPeriod': 100000}
    print(json.dumps(limits))
elif sys.argv[1:] == ['update', '--memory=512m', '--memory-swap=512m', '--cpus=0.5', 'bbbbbb222222']:
    assert not state.exists(), 'already correct budgets were updated again'
    state.write_text(json.dumps({'Memory': 512*1048576, 'MemorySwap': 512*1048576,
                                 'CpuQuota': 50000, 'CpuPeriod': 100000}))
else:
    raise SystemExit(75)
''')
            binary.chmod(0o755)
            play = root / 'play.yml'
            play.write_text(yaml.safe_dump([{
                'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                'vars': {'ansible_become': False, 'ansible_python_interpreter': sys.executable,
                         'container_engine': 'podman', 'dingofs_user': 'root',
                         'dingo_monitor_hosts': ['localhost'],
                         'dingo_monitor_resource_limits': {'grafana': {'cpus': 0.5, 'memory_mb': 512}},
                         'monitor_status': {'stdout': 'Id Role Host Container Id Status\n'
                                            'aaaaaa prometheus localhost aaaaaa111111 Up 1 minute\n'
                                            'bbbbbb grafana localhost bbbbbb222222 Up 1 minute\n'}},
                'environment': {'PATH': str(root) + os.pathsep + os.environ['PATH'], 'MONITOR_FIXTURE': str(root)},
                'tasks': [{'include_role': {'name': 'dingo_monitor', 'tasks_from': 'resources'}}] * 2,
            }]))
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(play)],
                                    cwd=REPO, capture_output=True, text=True, timeout=40,
                                    env=dict(os.environ, ANSIBLE_STDOUT_CALLBACK='default'))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads((root / 'limits.json').read_text())['CpuQuota'], 50000)


if __name__ == '__main__':
    unittest.main()
