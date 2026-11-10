"""Failure fixtures for issue triage; no real mounts, disks or services."""
import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

import jinja2
import yaml

REPO = Path(__file__).resolve().parents[1]


def render(path, variables):
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters['bool'] = bool
    env.filters['quote'] = shlex.quote
    return env.from_string((REPO / path).read_text()).render(**variables)


class MonitorTests(unittest.TestCase):
    def test_status_errors_and_unknown_output_cannot_mean_absent(self):
        spec = importlib.util.spec_from_file_location('monitor_fixture', REPO / 'filter_plugins/dingo_monitor.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        header = 'Id      Role        Host    Container Id  Status\n'
        self.assertEqual(module.monitor_count(header), 0)
        self.assertEqual(module.monitor_count(header + 'abcdef  grafana  node  -  Cleaned\n'), 0)
        self.assertEqual(module.monitor_count(header + 'abcdef  grafana  node  123456  Up 3 hours\n'), 1)
        for output in ['', 'connect: connection refused', header + 'garbled output']:
            with self.subTest(output=output), self.assertRaises(Exception):
                module.monitor_count(output)


class ShellTests(unittest.TestCase):
    def test_mount_registry_matches_exact_host_and_path_fields(self):
        source = (REPO / 'roles/dingo_client/templates/dingofs-mount.sh.j2').read_text()
        start = source.index("mnt_info=$(printf")
        end = source.index('\nif [ -z "$mnt_info" ]', start)
        script = "g_mnt=/dingofs/a; local_hosts='node1 10.0.0.1'; mountpoints=\"$FIXTURE_ROWS\"\n" + source[start:end] + '\nprintf "%s" "$mnt_info"'
        rows = '\n'.join(['1 fs client node10:100:/dingofs/a true',
                          '1 fs client node1:101:/dingofs/ab true',
                          '1 fs client node1:102:/dingofs/a-backup true',
                          '1 fs client node1:103:/dingofs/a true',
                          '1 fs client 10.0.0.1:104:/dingofs/a true'])
        result = subprocess.run(['/bin/bash'], input=script, capture_output=True, text=True,
                                env=dict(os.environ, FIXTURE_ROWS=rows))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['node1:103:/dingofs/a', '10.0.0.1:104:/dingofs/a'])

    def test_protected_names_are_literal_shell_values(self):
        values = ['a"b', 'x$y', 'x;echo bad', '$(touch SHOULD_NOT_EXIST)', "quote'file"]
        template = (REPO / 'roles/cron_cleanup/templates/cleanup_and_compression.sh.j2').read_text()
        line = next(line for line in template.splitlines() if line.startswith('PROTECTED_FILES='))
        env = jinja2.Environment()
        env.filters['quote'] = shlex.quote
        script = env.from_string(line).render(cron_cleanup_protected_files=values)
        result = subprocess.run(['/bin/bash'], input=script + '\nprintf "%s\\n" "${PROTECTED_FILES[@]}"',
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), values)

    def test_failed_tar_discards_partial_archive_and_keeps_sources(self):
        source = (REPO / 'roles/cron_cleanup/templates/cleanup_and_compression.sh.j2').read_text()
        start = source.index('            local archive_name=')
        end = source.index('\n        else:', start) if '\n        else:' in source[start:] else source.index('\n        else\n', start)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / 'original.log'
            log.write_text('keep me')
            listing = root / 'list'
            listing.write_bytes(b'./original.log\0')
            # This real shell path simulates a tar that writes output then fails.
            script = '''tar() { : > "$2"; return 1; }
run() {
compressed_count=1
compress_list=''' + shlex.quote(str(listing)) + '\n' + source[start:end] + '\n}\nrun\n'
            result = subprocess.run(['/bin/bash'], input=script, cwd=root, text=True, capture_output=True)
            self.assertIn('Failed to create archive', result.stderr)
            self.assertTrue(log.exists())
            self.assertEqual(list(root.glob('*.tar.gz')), [])
            self.assertEqual(list(root.glob('.archive_*')), [])

    def test_data_repair_never_formats_an_existing_volume(self):
        defaults = yaml.safe_load((REPO / 'roles/lvm_data/defaults/main.yml').read_text())
        for repair, mounted, filesystem, success in [(False, False, 'xfs', False),
                                                    (True, False, 'xfs', True),
                                                    (True, True, 'xfs', True),
                                                    (True, False, '', False)]:
            with self.subTest(repair=repair, mounted=mounted, fs=filesystem), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fstab = root / 'fstab'
                fstab.write_text('')
                target = root / 'disk1'
                events = root / 'events'
                variables = dict(defaults, lvm_data_mount_count=1, lvm_data_mount_prefix=str(root / 'disk'), lvm_data_repair=repair)
                source = render('roles/lvm_data/templates/setup_lvm_data.sh.j2', variables)
                source = source[:source.index('# --- 1.')].replace('/etc/fstab', str(fstab))
                setup = '''vgs() { return 0; }
findmnt() { ''' + ('echo /dev/vg_disk1/lv' if mounted else 'return 1') + '''; }
readlink() { echo "$2"; }
blkid() { if [ "$4" = UUID ]; then echo fixture-uuid; else echo "''' + filesystem + '''"; fi; }
vgchange() { echo activate >> "''' + str(events) + '''"; }
mount() { echo mount >> "''' + str(events) + '''"; }
pvcreate() { echo DESTRUCTIVE >> "''' + str(events) + '''"; return 99; }
mkfs() { echo DESTRUCTIVE >> "''' + str(events) + '''"; return 99; }
'''
                result = subprocess.run(['/bin/bash'], input=setup + source, text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                self.assertNotIn('DESTRUCTIVE', events.read_text() if events.exists() else '')
                if success:
                    self.assertIn('UUID=fixture-uuid ' + str(target), fstab.read_text())
                elif not repair:
                    self.assertIn('HALF-PROVISIONED', result.stderr)

    def test_cache_index_skips_foreign_mounts_and_fstab_reservations(self):
        defaults = yaml.safe_load((REPO / 'roles/lvm_cache/defaults/main.yml').read_text())
        source = render('roles/lvm_cache/templates/setup_lvm.sh.j2', defaults)
        source = source[source.index('require_empty_target()'):source.index('index=1\n', source.index('require_empty_target()'))]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fstab = root / 'fstab'
            fstab.write_text('/dev/foreign /mnt/disk2 ext4 defaults 0 0\n')
            source = source.replace('/etc/fstab', str(fstab))
            result = subprocess.run(['/bin/bash'], input='vgs() { return 1; }\nmountpoint() { [ "$2" = /mnt/disk1 ]; }\n' + source + '\nindex=1; next_free_index; echo "$index"', capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), '3')


