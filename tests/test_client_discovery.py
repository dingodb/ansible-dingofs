"""Discovery fixtures never mount filesystems or operate real services."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('client_discover', REPO / 'library/dingofs_client_discover.py')
discovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(discovery)
MOUNTINFO = ('31 1 8:1 / / rw - ext4 /dev/root rw\n'
             '42 31 0:12 / /mnt/work\\040space rw - fuse.dingofs DingoFS:work rw\n'
             '43 31 0:12 /subdir /mnt/alias ro - fuse.dingofs DingoFS:work rw\n'
             '44 31 0:13 / /mnt/other rw - fuse.sshfs server:/path rw\n')


def service(name='dingofs-mount-work-v5.2.service', **values):
    result = dict(service=name, fsname='work', mount_point='/mnt/work space', active_state='active')
    result.update(values)
    return result


def process(**values):
    result = dict(pid=123, service='dingofs-mount-work-v5.2.service', fsname='work',
                  mount_point='/mnt/work space', mount_namespace='current')
    result.update(values)
    return result


class DiscoveryTests(unittest.TestCase):
    def test_mountinfo_decodes_paths_and_preserves_bind_mounts(self):
        mounts = discovery.parse_mountinfo(MOUNTINFO)
        found, other = discovery.correlate(mounts, [service()], [process()])
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0]['root'], '/subdir')
        self.assertTrue(found[0]['read_only'])
        self.assertEqual(found[1]['mount_point'], '/mnt/work space')
        self.assertEqual(found[1]['services'], ['dingofs-mount-work-v5.2.service'])
        self.assertEqual(found[1]['pids'], [123])
        self.assertEqual(other[0]['fstype'], 'fuse.sshfs')

    def test_empty_or_malformed_mount_table_is_an_error(self):
        for data in ('', 'broken', '42 31 - fuse bad\n'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                discovery.parse_mountinfo(data)

    def test_manual_mount_does_not_invent_a_service(self):
        found, _ = discovery.correlate(discovery.parse_mountinfo(MOUNTINFO), [], [process(service='ssh.service')])
        self.assertEqual(found[1]['services'], [])

    def test_generic_fuse_needs_live_client_evidence(self):
        mounts = discovery.parse_mountinfo('42 31 0:12 / /mnt/work rw - fuse generic rw\n')
        found, other = discovery.correlate(mounts, [], [])
        self.assertEqual(found, [])
        self.assertEqual(len(other), 1)
        found, other = discovery.correlate(mounts, [], [process(mount_point='/mnt/work')])
        self.assertEqual(found[0]['fsname'], 'work')
        self.assertEqual(other, [])

    def test_process_in_another_namespace_cannot_identify_a_host_mount(self):
        mounts = discovery.parse_mountinfo('42 31 0:12 / /mnt/work rw - fuse generic rw\n')
        found, _ = discovery.correlate(mounts, [], [process(mount_point='/mnt/work', mount_namespace='other')])
        self.assertEqual(found, [])

    def test_wrong_filesystem_and_unmounted_services_are_distinct(self):
        mounts = discovery.parse_mountinfo(MOUNTINFO)
        services = [service(fsname='wrong'), service('stopped.service', mount_point='/mnt/absent', active_state='inactive'),
                    service('unknown.service', mount_point='')]
        discovery.correlate(mounts, services, [])
        self.assertEqual([s['mount_state'] for s in services], ['different_filesystem', 'not_mounted', 'unknown'])

    def test_starting_client_cannot_claim_a_different_mounted_filesystem(self):
        services = [service(fsname='wrong')]
        found, _ = discovery.correlate(discovery.parse_mountinfo(MOUNTINFO), services, [process(fsname='wrong')])
        self.assertEqual(found[1]['pids'], [])
        self.assertEqual(services[0]['mount_state'], 'different_filesystem')

    def test_same_fsname_at_other_path_does_not_match_service(self):
        services = [service(mount_point='/mnt/work space/child')]
        discovery.correlate(discovery.parse_mountinfo(MOUNTINFO), services, [])
        self.assertEqual(services[0]['mount_state'], 'not_mounted')

    def test_current_and_legacy_client_arguments(self):
        for argv in (['/bin/dingo-client', 'mds://host:6900/work', '/mnt/work', '--log_dir=/tmp'],
                     ['/bin/dingo-fuse', '--fsname=work', '--mountpoint', '/mnt/work'],
                     ['/bin/dingo-client', '-o', 'fsname=DingoFS:work', '/mnt/work']):
            with self.subTest(argv=argv):
                self.assertEqual(discovery.client_identity(argv), dict(fsname='work', mount_point='/mnt/work'))
        self.assertEqual(discovery.client_identity(['/bin/dingo-client', '--conf', '/etc/client.conf'])['mount_point'], '')

    def test_scripts_are_read_as_literals_and_never_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'start.sh'
            marker = Path(directory).resolve() / 'executed'
            path.write_text('g_fsname="work"\ng_mnt="/mnt/work space"\ntouch ' + str(marker))
            self.assertEqual(discovery.script_identity(str(path), []), dict(fsname='work', mount_point='/mnt/work space'))
            self.assertFalse(marker.exists())
            path.write_text('g_fsname="$(touch ' + str(marker) + ')"\ng_mnt="$HOME/mnt"')
            self.assertEqual(discovery.script_identity(str(path), []), {})
            self.assertFalse(marker.exists())
            link = Path(directory) / 'link.sh'; link.symlink_to(path)
            self.assertEqual(discovery.script_identity(str(link), []), {})

    def test_script_under_fuse_is_not_opened(self):
        with patch.object(Path, 'is_symlink', side_effect=AssertionError('must not traverse FUSE')):
            self.assertEqual(discovery.script_identity('/mnt/work space/start.sh', discovery.parse_mountinfo(MOUNTINFO)), {})

    def test_services_include_inactive_units_and_custom_direct_clients(self):
        def runner(args, timeout):
            if args[0] == 'list-unit-files':
                return 'dingofs-mount-old.service disabled\ncustom.service enabled\nssh.service enabled\ngetty@.service enabled\n'
            if args[0] == 'list-units':
                return 'custom.service loaded active running Custom\n'
            self.assertNotIn('getty@.service', args)
            return ('Id=dingofs-mount-old.service\nActiveState=failed\nSubState=failed\nMainPID=0\n'
                    'ExecStart={ path=/missing/old.sh ; argv[]=/missing/old.sh ; }\n\n'
                    'Id=custom.service\nActiveState=active\nMainPID=123\n'
                    'ExecStart={ path=/missing/dingo-client ; argv[]=/missing/dingo-client mds://host/work /mnt/custom ; }\n\n'
                    'Id=ssh.service\nMainPID=456\nExecStart={ path=/usr/sbin/sshd ; }\n')
        warnings = []
        services = discovery.collect_services([process(service='ssh.service')], [], 1, warnings, runner)
        self.assertEqual([s['service'] for s in services], ['custom.service', 'dingofs-mount-old.service'])
        self.assertEqual(services[0]['fsname'], 'work')
        self.assertEqual(services[0]['mount_point'], '/mnt/custom')
        self.assertEqual(services[1]['active_state'], 'failed')
        self.assertTrue(warnings)  # Missing scripts are not silently treated as inspected.

    def test_systemd_failure_keeps_mount_discovery_and_marks_it_partial(self):
        warnings = []
        def runner(args, timeout):
            raise subprocess.TimeoutExpired('systemctl', timeout)
        self.assertEqual(discovery.collect_services([], [], 1, warnings, runner), [])
        self.assertIn('incomplete', warnings[0])

    def test_proc_scan_omits_raw_command_line_and_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / 'self/ns').mkdir(parents=True)
            (proc / 'self/ns/mnt').symlink_to('mnt:[1]')
            for pid, namespace in ((123, 'mnt:[1]'), (124, 'mnt:[2]')):
                entry = proc / str(pid)
                (entry / 'ns').mkdir(parents=True)
                (entry / 'ns/mnt').symlink_to(namespace)
                (entry / 'cmdline').write_bytes(b'/bin/dingo-client\0mds://host/work\0/mnt/work\0--secret=PRIVATE\0')
                (entry / 'cgroup').write_text('0::/system.slice/custom.service\n')
            results = discovery.collect_processes(proc, [])
            self.assertEqual([p['mount_namespace'] for p in results], ['current', 'other'])
            self.assertEqual(results[0]['service'], 'custom.service')
            self.assertNotIn('PRIVATE', json.dumps(results))


@unittest.skipUnless(shutil.which('ansible-playbook'), 'requires Ansible')
class DiscoveryPlaybookTests(unittest.TestCase):
    def run_play(self, strict=False, check=False):
        with tempfile.TemporaryDirectory(prefix='client-discovery-') as directory:
            root = Path(directory)
            library = root / 'library'; library.mkdir()
            (library / 'dingofs_client_discover.py').write_text('''from ansible.module_utils.basic import AnsibleModule
m = AnsibleModule(argument_spec=dict(command_timeout=dict(type='int')), supports_check_mode=True)
if m.params['command_timeout'] == 2:
    m.fail_json(msg='fixture cannot read mountinfo')
if m.params['command_timeout'] == 3:
    m.exit_json(changed=False, summary={'complete': False, 'dingofs_mounts': 0}, mounts=[], services=[], processes=[], other_fuse_mounts=[], warnings=['fixture cannot inspect service'])
m.exit_json(changed=False, summary={'complete': True, 'dingofs_mounts': 0}, mounts=[], services=[], processes=[], other_fuse_mounts=[], warnings=[])
''')
            hosts = {name: dict(ansible_connection='local', ansible_python_interpreter=sys.executable,
                                dingo_client_discover_command_timeout=timeout,
                                dingo_clients='{{ deliberately_undefined }}')
                     for name, timeout in (('empty-node', 1), ('failed-node', 2), ('partial-node', 3))}
            # A loopback closed port exercises Ansible's unreachable handling.
            hosts['unreachable-node'] = dict(ansible_host='127.0.0.1', ansible_port=1,
                                              ansible_connection='ssh', ansible_connect_timeout=1)
            inventory = root / 'inventory.yml'
            inventory.write_text(yaml.safe_dump({'client_servers': {'hosts': hosts}}))
            report_root = root / 'reports'
            report_root.mkdir(mode=0o755)
            report_root.chmod(0o755)
            args = ['ansible-playbook', '-i', str(inventory), str(REPO / 'playbooks/client_98_discover.yml'),
                    '-e', json.dumps(dict(dingo_client_discover_report_dir=str(report_root),
                                         dingo_client_discover_strict=strict))]
            if check:
                args.append('--check')
            result = subprocess.run(args, cwd=REPO, env=dict(os.environ, ANSIBLE_LIBRARY=str(library) + os.pathsep + str(REPO / 'library'),
                                    DINGO_CLIENT_REPORT_DIR=str(root / 'restart-reports'),
                                    ANSIBLE_STDOUT_CALLBACK='dingofs',
                                    ANSIBLE_NOCOLOR='1'), capture_output=True, text=True, timeout=60)
            reports = list(report_root.glob('run-*/report.json'))
            self.assertEqual(len(reports), 1, result.stdout + result.stderr)
            report = json.loads(reports[0].read_text())
            self.assertEqual(report['check_mode'], check)
            rows = {row['host']: row for row in report['hosts']}
            self.assertEqual(rows['empty-node']['scan_status'], 'ok')
            self.assertEqual(rows['failed-node']['scan_status'], 'failed')
            self.assertEqual(rows['partial-node']['scan_status'], 'partial')
            self.assertTrue(rows['partial-node']['warnings'])
            self.assertEqual(rows['unreachable-node']['scan_status'], 'unreachable')
            self.assertEqual(reports[0].stat().st_mode & 0o777, 0o600)
            self.assertEqual(report_root.stat().st_mode & 0o777, 0o755)
            self.assertFalse((root / 'restart-reports').exists())
            for name in ('summary.txt', 'hosts.tsv', 'mounts.tsv', 'layouts.tsv', 'issues.tsv'):
                self.assertTrue((reports[0].parent / name).is_file())
            self.assertIn('Nodes: 4 | OK: 1 | PARTIAL: 1 | FAILED: 1 | UNREACHABLE: 1', result.stdout)
            self.assertNotIn('TASK [', result.stdout)
            self.assertNotIn('PLAY RECAP', result.stdout)
            self.assertNotIn('dingo_client_discovery:', result.stdout)
            return result

    def test_partial_and_unreachable_hosts_are_saved(self):
        result = self.run_play()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_strict_check_mode_reports_before_failing(self):
        result = self.run_play(strict=True, check=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('inspect the saved report', result.stdout)


if __name__ == '__main__':
    unittest.main()
