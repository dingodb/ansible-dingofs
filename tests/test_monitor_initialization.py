"""Recover the CLI's short Grafana wait without concealing other failures."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

import yaml

REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('ansible-playbook'), 'requires Ansible')
class MonitorInitializationTests(unittest.TestCase):
    def run_play(self, root, variables, tasks):
        variables.update({'ansible_connection': 'local', 'ansible_become': False,
                          'ansible_python_interpreter': sys.executable, 'dingofs_user': 'root'})
        play = root / 'play.yml'
        play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False,
                                        'vars': variables, 'tasks': tasks}]))
        return subprocess.run(['ansible-playbook', '-i', 'localhost,', str(play)],
                              cwd=REPO, capture_output=True, text=True, timeout=45,
                              env=dict(os.environ, ANSIBLE_STDOUT_CALLBACK='default'))

    def deploy_task(self, root, error):
        cli = root / 'dingo'
        cli.write_text('#!/bin/sh\nprintf "%s\\n" ' + repr(error) + '\nexit 1\n')
        cli.chmod(0o755)
        return next(t for t in yaml.safe_load((REPO / 'roles/dingo_monitor/tasks/main.yml').read_text())
                    if t.get('name') == 'Deploy the monitor stack')

    def test_cold_start_waits_for_datasource_and_repairs_the_dashboard_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dashboard = root / 'grafana/provisioning/dashboards/server_metric_zh.json'
            dashboard.parent.mkdir(parents=True)
            dashboard.write_text(json.dumps({'uid': 'server-metric', 'panels': [
                {'datasource': {'type': 'prometheus', 'uid': '${PROMETHEUS_UID}'}}]}))
            calls = {}
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass
                def do_GET(self):
                    calls[self.path] = calls.get(self.path, 0) + 1
                    status, body = 200, {}
                    if self.path.startswith('/api/') and self.path != '/api/health' and self.headers.get('Authorization') != 'Basic ' + base64.b64encode(b'admin:admin').decode():
                        status, body = 401, {'message': 'unauthorized'}
                    elif self.path == '/-/ready':
                        body = {'ready': True}
                    elif self.path == '/api/health':
                        status = 503 if calls[self.path] < 3 else 200
                        body = {'database': 'ok' if status == 200 else 'starting'}
                    elif self.path == '/api/datasources':
                        body = [] if calls[self.path] == 1 else [
                            {'uid': 'verified_uid', 'type': 'prometheus', 'isDefault': True}]
                    elif self.path == '/api/datasources/uid/verified_uid/health':
                        body = {'status': 'OK'}
                    elif self.path == '/api/dashboards/uid/server-metric':
                        body = {'dashboard': json.loads(dashboard.read_text())}
                    else:
                        status = 404
                    payload = json.dumps(body).encode()
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                variables = {'dingo_cli_bin_dir': str(root), 'dingo_monitor_config_file': str(root / 'monitor.yml'),
                             '_monitor_existing_count': 0, 'dingo_monitor_initialize_grafana': True,
                             'dingo_monitor_hosts': ['localhost'], 'dingo_monitor_central_host': 'localhost',
                             'dingo_monitor_service_address': '127.0.0.1',
                             'dingo_monitor_prometheus_port': port, 'dingo_monitor_grafana_port': port,
                             'dingo_monitor_ready_retries': 4, 'dingo_monitor_ready_delay': 0,
                             'dingo_monitor_data_dir': str(root),
                             'monitor_status': {'stdout': 'Id Role Host Container Id Status\n' + '\n'.join(
                                 f'{i:06x} {role} localhost {i:012x} Up 1 minute'
                                 for i, role in enumerate(['prometheus', 'grafana', 'node_exporter', 'monitor_sync'], 1))}}
                tasks = [self.deploy_task(root, 'Error: failed to fetch prometheus info')]
                tasks += [{'include_role': {'name': 'dingo_monitor', 'tasks_from': 'initialize'}}] * 2
                result = self.run_play(root, variables, tasks)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('verified_uid', dashboard.read_text())
                self.assertNotIn('${PROMETHEUS_UID}', dashboard.read_text())
                self.assertGreaterEqual(calls['/api/health'], 3)
                self.assertGreaterEqual(calls['/api/datasources'], 2)
                self.assertIn('changed=2', result.stdout)  # CLI attempt + one repair
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_unrelated_cli_errors_and_non_opted_in_sites_still_fail(self):
        for enabled, message in [(True, 'Error: image not found'),
                                 (False, 'Error: failed to fetch prometheus info')]:
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                task = self.deploy_task(root, message)
                result = self.run_play(root, {'dingo_cli_bin_dir': str(root),
                                             'dingo_monitor_config_file': str(root / 'monitor.yml'),
                                             '_monitor_existing_count': 0,
                                             'dingo_monitor_initialize_grafana': enabled}, [task])
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