@unittest.skipUnless(shutil.which('ansible-playbook'), 'requires Ansible')
class CacheIdentityTests(unittest.TestCase):
    def test_missing_and_empty_id_regenerate_but_an_existing_id_is_preserved(self):
        tasks = yaml.safe_load((REPO / 'roles/dingo_cache/tasks/main.yml').read_text())
        start = next(i for i, t in enumerate(tasks) if t.get('name') == 'Check for existing config file')
        end = next(i for i, t in enumerate(tasks) if t.get('name') == 'Set config ID fact')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conf = root / 'dingo-cache.conf'
            ids = []
            for content in [None, '--listen_ip=127.0.0.1\n', '--id=\n', '--id=keep-existing\n']:
                if content is None:
                    conf.unlink(missing_ok=True)
                else:
                    conf.write_text(content)
                selected = tasks[start:end + 1] + [{'copy': {'content': '{{ dingo_cache_config_id }}', 'dest': str(root / 'id')}}]
                play = root / 'play.yml'
                play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False,
                    'vars': {'ansible_connection': 'local', 'ansible_become': False,
                             'ansible_python_interpreter': sys.executable, 'dingo_cache_base_dir': str(root)},
                    'tasks': selected}]))
                result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(play)], cwd=REPO,
                    env=dict(os.environ, ANSIBLE_CONFIG=str(REPO / 'ansible.cfg')), capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                ids.append((root / 'id').read_text())
            self.assertEqual(len(ids[0]), 36)
            self.assertEqual(ids[:3], [ids[0]] * 3)
            self.assertEqual(ids[3], 'keep-existing')

