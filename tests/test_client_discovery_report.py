"""Bounded terminal summaries, complete exports, and ordinary Ansible output."""
import csv
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('discovery_report', REPO / 'library/dingofs_client_discovery_report.py')
reporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reporter)


def host(name, status='ok', mounted=True, **values):
    result = dict(host=name, address='192.0.2.1', scan_status=status, warnings=[], services=[], processes=[],
                  other_fuse_mounts=[], error='', mounts=[dict(fsname='work', mount_point='/mnt/work', root='/',
                  fstype='fuse.dingofs', source='DingoFS:work', read_only=False, services=[], pids=[])] if mounted else [])
    result.update(values)
    return result


class ReportTests(unittest.TestCase):
    def test_thousands_of_hosts_share_one_layout_and_bounded_output(self):
        report = reporter.build_report([host('node-{:04d}'.format(i)) for i in range(2000)])
        self.assertEqual(report['totals']['mounts'], 2000)
        self.assertEqual(len(report['layouts']), 1)
        self.assertEqual(report['layouts'][0]['nodes'], 2000)
        self.assertEqual(report['membership']['node-1999'], 'G01')
        self.assertNotIn('node-1999', report['summary'])
        self.assertLess(len(report['summary'].splitlines()), 15)

    def test_failed_and_unreachable_scans_cannot_be_no_mounts(self):
        report = reporter.build_report([host('failed', 'failed', False), host('offline', 'unreachable', False),
                                        host('empty', mounted=False), host('partial', 'partial', False)])
        self.assertEqual(report['layouts'][0]['hosts'], ['empty', 'partial'])
        self.assertNotIn('failed', report['membership'])
        self.assertEqual([issue['category'] for issue in report['issues']],
                         ['SCAN_UNREACHABLE', 'SCAN_FAILED', 'SCAN_PARTIAL'])

    def test_findings_and_layouts_are_bounded_independently(self):
        hosts = [host('node-{:02d}'.format(i), 'partial', warnings=['cannot inspect service']) for i in range(30)]
        for i, entry in enumerate(hosts):
            entry['mounts'][0]['mount_point'] += str(i)
        report = reporter.build_report(hosts, limit=3)
        self.assertEqual(len(report['layouts']), 30)
        self.assertEqual(len(report['issues']), 30)
        self.assertIn('27 more finding(s)', report['summary'])
        self.assertIn('27 more group(s)', report['summary'])
        self.assertNotIn('node-29', report['summary'])

    def test_inactive_wrong_and_unknown_services_need_attention(self):
        services = [dict(service='old.service', active_state='inactive', mount_state='not_mounted'),
                    dict(service='wrong.service', active_state='active', mount_state='different_filesystem'),
                    dict(service='unknown.service', active_state='active', mount_state='unknown')]
        report = reporter.build_report([host('node', services=services)])
        self.assertEqual({issue['category'] for issue in report['issues']},
                         {'NOT_MOUNTED', 'WRONG_FILESYSTEM', 'MOUNT_UNKNOWN'})
        self.assertEqual(report['totals']['attention_hosts'], 1)

    def test_complete_exports_and_permissions_even_in_check_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            hosts = [host('node-{:02d}'.format(i), 'partial', warnings=['warning']) for i in range(30)]
            result = reporter.write_report(root, hosts, limit=2, check_mode=True)
            report = json.loads(Path(result['reports']['report.json']).read_text())
            self.assertTrue(report['check_mode'])
            self.assertEqual(len(report['hosts']), 30)
            for name in ('hosts.tsv', 'mounts.tsv', 'issues.tsv'):
                with Path(result['reports'][name]).open() as stream:
                    self.assertEqual(len(list(csv.DictReader(stream, delimiter='\t'))), 30)
            self.assertEqual(root.stat().st_mode & 0o777, 0o755)
            self.assertEqual(Path(result['report_directory']).stat().st_mode & 0o777, 0o700)
            self.assertTrue(all(Path(path).stat().st_mode & 0o777 == 0o600 for path in result['reports'].values()))
            second = reporter.write_report(root, [host('fresh')])
            self.assertNotEqual(result['report_directory'], second['report_directory'])
            self.assertEqual(second['totals']['findings'], 0)

    def test_terminal_control_sequences_cannot_create_extra_rows(self):
        report = reporter.build_report([host('node\nFAKE\x1b[31m', 'failed', False, error='bad\nstate\t\x00')])
        self.assertNotIn('\x1b', report['summary'])
        self.assertNotIn('\x00', report['summary'])
        self.assertIn('bad state', report['summary'])

    def test_read_only_and_service_versions_have_distinct_layouts(self):
        first, second = host('first'), host('second')
        second['mounts'][0]['read_only'] = True
        report = reporter.build_report([first, second])
        self.assertEqual(len(report['layouts']), 2)
        self.assertIn('(ro)', report['summary'])
        for entry, version in ((first, 'v5.1'), (second, 'v5.2')):
            name = 'dingofs-mount-work-' + version + '.service'
            entry['mounts'][0]['read_only'] = False
            entry['mounts'][0]['services'] = [name]
            entry['services'] = [dict(service=name, active_state='active', mount_state='mounted')]
        report = reporter.build_report([first, second])
        self.assertEqual(len(report['layouts']), 2)
        self.assertIn('v5.1:active', report['summary'])
        self.assertIn('v5.2:active', report['summary'])


@unittest.skipUnless(shutil.which('ansible-playbook'), 'requires Ansible')
class DefaultOutputTests(unittest.TestCase):
    def test_ordinary_play_uses_normal_task_output_and_recap(self):
        with tempfile.TemporaryDirectory() as directory:
            play = Path(directory) / 'ordinary.yml'
            # JSON is accepted as YAML and avoids another test dependency.
            play.write_text(json.dumps([dict(hosts='all', gather_facts=False, become=False,
                tasks=[dict(name='Ordinary task', debug=dict(msg='ordinary-output-visible'))])]))
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(play)],
                                    cwd=REPO, capture_output=True, text=True, timeout=30,
                                    env=dict(os.environ, ANSIBLE_NOCOLOR='1', ANSIBLE_STDOUT_CALLBACK='dingofs'))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('TASK [Ordinary task]', result.stdout)
            self.assertIn('ordinary-output-visible', result.stdout)
            self.assertIn('PLAY RECAP', result.stdout)
            self.assertNotIn('No discovery report was produced', result.stdout)


if __name__ == '__main__':
    unittest.main()
