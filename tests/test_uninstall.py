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
from unittest.mock import patch

import yaml

REPO = Path(__file__).resolve().parents[1]
MODULE = REPO / 'roles/dingo_uninstall/library/dingofs_uninstall_plan.py'
spec = importlib.util.spec_from_file_location('uninstall_plan', MODULE)
plan = importlib.util.module_from_spec(spec); spec.loader.exec_module(plan)
state_spec = importlib.util.spec_from_file_location('mount_state', REPO / 'roles/dingo_uninstall/library/dingofs_mount_state.py')
state = importlib.util.module_from_spec(state_spec); state_spec.loader.exec_module(state)


class MountStateTests(unittest.TestCase):
    def test_exact_mount_and_bind_mount_paths(self):
        data = ('31 1 8:1 / / rw - ext4 /dev/root rw\n'
                '42 31 0:12 / /mnt/work rw - fuse.dingofs DingoFS:work rw\n'
                '43 31 0:12 /sub /mnt/alias rw - fuse.dingofs DingoFS:work rw\n')
        with patch.object(Path, 'read_text', return_value=data):
            self.assertTrue(state.mount_state('/mnt/work')['mounted'])
            self.assertTrue(state.mount_state('/mnt/alias')['mounted'])
            self.assertFalse(state.mount_state('/mnt/work/child')['mounted'])
            self.assertFalse(state.mount_state('/mnt/work-other')['mounted'])
            self.assertFalse(state.mount_state('/does-not-exist')['mounted'])

    def test_escaped_paths_are_decoded(self):
        with patch.object(Path, 'read_text', return_value='42 31 0:12 / /mnt/work\\040space rw - fuse.dingofs DingoFS:work rw\n'):
            self.assertTrue(state.mount_state('/mnt/work space')['mounted'])

    def test_read_or_parse_failure_cannot_become_unmounted(self):
        with patch.object(Path, 'read_text', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError): state.mount_state('/mnt/work')
        for malformed in ['', 'not mountinfo\n', '42 31 - fuse\n']:
            with patch.object(Path, 'read_text', return_value=malformed):
                with self.assertRaises(ValueError): state.mount_state('/mnt/work')

    @unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('mountpoint'), 'requires Linux mountpoint')
    def test_real_unmounted_directory_including_util_linux_rc_32(self):
        with tempfile.TemporaryDirectory() as directory:
            target = str(Path(directory).resolve())
            result = subprocess.run(['mountpoint', '-q', target], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            version = subprocess.run(['mountpoint', '--version'], capture_output=True, text=True)
            if 'util-linux' in version.stdout:
                self.assertEqual(result.returncode, 32)
            self.assertFalse(state.mount_state(target)['mounted'])
            self.assertFalse(state.mount_state(target + '/absent')['mounted'])


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
        base_mountinfo = '31 1 8:1 / / rw - ext4 /dev/fixture rw\n'
        mounts = self.root / 'mountinfo'; mounts.write_text(base_mountinfo)
        code = code.replace("MOUNTINFO = Path('/proc/self/mountinfo')", 'MOUNTINFO = Path(' + repr(str(mounts)) + ')')
        module.write_text(code)
        status_module = role / 'library/dingofs_mount_state.py'
        status_source = status_module.read_text()
        self.assertIn("MOUNTINFO = Path('/proc/self/mountinfo')", status_source)
        status_module.write_text(status_source.replace("MOUNTINFO = Path('/proc/self/mountinfo')", 'MOUNTINFO = Path(' + repr(str(mounts)) + ')'))
        # Isolate process inspection too: CI runs unprivileged and must not
        # inspect the runner's real /proc or depend on its current processes.
        proc = self.root / 'proc'; proc.mkdir()
        remove = role / 'tasks/remove.yml'
        source = remove.read_text()
        self.assertIn("pathlib.Path('/proc')", source)
        remove.write_text(source.replace("pathlib.Path('/proc')", 'pathlib.Path(' + repr(str(proc)) + ')'))
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
        live = proc / '98765'; live.mkdir()
        (live / 'exe').symlink_to(self.base / 'dingo-cache')
        (live / 'cmdline').write_bytes((str(self.base / 'dingo-cache') + '\0').encode())
        refused = subprocess.run(['ansible-playbook', '-i', 'fixture,', str(play), '--limit', 'fixture'],
                                 cwd=self.root, env=env, text=True, capture_output=True, timeout=120)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('still has running processes', refused.stdout + refused.stderr)
        self.assertTrue(self.unit.exists()); self.assertTrue(self.base.exists()); self.assertTrue(self.data.exists())
        self.assertEqual(registry.read_text(), 'present')
        shutil.rmtree(live)
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
        # The old uninstall path rejected util-linux's legitimate rc=32.
        mountpoint=fakebin/'mountpoint';mountpoint.write_text('#!/bin/sh\nexit 32\n');mountpoint.chmod(0o755)
        # An unmount that really fails must still block removal; no real host
        # mount/umount is used in this fixture.
        mounts.write_text('42 31 0:12 / '+str(new_mount)+' rw - fuse.dingofs DingoFS:same-name rw\n')
        refuse=self.root/'refuse-unmount';refuse.touch()
        unmount_events=self.root/'unmount-events'
        umount=fakebin/'umount'
        umount.write_text('#!'+sys.executable+'\nimport pathlib,sys,json\n'
                          +'with open('+repr(str(unmount_events))+',"a") as f:f.write(json.dumps(sys.argv[1:])+"\\n")\n'
                          +'if pathlib.Path('+repr(str(refuse))+').exists():sys.exit(16)\n'
                          +'pathlib.Path('+repr(str(mounts))+').write_text('+repr(base_mountinfo)+')\n')
        umount.chmod(0o755)
        result=subprocess.run(['ansible-playbook','-i','fixture,',str(play),'--limit','fixture'],
                              cwd=self.root,env=env,text=True,capture_output=True,timeout=120)
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertTrue(new_unit.exists());self.assertTrue(new_base.exists());self.assertTrue(new_log.exists())
        refuse.unlink()
        run([])
        self.assertFalse(new_unit.exists());self.assertFalse(new_base.exists());self.assertFalse(new_mount.exists())
        self.assertTrue(new_log.exists());self.assertTrue(self.old.exists())
        self.assertEqual([json.loads(v) for v in unmount_events.read_text().splitlines()],
                         [['--',str(new_mount)],['--',str(new_mount)]])

        # Recreate an already-detached installation, as after the client's own
        # shutdown auto-unmounts FUSE. No umount invocation should be needed.
        new_base.mkdir();new_mount.mkdir()
        new_unit.write_text('ExecStart='+str(new_base/'dingofs-mount.sh')+'\n')
        (new_base/'dingofs-mount.sh').write_text('g_mnt="'+str(new_mount)+'"\ng_fsname="same-name"\ng_log="'+str(new_log)+'"\ng_mdsaddr="127.0.0.1:7000"\n')
        before_unmount=unmount_events.read_text()
        run([])
        self.assertEqual(unmount_events.read_text(),before_unmount)
        self.assertFalse(new_unit.exists());self.assertFalse(new_base.exists());self.assertFalse(new_mount.exists())
        self.assertTrue(new_log.exists());self.assertTrue(self.old.exists())


if __name__ == '__main__':unittest.main()
