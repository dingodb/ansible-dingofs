"""Exercise standalone integration with existing topology/CLI and TiUP inputs."""
import copy
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
MDS = REPO / 'roles/dingofs_standalone'
TIKV = REPO / 'roles/tikv_standalone'


@unittest.skipUnless(shutil.which('ansible-playbook'), 'requires Ansible')
class StandaloneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.variables = {
            'ansible_connection': 'local', 'ansible_become': False,
            'ansible_python_interpreter': sys.executable,
            'ansible_facts': {'all_ipv4_addresses': ['192.0.2.10'],
                              'architecture': 'x86_64', 'service_mgr': 'systemd'},
            'dingofs_skip_version_check': True, 'dingofs_site_version': 'v5.2',
            'dingofs_container_image': 'example.com/fs:v5.2-fixture',
            'dingofs_cluster_name': 'demo', 'dingofs_meta_existing_hosts': False,
            'dingofs_user': 'root', 'dingofs_home': '/root', 'dingofs_ssh_key_type': 'ed25519',
            'dingo_cli_ssh_private_key_file': '/root/.ssh/id_ed25519_demo',
            'dingo_cli_db_url': 'sqlite:///root/.dingo/data/dingocli.db',
            'container_engine': 'podman', 'mds_replicas': 1,
            'dingofs_mds_storage_engine': 'tikv',
            'dingofs_mds_storage_url': 'list://192.0.2.11:2379',
            'dingofs_mds_extra_config': {'bthread_concurrency': 4, 'mds_quota_worker_num': 1},
            'dingofs_standalone_mds_memory_mb': 1024, 'dingofs_standalone_mds_cpus': 1,
            'tikv_cluster_name': 'demo-tikv', 'tikv_version': 'v8.5.8',
            'tikv_config_dir': str(self.root / 'tikv-config'),
            'tikv_tiup_home': str(self.root / 'tiup'),
        }

    def run_tasks(self, tasks, overrides=None, hosts=1, defaults=None):
        variables = copy.deepcopy(self.variables)
        variables.update(overrides or {})
        inventory = {'all': {'children': {
            'meta_servers': {'hosts': {
                'mds%d' % i: {'ansible_host': '198.51.100.10', 'dingofs_service_address': '192.0.2.10'}
                for i in range(hosts)}},
            'admin': {'hosts': {'mds0': {}}},
            'tikv_servers': {'hosts': {'kv0': {'ansible_host': '192.0.2.11'}}},
        }}}
        inv = self.root / 'hosts.yml'
        inv.write_text(yaml.safe_dump(inventory))
        play = self.root / 'play.yml'
        body = {'hosts': 'mds0', 'gather_facts': False, 'tasks': tasks}
        if defaults:
            body['vars_files'] = [str(defaults)]
        play.write_text(yaml.safe_dump([body]))
        varfile = self.root / 'vars.json'
        varfile.write_text(json.dumps(variables))
        return subprocess.run(['ansible-playbook', '-i', str(inv), str(play), '-e', '@' + str(varfile)],
                              cwd=REPO, capture_output=True, text=True, timeout=40,
                              env=dict(os.environ, ANSIBLE_ROLES_PATH=str(REPO / 'roles'),
                                       ANSIBLE_STDOUT_CALLBACK='default', ANSIBLE_NOCOLOR='1'))

    def assert_run(self, tasks, success=True, **kwargs):
        result = self.run_tasks(tasks, **kwargs)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        if not success:
            self.assertIn('assertion:', result.stdout + result.stderr)
        return result

    def task(self, role, file, name):
        return next(t for t in yaml.safe_load((role / 'tasks' / file).read_text()) if t.get('name') == name)

    def test_existing_topology_supports_single_mds_and_a_separate_tikv_host(self):
        path = self.root / 'plan.json'
        hostpath = self.root / 'dingo-hosts.yml'
        self.assert_run([
            {'include_role': {'name': 'dingofs_cluster', 'tasks_from': 'plan'}},
            {'copy': {'content': '{{ _dingo_add_topology | to_json }}', 'dest': str(path)}},
            {'template': {'src': str(REPO / 'roles/dingofs_cluster/templates/dingo-hosts.yaml.j2'),
                          'dest': str(hostpath)}},
        ])
        topology = json.loads(path.read_text())
        self.assertEqual([k for k in topology if k.endswith('_services')], ['mds_services'])
        mds = topology['mds_services']
        self.assertEqual(mds['deploy'], [{'host': '${machine1}', 'replicas': 1}])
        self.assertEqual(mds['config']['storage_url'], 'list://192.0.2.11:2379')
        self.assertEqual(mds['config']['bthread_concurrency'], 4)
        hosts = yaml.safe_load(hostpath.read_text())
        self.assertEqual(hosts['hosts'][0]['hostname'], '192.0.2.10')
        self.assertEqual(hosts['global']['private_key_file'], '/root/.ssh/id_ed25519_demo')

    def test_standalone_guard_rejects_ha_and_reserved_config_overrides(self):
        task = self.task(MDS, 'main.yml', 'Validate the lightweight single-MDS workflow')
        self.assert_run([task])
        for change in [{'mds_replicas': 3}, {'dingofs_mds_storage_engine': 'dingo-store'},
                       {'dingofs_mds_extra_config': {'storage_url': 'wrong'}},
                       {'dingo_cli_db_url': 'rqlite://localhost:4001'}]:
            with self.subTest(change=change):
                self.assert_run([task], overrides=change, success=False)
        self.assert_run([task], hosts=2, success=False)

    def test_port_guard_does_not_match_a_port_suffix_but_rejects_real_listeners(self):
        task = self.task(MDS, 'deploy.yml', 'Require the standalone MDS port to be unused')
        base = {'_dingo_add_plan': {'ports': {'mds0': [6900]}},
                '_standalone_listeners': {'stdout': 'LISTEN 0 128 [::]:16900 [::]:*'}}
        self.assert_run([task], overrides=base)
        base['_standalone_listeners']['stdout'] = 'LISTEN 0 128 0.0.0.0:6900 0.0.0.0:*'
        self.assert_run([task], overrides=base, success=False)

    def test_resource_update_targets_only_the_validated_registered_container(self):
        tasks = [self.task(MDS, 'deploy.yml', name) for name in [
            'Require one healthy MDS before selecting its container',
            'Select the registered MDS container', 'Require an actual Podman container ID']]
        cid = 'abcdef0123456789'
        overrides = {
            '_dingo_add_plan': {'instances': {'mds': {'mds0': 1}}},
            '_standalone_status': {'stdout': 'cluster name : demo\n1 mds mds0 1/1 %s Up 1 minute' % cid,
                                   'stdout_lines': ['cluster name : demo', '1 mds mds0 1/1 %s Up 1 minute' % cid]},
        }
        self.assert_run(tasks, overrides=overrides)
        overrides['_standalone_status']['stdout'] = overrides['_standalone_status']['stdout'].replace('Up 1 minute', 'Exited')
        self.assert_run(tasks, overrides=overrides, success=False)

    def test_mds_online_check_accepts_both_cli_cases_but_not_offline_or_header(self):
        source = self.task(MDS, 'deploy.yml', 'Verify the MDS is reachable through its advertised service address')
        task = {'assert': {'that': source['until']}}
        for state in ['online', 'ONLINE', 'offline', 'header-only']:
            stdout = '| ID | ADDR | STATE | LAST ONLINE TIME | ONLINESTATE |\n'
            if state != 'header-only':
                stdout += '| 1001 | 192.0.2.10:6900 | NORMAL | 2026-11-10 | %s |\n' % state
            with self.subTest(state=state):
                self.assert_run([task], overrides={'_standalone_mds_rpc': {'rc': 0, 'stdout': stdout}},
                                success=state in ['online', 'ONLINE'])

    def test_tiup_inputs_keep_backend_placement_and_existing_state_independent(self):
        topology = {
            'global': {'user': 'root'},
            'server_configs': {'pd': {'replication.max-replicas': 1, 'replication.enable-placement-rules': False}},
            'pd_servers': [{'host': '192.0.2.11', 'ignore_exporter': True}],
            'tikv_servers': [{'host': '192.0.2.11', 'ignore_exporter': True}],
        }
        tasks = [self.task(TIKV, 'main.yml', 'Require a single local TiUP backend, independently placed from MDS')]
        override = {'_tikv_topology': topology,
                    'ansible_facts': {'all_ipv4_addresses': ['192.0.2.11'], 'architecture': 'x86_64', 'service_mgr': 'systemd'}}
        self.assert_run(tasks, overrides=override, success=False)
        topology['tidb_servers'] = [{'host': '192.0.2.11'}]
        self.assert_run(tasks, overrides=override)
        topology['tidb_servers'].append({'host': '192.0.2.11'})
        self.assert_run(tasks, overrides=override, success=False)
        topology.pop('tidb_servers')
        preserve = self.task(TIKV, 'main.yml', 'Preserve existing TiUP deployments on re-runs')
        import base64
        override.update({'_tikv_registered': {'stat': {'exists': True}},
                         '_tikv_saved_topology': {'content': base64.b64encode(yaml.safe_dump(topology).encode()).decode()},
                         '_tikv_registered_meta': {'content': base64.b64encode(b'tidb_version: v8.5.8\n').decode()}})
        self.assert_run([preserve], overrides=override)
        override['tikv_version'] = 'v8.5.9'
        self.assert_run([preserve], overrides=override, success=False)

    def test_existing_backend_only_allows_explicit_tidb_addition_without_pd_kv_changes(self):
        import base64
        base = {'global': {'user': 'root'}, 'pd_servers': [{'host': '192.0.2.11'}],
                'tikv_servers': [{'host': '192.0.2.11'}]}
        desired = dict(base, tidb_servers=[{'host': '192.0.2.11'}])
        overrides = {'_tikv_topology': desired, '_tikv_registered': {'stat': {'exists': True}},
                     '_tikv_saved_topology': {'content': base64.b64encode(yaml.safe_dump(base).encode()).decode()},
                     '_tikv_registered_meta': {'content': base64.b64encode(b'tidb_version: v8.5.8\ntopology: {}\n').decode()}}
        task = self.task(TIKV, 'main.yml', 'Preserve existing TiUP deployments on re-runs')
        self.assert_run([task], overrides=overrides, success=False)
        overrides['tikv_standalone_add_tidb'] = True
        self.assert_run([task], overrides=overrides)
        desired['pd_servers'] = [{'host': '192.0.2.12'}]
        self.assert_run([task], overrides=overrides, success=False)
        desired['pd_servers'] = base['pd_servers']
        overrides['_tikv_registered_meta']['content'] = base64.b64encode(
            b'tidb_version: v8.5.8\ntopology:\n  tidb_servers:\n    - host: 192.0.2.11\n').decode()
        self.assert_run([task], overrides=overrides, success=False)

    def test_required_tidb_requires_local_placement_and_an_explicit_budget(self):
        task = self.task(TIKV, 'main.yml', 'Validate the required local TiDB instance')
        instance = {'host': '192.0.2.11', 'listen_host': '192.0.2.11', 'ignore_exporter': True,
                    'port': 4000, 'status_port': 10080,
                    'resource_control': {'memory_limit': '4096M', 'cpu_quota': '200%'}}
        overrides = {'_tikv_topology': {'pd_servers': [{'host': '192.0.2.11'}], 'tidb_servers': [instance]}}
        self.assert_run([task], overrides=overrides)
        instance['host'] = '192.0.2.12'
        self.assert_run([task], overrides=overrides, success=False)

    def test_mds_preflight_requires_a_running_tidb_for_this_site(self):
        all_tasks = yaml.safe_load((REPO / 'playbooks/tikv_verify.yml').read_text())[0]['tasks']
        names = ['Fail when any TiKV component is not Up',
                 "Require the site's TiDB SQL service before MDS deployment"]
        tasks = [next(t for t in all_tasks if t.get('name') == name) for name in names]
        base = ('192.0.2.11:2379 pd 192.0.2.11 2379/2380 linux/x86_64 Up|L /pd /deploy/pd\n'
                '192.0.2.11:20160 tikv 192.0.2.11 20160/20180 linux/x86_64 Up /kv /deploy/kv\n')
        for suffix, success in [('', False),
                                ('192.0.2.11:4000 tidb 192.0.2.11 4000/10080 linux/x86_64 Up - /tidb\n', True),
                                ('192.0.2.11:4000 tidb 192.0.2.11 4000/10080 linux/x86_64 Down - /tidb\n', False)]:
            with self.subTest(suffix=suffix):
                self.assert_run(tasks, overrides={'tikv_min_tidb': 1, '_tiup_display': {'stdout': base + suffix}}, success=success)

    def test_tiup_binary_without_root_manifest_is_initialized_once(self):
        home = self.root / 'tiup'
        (home / 'bin').mkdir(parents=True)
        binary = home / 'bin/tiup'
        binary.write_text('#!' + sys.executable + '\n' + '''
import json, os, sys
from pathlib import Path
home = Path(os.environ['TIUP_HOME'])
assert sys.argv[1:] == ['mirror', 'set', 'https://mirror.example.test']
with (home / 'calls').open('a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
(home / 'bin/root.json').write_text('trusted-root-fixture')
''')
        binary.chmod(0o755)
        tasks = [self.task(TIKV, 'deploy.yml', name) for name in [
            'Inspect TiUP repository initialization',
            'Initialize the TiUP repository when its root manifest is missing']]
        variables = {'tikv_tiup_mirror_url': 'https://mirror.example.test'}
        self.assert_run(tasks, overrides=variables)
        self.assertEqual(len((home / 'calls').read_text().splitlines()), 1)
        (home / 'bin/root.json').write_text('preserve-existing-root')
        self.assert_run(tasks, overrides=variables)
        self.assertEqual(len((home / 'calls').read_text().splitlines()), 1)
        self.assertEqual((home / 'bin/root.json').read_text(), 'preserve-existing-root')

    def test_tiup_health_waits_for_initial_store_registration(self):
        home = self.root / 'tiup'
        (home / 'bin').mkdir(parents=True)
        binary = home / 'bin/tiup'
        binary.write_text('#!' + sys.executable + '\n' + '''
import os
from pathlib import Path
counter = Path(os.environ['TIUP_HOME']) / 'display-count'
count = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(count + 1))
state = 'Down' if count == 0 else 'Up'
print('192.0.2.11:2379 pd 192.0.2.11 2379/2380 linux/x86_64 Up|L|UI /pd /deploy/pd')
print('192.0.2.11:20160 tikv 192.0.2.11 20160/20180 linux/x86_64 %s /kv /deploy/kv' % state)
if os.environ.get('WITH_TIDB') == 'yes':
    print('192.0.2.11:4000 tidb 192.0.2.11 4000/10080 linux/x86_64 Up - /deploy/tidb')
''')
        binary.chmod(0o755)
        task = self.task(TIKV, 'deploy.yml', 'Read the TiUP backend status')
        self.assert_run([task])
        self.assertEqual((home / 'display-count').read_text(), '2')
        task['environment']['WITH_TIDB'] = 'yes'
        self.assert_run([task], overrides={'_tikv_component_count': 3})


if __name__ == '__main__':
    unittest.main()