@unittest.skipUnless(shutil.which('ansible-playbook'), 'requires Ansible')
class SecurityTests(unittest.TestCase):
    def test_monitor_first_install_is_distinct_from_a_failed_status_query(self):
        tasks=yaml.safe_load((REPO / 'roles/dingo_monitor/tasks/main.yml').read_text())
        start=next(i for i,t in enumerate(tasks) if t.get('name') == 'Read the stored monitor configuration')
        end=next(i for i,t in enumerate(tasks) if t.get('name') == 'Deploy the monitor stack')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            cli=root / 'dingo'
            cli.write_text('#!/bin/sh\nif [ "$2" = config ]; then printf "%s\\n" "$SAVED_CONFIG"; else echo "status probe failed" >&2; exit 1; fi\n')
            cli.chmod(0o755)
            for saved,success in [('<empty monitor>',True), ('-',True), ('grafana: {}',False), ('',False)]:
                witness=root / 'deployed'
                witness.unlink(missing_ok=True)
                play=root / 'play.yml'
                play.write_text(yaml.safe_dump([{'hosts':'localhost','gather_facts':False,
                    'vars':{'ansible_connection':'local','ansible_become':False,
                            'ansible_python_interpreter':sys.executable,'dingo_cli_bin_dir':str(root),'dingofs_user':'root'},
                    'environment':{'SAVED_CONFIG':saved}, 'tasks':tasks[start:end]+[
                        {'copy':{'content':'deploy authorized','dest':str(witness)}, 'when':'_monitor_existing_count | int == 0'}]}]))
                result=subprocess.run(['ansible-playbook','-i','localhost,',str(play)],cwd=REPO,
                    env=dict(os.environ,ANSIBLE_CONFIG=str(REPO / 'ansible.cfg')),capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode == 0,success,result.stdout+result.stderr)
                self.assertEqual(witness.exists(),success)

    def test_rqlite_rejects_implicit_insecure_defaults_and_renders_authenticated_join(self):
        defaults = yaml.safe_load((REPO / 'roles/rqlite/defaults/main.yml').read_text())
        tasks = yaml.safe_load((REPO / 'roles/rqlite/tasks/main.yml').read_text())
        start = next(i for i,t in enumerate(tasks) if t.get('name','').startswith('Require authenticated rqlite'))
        end = next(i for i,t in enumerate(tasks) if t.get('name','').startswith('Prepare rqlite tarball'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            variables = dict(defaults, ansible_connection='local', ansible_become=False,
                             ansible_python_interpreter=sys.executable, rqlite_node_id=2,
                             ansible_host='10.0.0.2', rqlite_join_address='10.0.0.1')
            users = [{'username':'operator','password':'fixture-secret-123','perms':['all']}]
            cases = [({}, False), ({'rqlite_auth_users': users, 'rqlite_join_username':'operator'}, True),
                     ({'rqlite_auth_users': [dict(users[0], username='*')], 'rqlite_join_username':'*'}, False),
                     ({'rqlite_auth_users': users, 'rqlite_join_username':''}, False)]
            inv = root / 'inventory.yml'
            inv.write_text(yaml.safe_dump({'all':{'children':{'rqlite_slaves':{'hosts':{'fixture':{}}}}}}))
            for override, success in cases:
                play = root / 'play.yml'
                play.write_text(yaml.safe_dump([{'hosts':'fixture','gather_facts':False,
                    'vars':dict(variables, **override), 'tasks':tasks[start:end] + [
                        {'template':{'src':str(REPO / 'roles/rqlite/templates/rqlite.service.j2'), 'dest':str(root / 'unit')}}]}]))
                result = subprocess.run(['ansible-playbook','-i',str(inv),str(play)],cwd=REPO,
                    env=dict(os.environ,ANSIBLE_CONFIG=str(REPO / 'ansible.cfg')),capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode == 0,success,result.stdout+result.stderr)
                self.assertNotIn('fixture-secret-123',result.stdout+result.stderr)
                if success:
                    unit=(root / 'unit').read_text()
                    self.assertIn('User=rqlite',unit)
                    self.assertIn('-auth /etc/rqlite/auth.json',unit)
                    self.assertIn('-join 10.0.0.1:4001 -join-as operator',unit)


@unittest.skipUnless(shutil.which('ansible-playbook'), 'requires Ansible')
class CacheUpgradeTests(unittest.TestCase):
    def test_interrupted_restart_is_retried_even_when_copy_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / 'cache'
            base.mkdir()
            installed = base / 'dingo-cache'
            installed.write_text('old binary')
            installed.chmod(0o755)
            (base / 'dingo-cache.conf').write_text('--listen_ip=127.0.0.1\n')
            candidate = root / 'candidate'
            candidate.write_text('new binary')
            active = root / 'proc/123/exe'
            active.parent.mkdir(parents=True)
            active.write_text('old binary')
            fail = root / 'fail-restart'
            fail.touch()
            events = root / 'events'
            library = root / 'library'
            library.mkdir()
            (library / 'systemd.py').write_text("""from ansible.module_utils.basic import AnsibleModule
from pathlib import Path
import os,shutil
m=AnsibleModule(argument_spec=dict(name=dict(),state=dict()))
Path(os.environ['CACHE_EVENTS']).open('a').write('restart\\n')
if Path(os.environ['CACHE_FAIL']).exists():m.fail_json(msg='injected restart failure')
shutil.copyfile(os.environ['CACHE_INSTALLED'],os.environ['CACHE_ACTIVE'])
m.exit_json(changed=True)
""")
            (library / 'wait_for.py').write_text("""from ansible.module_utils.basic import AnsibleModule
m=AnsibleModule(argument_spec=dict(host=dict(),port=dict(),delay=dict(),timeout=dict()))
m.exit_json(changed=False)
""")
            fakebin = root / 'bin'
            fakebin.mkdir()
            systemctl = fakebin / 'systemctl'
            systemctl.write_text('#!/bin/sh\nif [ "$1" = show ]; then echo 123; else echo active; fi\n')
            systemctl.chmod(0o755)
            source = (REPO / 'playbooks/cache_06_upgrade.yml').read_text()
            source = source.replace('../roles/dingo_cache/tasks/preflight_binary.yml', str(REPO / 'roles/dingo_cache/tasks/preflight_binary.yml'))
            source = source.replace('/proc/$pid/exe', str(root / 'proc') + '/$pid/exe')
            tasks = yaml.safe_load(source)[0]['tasks']
            variables = dict(ansible_connection='local', ansible_become=False,
                             ansible_python_interpreter=sys.executable, dingo_cache_base_dir=str(base),
                             dingo_cache_bin_name='dingo-cache', dingo_cache_bin_local_src=str(candidate),
                             dingo_cache_service_name='fixture-cache', dingo_cache_listen_port=19080)
            play = root / 'play.yml'
            play.write_text(yaml.safe_dump([{'hosts':'localhost','gather_facts':False,'vars':variables,
                'environment':{'PATH':str(fakebin)+':'+os.environ['PATH'], 'CACHE_EVENTS':str(events),
                               'CACHE_FAIL':str(fail), 'CACHE_ACTIVE':str(active), 'CACHE_INSTALLED':str(installed)},
                'tasks':tasks}]))
            def run():
                return subprocess.run(['ansible-playbook','-i','localhost,',str(play)],cwd=REPO,
                    env=dict(os.environ,ANSIBLE_CONFIG=str(REPO / 'ansible.cfg'), ANSIBLE_LIBRARY=str(library)),
                    capture_output=True,text=True,timeout=45)
            result=run()
            self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(installed.read_text(),'new binary')
            self.assertTrue((base / '.restart-required').exists())
            fail.unlink()
            for attempt in range(2):
                result=run()
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertFalse((base / '.restart-required').exists())
                self.assertEqual(events.read_text().splitlines(),['restart','restart'])
            active.write_text('old binary from before marker support')
            result=run()
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(events.read_text().splitlines(),['restart'] * 3)


if __name__ == '__main__':
    unittest.main()
