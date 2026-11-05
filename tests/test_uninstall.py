"""Uninstall plans and real Ansible tasks operate only on temporary fixtures."""
import importlib.util
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
MODULE = REPO / 'roles/dingo_uninstall/library/dingofs_uninstall_plan.py'
spec = importlib.util.spec_from_file_location('uninstall_plan', MODULE)
plan = importlib.util.module_from_spec(spec); spec.loader.exec_module(plan)


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dingo-uninstall-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.units = self.root / 'units'; self.units.mkdir()
        self.state = self.root / 'state'
        self.base = self.root / 'cache-v52'; self.base.mkdir()
        self.data = self.root / 'data-v52'; self.data.mkdir()
        self.logs = self.root / 'logs-v52'; self.logs.mkdir()
        (self.data / 'block').write_text('keep data')
        (self.logs / 'log').write_text('keep logs')
        self.service = 'dingo-cache-v52'
        self.unit = self.units / (self.service + '.service')
        self.unit.write_text('[Service]\nExecStart=' + str(self.base / 'dingo-cache-start.sh') + '\n')
        (self.base / 'dingo-cache-start.sh').write_text('#!/bin/sh\n')
        self.config = self.base / 'dingo-cache.conf'
        self.config.write_text('--cache_dir=' + str(self.data) + '\n--log_dir=' + str(self.logs)
                               + '\n--group_name=test\n--mds_addrs=127.0.0.1:7000\n--id=member\n--listen_ip=127.0.0.2\n--listen_port=11100\n')
        self.p = dict(kind='cache', service=self.service, base_dir=str(self.base), mount_point='', fsname='',
                      mds_addrs='127.0.0.1:7000', group='test', remove_installation=True,
                      remove_data=False, remove_logs=False, unregister=True)
        self.old = self.units / 'dingofs-mount-old.service'
        self.old_base = self.root / 'old-client'; self.old_base.mkdir()
        self.old.write_text('ExecStart=' + str(self.old_base / 'dingofs-mount.sh') + '\n')
        (self.old_base / 'dingofs-mount.sh').write_text('g_mnt="/mnt/old"\ng_fsname="same-name"\ng_log="'
                                                     + str(self.root / 'old-log') + '"\ng_mdsaddr="127.0.0.1:6900"\n')

    def build(self, **values):
        return plan.make_plan(dict(self.p, **values), self.units, self.state, mounts=[])

    def test_defaults_preserve_cache_data_and_logs(self):
        value = self.build()
        self.assertEqual(value['remove_paths'], [str(self.base)])
        self.assertEqual(set(value['retained_paths']), {str(self.data), str(self.logs)})
        self.assertTrue(value['unregister'])

    def test_purge_flags_are_independent(self):
        self.assertEqual(set(self.build(remove_data=True)['remove_paths']), {str(self.base), str(self.data)})
        self.assertEqual(set(self.build(remove_logs=True)['remove_paths']), {str(self.base), str(self.logs)})

    def test_wrong_service_or_mds_cannot_remove_another_instance(self):
        for values in [dict(base_dir=str(self.old_base)), dict(mds_addrs='127.0.0.1:6900'), dict(group='old')]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.build(**values)

    def test_broad_symlink_and_mounted_directories_are_refused(self):
        for value in ['/', '/mnt/disk1', '/root/dingofs/dingo-client', '/root/../etc']:
            with self.subTest(path=value), self.assertRaises(ValueError):
                plan.safe_path(value)
        link = self.root / 'alias'; link.symlink_to(self.data)
        with self.assertRaises(ValueError): plan.safe_path(str(link))
        with self.assertRaises(ValueError):
            plan.make_plan(dict(self.p, remove_data=True), self.units, self.state,
                           mounts=[dict(path=str(self.data), fstype='xfs', source='/dev/fixture')])

    def test_shared_data_with_another_service_is_refused(self):
        other = self.root / 'other'; other.mkdir()
        (self.units / 'dingo-cache-old.service').write_text('ExecStart=' + str(other / 'dingo-cache-start.sh') + '\n')
        (other / 'dingo-cache.conf').write_text('--cache_dir=' + str(self.data) + '\n')
        with self.assertRaises(ValueError): self.build(remove_data=True)

    def test_retained_data_inside_installation_prevents_recursive_delete(self):
        self.config.write_text(self.config.read_text().replace(str(self.data), str(self.base / 'data')))
        with self.assertRaises(ValueError): self.build()
        self.assertEqual(self.build(remove_installation=False)['remove_paths'], [])

    def test_saved_plan_allows_a_later_purge(self):
        metadata = self.build()['metadata']; metadata['unregistered'] = True
        self.state.mkdir(); (self.state / (self.service + '.json')).write_text(json.dumps(metadata))
        self.unit.unlink(); shutil.rmtree(self.base)
        value = self.build(remove_data=True, remove_logs=True)
        self.assertFalse(value['unregister'])
        self.assertEqual(set(value['remove_paths']), {str(self.data), str(self.logs)})

    def test_absent_new_client_does_not_match_old_filesystem_name(self):
        value = self.build(kind='client', service='dingofs-mount-new-same-name',
                           base_dir=str(self.root / 'new-client'), mount_point=str(self.root / 'new-mount'),
                           fsname='same-name')
        self.assertTrue(value['noop'])
        self.assertTrue(self.old.exists())

    @unittest.skipUnless(shutil.which('ansible-playbook'), 'requires Ansible')
    def test_real_tasks_check_default_later_purge_and_idempotence(self):
        roles = self.root / 'roles'; role = roles / 'dingo_uninstall'
        shutil.copytree(REPO / 'roles/dingo_uninstall', role)
        module = role / 'library/dingofs_uninstall_plan.py'
        code = module.read_text()
        self.assertIn("UNIT_DIR = Path('/etc/systemd/system')", code)
        code = code.replace("UNIT_DIR = Path('/etc/systemd/system')", 'UNIT_DIR = Path(' + repr(str(self.units)) + ')')
        mounts = self.root / 'mountinfo'; mounts.write_text('')
        code = code.replace("MOUNTINFO = Path('/proc/self/mountinfo')", 'MOUNTINFO = Path(' + repr(str(mounts)) + ')')
        module.write_text(code)
        library = self.root / 'library'; library.mkdir()
        events = self.root / 'events'
        (library / 'systemd.py').write_text('''from ansible.module_utils.basic import AnsibleModule
import os,json
m=AnsibleModule(argument_spec=dict(name=dict(),state=dict(),enabled=dict(type='bool'),daemon_reload=dict(type='bool')),supports_check_mode=True)
if not m.check_mode:
 with open(os.environ['UNINSTALL_EVENTS'],'a') as f:f.write(json.dumps(m.params)+'\\n')
m.exit_json(changed=not m.check_mode)
''')
        fakebin = self.root / 'bin'; fakebin.mkdir()
        systemctl = fakebin / 'systemctl'
        systemctl.write_text('#!' + sys.executable + '\nimport pathlib,sys\np=pathlib.Path(' + repr(str(self.units))
                            + ')/(sys.argv[2] if sys.argv[2].endswith(".service") else sys.argv[2]+".service")\n'
                            + 'print("LoadState="+("loaded" if p.exists() else "not-found"))\nprint("ActiveState=inactive")\nprint("FragmentPath="+(str(p) if p.exists() else ""))\n')
        systemctl.chmod(0o755)
        registry = self.root / 'registry'; registry.write_text('present')
        cli = self.root / 'dingo'
        cli.write_text('#!' + sys.executable + '\nimport pathlib,sys,json\np=pathlib.Path(' + repr(str(registry)) + ')\n'
                       + 'if "leave" in sys.argv:p.write_text("absent")\n'
                       + 'members=[dict(member_id="member",ip="127.0.0.2",port=11100)] if p.read_text()=="present" else []\n'
                       + 'print(json.dumps(dict(error=dict(code=0),result=dict(members=members))))\n')
        cli.chmod(0o755)
        play = self.root / 'play.yml'
        variables = dict(ansible_connection='local',ansible_become=False,ansible_python_interpreter=sys.executable,
                         dingo_uninstall_kind='cache',dingo_cache_service_name=self.service,dingo_cache_base_dir=str(self.base),
                         dingo_cache_mds_addrs='127.0.0.1:7000',dingo_cache_group_name='test',
                         dingo_uninstall_state_dir=str(self.state),dingo_uninstall_cli=str(cli))
        play.write_text(yaml.safe_dump([dict(hosts='all',gather_facts=False,become=False,vars=variables,
                          environment=dict(PATH=str(fakebin)+':'+os.environ['PATH'],UNINSTALL_EVENTS=str(events)),roles=['dingo_uninstall'])]))
        env = dict(os.environ,ANSIBLE_ROLES_PATH=str(roles),ANSIBLE_LIBRARY=str(library),ANSIBLE_STDOUT_CALLBACK='default')
        def run(args):
            r=subprocess.run(['ansible-playbook','-i','fixture,',str(play),'--limit','fixture']+args,
                             cwd=self.root,env=env,text=True,capture_output=True,timeout=120)
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)
            return r.stdout
        run(['--check'])
        self.assertFalse(events.exists()); self.assertTrue(self.unit.exists()); self.assertTrue(self.base.exists())
        run([])
        self.assertFalse(self.unit.exists()); self.assertFalse(self.base.exists())
        self.assertTrue(self.data.exists()); self.assertTrue(self.logs.exists())
        self.assertEqual(registry.read_text(),'absent'); self.assertTrue(self.old.exists())
        purge=['-e','dingo_uninstall_remove_data=true','-e','dingo_uninstall_remove_logs=true']
        run(purge)
        self.assertFalse(self.data.exists()); self.assertFalse(self.logs.exists()); self.assertTrue(self.old.exists())
        last=run(purge); self.assertIn('changed=0',last)
        for line in events.read_text().splitlines():
            event=json.loads(line)
            self.assertIn(event.get('name'),[None,self.service+'.service'])

        # A busy client must fail before stopping or removing either version.
        new_base=self.root/'new-client';new_base.mkdir()
        new_mount=self.root/'new-mount';new_mount.mkdir()
        new_log=self.root/'new-log';new_log.mkdir()
        new_service='dingofs-mount-new-same-name'
        new_unit=self.units/(new_service+'.service')
        new_unit.write_text('ExecStart='+str(new_base/'dingofs-mount.sh')+'\n')
        (new_base/'dingofs-mount.sh').write_text('g_mnt="'+str(new_mount)+'"\ng_fsname="same-name"\ng_log="'+str(new_log)+'"\ng_mdsaddr="127.0.0.1:7000"\n')
        checker=roles/'common/files/dingofs_mount_usage_check.sh';checker.parent.mkdir(parents=True)
        checker.write_text('#!/bin/sh\nexit 1\n');checker.chmod(0o755)
        variables.update(dingo_uninstall_kind='client',dingo_client_mds_addrs='127.0.0.1:7000',
                         dingo_clients=[dict(fsname='same-name',mount_point=str(new_mount),
                                             base_dir=str(new_base),service_name=new_service)])
        definition=yaml.safe_load(play.read_text());definition[0]['vars']=variables
        play.write_text(yaml.safe_dump(definition))
        before=events.read_text()
        result=subprocess.run(['ansible-playbook','-i','fixture,',str(play),'--limit','fixture'],
                              cwd=self.root,env=env,text=True,capture_output=True,timeout=120)
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(events.read_text(),before)
        self.assertTrue(new_unit.exists());self.assertTrue(new_base.exists());self.assertTrue(self.old.exists())

        # Once idle, uninstall never recursively deletes the mountpoint contents.
        checker.write_text('#!/bin/sh\nexit 0\n')
        mountpoint=fakebin/'mountpoint';mountpoint.write_text('#!/bin/sh\nexit 1\n');mountpoint.chmod(0o755)
        run([])
        self.assertFalse(new_unit.exists());self.assertFalse(new_base.exists());self.assertFalse(new_mount.exists())
        self.assertTrue(new_log.exists());self.assertTrue(self.old.exists())


if __name__ == '__main__':unittest.main()
