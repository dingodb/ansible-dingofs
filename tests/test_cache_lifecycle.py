"""Cache deploy and legacy binary updates converge after interrupted restarts."""
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
class CacheLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cache-lifecycle-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / 'cache'
        self.base.mkdir()
        self.installed = self.base / 'dingo-cache'
        self.installed.write_text('old binary')
        self.installed.chmod(0o755)
        self.candidate = self.root / 'candidate'
        self.candidate.write_text('new binary')
        self.config = self.base / 'dingo-cache.conf'
        self.config.write_text('--listen_ip=127.0.0.1\n--log_level=info\n')
        self.marker = self.base / '.restart-required'
        self.fail = self.root / 'fail-restart'
        self.events = self.root / 'events'
        self.running = self.root / 'running/dingo-cache'
        self.running.parent.mkdir()
        self.running.write_text('old binary')
        # A real systemd MainPID can be the shell running dingo-cache-start.sh.
        wrapper = self.root / 'proc/123'
        (wrapper / 'task/123').mkdir(parents=True)
        (wrapper / 'exe').symlink_to('/bin/bash')
        (wrapper / 'task/123/children').write_text('124 ')
        (self.root / 'proc/124').mkdir()
        (self.root / 'proc/124/exe').symlink_to(self.running)
        foreign = self.root / 'foreign/dingo-cache'
        foreign.parent.mkdir()
        foreign.write_text('unrelated older cache')
        (self.root / 'proc/999').mkdir()
        (self.root / 'proc/999/exe').symlink_to(foreign)

        self.library = self.root / 'library'
        self.library.mkdir()
        (self.library / 'systemd.py').write_text('''from ansible.module_utils.basic import AnsibleModule
from pathlib import Path
import os,shutil
m=AnsibleModule(argument_spec=dict(name=dict(),state=dict(),enabled=dict(type='bool'),
                                daemon_reload=dict(type='bool')),supports_check_mode=True)
if m.params['state']=='restarted' and not m.check_mode:
    with Path(os.environ['CACHE_EVENTS']).open('a') as stream:stream.write('restart\\n')
    if Path(os.environ['CACHE_FAIL']).exists():m.fail_json(msg='injected restart failure')
    shutil.copyfile(os.environ['CACHE_INSTALLED'],os.environ['CACHE_RUNNING'])
m.exit_json(changed=m.params['state']=='restarted')
''')
        (self.library / 'wait_for.py').write_text('''from ansible.module_utils.basic import AnsibleModule
m=AnsibleModule(argument_spec=dict(host=dict(),port=dict(),delay=dict(),timeout=dict()))
m.exit_json(changed=False)
''')
        self.fakebin = self.root / 'bin'
        self.fakebin.mkdir()
        systemctl = self.fakebin / 'systemctl'
        systemctl.write_text('#!/bin/sh\nif [ "$1" = show ]; then echo 123; else echo active; fi\n')
        systemctl.chmod(0o755)

        self.templates = self.root / 'templates'
        self.templates.mkdir()
        (self.templates / 'dingo-cache.conf.j2').write_text('--listen_ip=127.0.0.1\n--log_level={{ fixture_log_level }}\n')
        (self.templates / 'dingo-cache-start.sh.j2').write_text('#!/bin/sh\nexec {{ dingo_cache_base_dir }}/dingo-cache\n')
        (self.templates / 'dingo-cache.service.j2').write_text('[Service]\nExecStart={{ dingo_cache_base_dir }}/dingo-cache-start.sh\n')
        self.units = self.root / 'units'
        self.units.mkdir()
        startup = self.base / 'dingo-cache-start.sh'
        startup.write_text('#!/bin/sh\nexec ' + str(self.base) + '/dingo-cache\n')
        startup.chmod(0o755)
        unit = self.units / 'fixture-cache.service'
        unit.write_text('[Service]\nExecStart=' + str(self.base) + '/dingo-cache-start.sh\n')
        unit.chmod(0o644)
        self.variables = dict(ansible_connection='local', ansible_become=False,
                              ansible_python_interpreter=sys.executable,
                              dingo_cache_base_dir=str(self.base), dingo_cache_bin_name='dingo-cache',
                              dingo_cache_bin_local_src=str(self.candidate),
                              dingo_cache_service_name='fixture-cache', dingo_cache_listen_port=19080,
                              dingo_cache_listen_ip='127.0.0.1', dingo_cache_config_id='fixture-id',
                              fixture_log_level='info')
        inspector = (REPO / 'roles/dingo_cache/tasks/inspect_runtime.yml').read_text()
        self.inspector = self.root / 'inspect_runtime.yml'
        self.inspector.write_text(inspector.replace('/proc/', str(self.root / 'proc') + '/'))

    def adapt(self, value):
        if isinstance(value, list):
            return [self.adapt(item) for item in value]
        if not isinstance(value, dict):
            return value.replace('/etc/systemd/system', str(self.units)) if isinstance(value, str) else value
        result = {key: self.adapt(item) for key, item in value.items()}
        if 'import_tasks' in result:
            source = result['import_tasks']
            if source.endswith('inspect_runtime.yml'):
                result['import_tasks'] = str(self.inspector)
            elif source.endswith('preflight_binary.yml'):
                result['import_tasks'] = str(REPO / 'roles/dingo_cache/tasks/preflight_binary.yml')
        if 'template' in result:
            result['template']['src'] = str(self.templates / result['template']['src'])
        return result

    def run_play(self, entry='deploy', check=False):
        if entry == 'deploy':
            tasks = yaml.safe_load((REPO / 'roles/dingo_cache/tasks/install_service.yml').read_text())
            handlers = yaml.safe_load((REPO / 'roles/dingo_cache/handlers/main.yml').read_text())
        else:
            tasks = yaml.safe_load((REPO / 'playbooks/cache_06_upgrade.yml').read_text())[0]['tasks']
            handlers = []
        play = self.root / 'play.yml'
        play.write_text(yaml.safe_dump([dict(hosts='localhost', gather_facts=False,
            vars=self.variables, tasks=self.adapt(tasks), handlers=handlers,
            environment={'PATH': str(self.fakebin) + ':' + os.environ['PATH'],
                         'CACHE_EVENTS': str(self.events), 'CACHE_FAIL': str(self.fail),
                         'CACHE_INSTALLED': str(self.installed), 'CACHE_RUNNING': str(self.running)})]))
        return subprocess.run(['ansible-playbook', '-i', 'localhost,', str(play)] + (['--check'] if check else []),
            cwd=REPO, env=dict(os.environ, ANSIBLE_CONFIG=str(REPO / 'ansible.cfg'),
                              ANSIBLE_LIBRARY=str(self.library)), capture_output=True, text=True, timeout=60)

    def restarts(self):
        return self.events.read_text().splitlines() if self.events.exists() else []

    def assert_succeeded(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def interrupted_binary_update(self, entry):
        self.fail.touch()
        result = self.run_play(entry)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.installed.read_text(), 'new binary')
        self.assertTrue(self.marker.exists())
        self.fail.unlink()
        for _ in range(2):
            self.assert_succeeded(self.run_play(entry))
            self.assertFalse(self.marker.exists())
            self.assertEqual(self.restarts(), ['restart', 'restart'])
        # Recover drift from an update made before restart markers were added.
        self.running.write_text('old binary without a marker')
        self.assert_succeeded(self.run_play(entry))
        self.assertEqual(self.restarts(), ['restart'] * 3)

    def test_deploy_retries_interrupted_binary_restart_and_identical_retry_is_idle(self):
        self.interrupted_binary_update('deploy')

    def test_legacy_binary_only_entry_retains_restart_recovery(self):
        self.interrupted_binary_update('upgrade')

    def test_deploy_recovers_a_config_only_change_with_a_current_binary(self):
        self.candidate.write_text('old binary')
        self.variables['fixture_log_level'] = 'error'
        self.fail.touch()
        result = self.run_play()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('--log_level=error', self.config.read_text())
        self.assertEqual(self.running.read_text(), self.installed.read_text())
        self.assertTrue(self.marker.exists())
        self.fail.unlink()
        for _ in range(2):
            self.assert_succeeded(self.run_play())
            self.assertFalse(self.marker.exists())
            self.assertEqual(self.restarts(), ['restart', 'restart'])

    def test_deploy_check_mode_keeps_files_and_services_unchanged(self):
        self.assert_succeeded(self.run_play(check=True))
        self.assertEqual(self.installed.read_text(), 'old binary')
        self.assertEqual(self.running.read_text(), 'old binary')
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.restarts(), [])


if __name__ == '__main__':
    unittest.main()
