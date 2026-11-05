"""Regression fixtures for mount ownership, CPU allocation and RDMA mapping."""
import argparse
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]


def embedded(path, delimiter):
    source = (REPO / path).read_text().split("<<'" + delimiter + "'\n", 1)[1].rsplit('\n' + delimiter, 1)[0]
    namespace = {'__name__': 'fixture'}
    exec(compile(source, str(path), 'exec'), namespace)
    return namespace


class CpuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.code = embedded('roles/dingo_cache/files/cpuset-detect.sh', 'PYCPU')
        self.write('/sys/devices/system/cpu/online', '0-7')
        self.write('/sys/devices/system/node/node0/cpulist', '0-3')
        self.write('/sys/devices/system/node/node1/cpulist', '4-7')
        for cpu in range(8):
            self.write('/sys/devices/system/cpu/cpu%d/topology/thread_siblings_list' % cpu,
                       '%s-%s' % (cpu // 2 * 2, cpu // 2 * 2 + 1))
        self.write('/sys/fs/cgroup/cgroup.controllers', 'cpuset')
        (self.root / 'proc').mkdir()
        self.args = argparse.Namespace(self_exe='/opt/self/dingo-cache', no_busy=True, exclude='', busy=30)

    def write(self, path, value):
        target = self.root / path.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)
        return target

    def mapped_path(self, value):
        value = str(value)
        if value.startswith(('/sys/', '/proc', '/etc/kubernetes/', '/var/lib/kubelet/')):
            return self.root / value.lstrip('/')
        return Path(value)

    def collect(self):
        with patch.dict(self.code, Path=self.mapped_path):
            return self.code['collect'](self.args)

    def test_sibling_same_name_is_claimed_but_current_executable_is_excluded(self):
        for pid, executable, cpus in [(900001, '/opt/other/dingo-cache', '0-1'),
                                      (900002, '/opt/self/dingo-cache', '4-5')]:
            self.write('/proc/%s/cmdline' % pid, executable + '\0--cpuset=' + cpus)
            self.write('/proc/%s/status' % pid, 'Cpus_allowed_list:\t' + cpus)
            (self.root / ('proc/%s/exe' % pid)).symlink_to(executable)
        online, nodes, siblings, claims, _ = self.collect()
        self.assertEqual(set(claims), {0, 1})
        node, selected, _ = self.code['choose'](nodes, siblings, set(claims), 2, 0)
        self.assertEqual((node, selected), (1, [4, 6]))

    def test_exclusion_of_one_sibling_reserves_the_whole_core(self):
        _, nodes, siblings, _, _ = self.collect()
        node, selected, _ = self.code['choose'](nodes, siblings, {1}, 2, 0)
        self.assertEqual((node, selected), (1, [4, 6]))

    def test_bad_kubelet_state_is_an_error(self):
        self.write('/var/lib/kubelet/cpu_manager_state', 'broken')
        with self.assertRaises(ValueError):
            self.collect()

    def test_range_parser_rejects_invalid_cpu_lists(self):
        for value in ['2-1', 'a', '1-2-3', '-1']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.code['expand'](value)
        self.assertEqual(self.code['compact']({0, 1, 2, 5}), '0-2,5')

    def test_cache_and_client_detectors_share_the_same_implementation(self):
        self.assertEqual((REPO / 'roles/dingo_cache/files/cpuset-detect.sh').read_bytes(),
                         (REPO / 'roles/dingo_client/files/cpuset-detect.sh').read_bytes())


class RdmaTests(unittest.TestCase):
    def test_sf_parent_numa_and_target_fabric_readiness(self):
        code = embedded('roles/common/files/check-ib-ready.sh', 'PYRDMA')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = root / 'sys/class/infiniband/mlx5_5'
            port = device / 'ports/1'
            (port / 'gid_attrs/ndevs').mkdir(parents=True)
            (port / 'gid_attrs/ndevs/0').write_text('eth5')
            (port / 'gid_attrs/ndevs/1').write_text('unused')
            (port / 'state').write_text('4: ACTIVE')
            parent = root / 'sys/devices/pci0'
            (parent / 'sf1').mkdir(parents=True)
            (parent / 'sf1/numa_node').write_text('-1')
            (parent / 'numa_node').write_text('0')
            (device / 'device').symlink_to(parent / 'sf1')
            def mapped(value):
                value = str(value)
                return root / value.lstrip('/') if value.startswith('/sys/') else Path(value)
            original_read = Path.read_text
            def read_attribute(path, *args, **kwargs):
                if path.parent.name == 'ndevs' and path.name == '1':
                    raise OSError(errno.EINVAL, 'unused GID slot')
                return original_read(path, *args, **kwargs)
            with patch.dict(code, Path=mapped), patch.object(Path, 'read_text', read_attribute):
                mapping = code['resolve']('eth5', 1)
                self.assertEqual(mapping['device'], 'mlx5_5')
                self.assertEqual(mapping['numa_node'], 0)
                links = [{'ifname': 'other-fabric', 'flags': ['UP'],
                          'addr_info': [{'family': 'inet', 'local': '192.168.1.2'}]}]
                with patch('subprocess.check_output', return_value=json.dumps(links).encode()):
                    self.assertFalse(code['ready'](mapping, '192.168.'))
                links[0]['ifname'] = 'eth5'
                with patch('subprocess.check_output', return_value=json.dumps(links).encode()):
                    self.assertTrue(code['ready'](mapping, '192.168.'))
                (port / 'state').write_text('1: DOWN')
                with patch('subprocess.check_output', return_value=json.dumps(links).encode()):
                    self.assertFalse(code['ready'](mapping, '192.168.'))
                (port / 'state').write_text('4: ACTIVE')
                port.rename(device / 'ports/2')
                with patch.dict(os.environ, {'IB_WAIT_TIMEOUT': '0', 'IB_IP_PREFIX': '192.168.'}, clear=True), \
                        patch.object(sys, 'argv', ['check-ib-ready.sh']), \
                        patch('subprocess.check_output', return_value=json.dumps(links).encode()):
                    self.assertEqual(code['main'](), 0)


class MountTests(unittest.TestCase):
    def run_cri(self, mode, strict=True, namespace='default'):
        source = (REPO / 'roles/common/files/dingofs_mount_usage_check.sh').read_text()
        function = source[source.index('container_users_busy() {'):source.index('# --- Main')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / 'crictl'
            responses = {'pods': {'items': [{'id': 'pod', 'metadata': {'namespace': namespace, 'name': 'job'}}]},
                         'ps': {'containers': [{'id': 'container', 'podSandboxId': 'pod'}]},
                         'inspect': {'status': {'mounts': [{'hostPath': '/fixture/data', 'containerPath': '/data'}]}}}
            if mode == 'empty':
                responses = {'pods': {}, 'ps': {}}
            binary.write_text('#!' + sys.executable + '\nimport sys,json\nresponses=' + repr(responses)
                              + '\nmode=' + repr(mode) + '\n'
                              + 'if mode == "query_error" or (mode == "inspect_error" and sys.argv[1] == "inspect"): sys.exit(1)\n'
                              + 'print(json.dumps(responses[sys.argv[1]]))\n')
            binary.chmod(0o755)
            # The subprocess fixture also runs on non-Linux controllers.
            function = function.replace('target_device = mounted_device(target)', 'target_device = None')
            result = subprocess.run(['/bin/bash'], input='STRICT=' + str(strict).lower() + '\nlog() { echo "$*"; }\n' + function
                                    + '\ncontainer_users_busy /fixture\nexit $?\n', text=True,
                                    capture_output=True, env=dict(os.environ, PATH=str(root) + ':' + os.environ['PATH']))
            return result

    def test_default_namespace_container_is_busy(self):
        result = self.run_cri('mounted')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('default/job', result.stdout)

    def test_default_scope_only_checks_cci_prefix(self):
        for namespace, busy in [('cci-job', True), ('cci123', True),
                                ('gpu-operator', False), ('default', False)]:
            with self.subTest(namespace=namespace):
                self.assertEqual(self.run_cri('mounted', False, namespace).returncode, 0 if busy else 1)

    def test_empty_runtime_is_idle(self):
        self.assertEqual(self.run_cri('empty').returncode, 1)

    def test_runtime_and_inspect_errors_are_unknown(self):
        for mode in ['query_error', 'inspect_error']:
            with self.subTest(mode=mode):
                self.assertEqual(self.run_cri(mode).returncode, 2)


class MountReferenceTests(unittest.TestCase):
    def setUp(self):
        self.code = embedded('roles/common/files/dingofs_mount_usage_check.sh', 'PY')

    def detail(self, source='/', destination='/host', pid=123):
        return {'info': {'pid': pid}, 'status': {'mounts': [
            {'hostPath': source, 'containerPath': destination}]}}

    def test_root_bind_does_not_hold_a_new_child_mount(self):
        rows = [{'path': '/host', 'device': '8:1', 'fstype': 'ext4'},
                {'path': '/host/mnt/public-fs', 'device': '0:10', 'fstype': 'fuse.dingofs'}]
        with patch.dict(self.code, mountinfo=lambda _: rows):
            self.assertEqual(self.code['container_references'](
                self.detail(), '/mnt/new/public-fs', None), [])

    def test_retained_child_mount_below_root_bind_is_busy(self):
        rows = [{'path': '/host/mnt/new/public-fs', 'device': '0:11', 'fstype': 'fuse.dingofs'}]
        with patch.dict(self.code, mountinfo=lambda _: rows):
            self.assertTrue(self.code['container_references'](self.detail(), '/mnt/new/public-fs', None))

    def test_direct_or_descendant_reference_survives_detached_mount(self):
        for source in ['/mnt/new', '/mnt/new/data']:
            self.assertTrue(self.code['container_references'](self.detail(source, '/data', None), '/mnt/new', None))

    def test_unmounted_siblings_on_root_device_are_not_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'old').mkdir(); (root / 'new').mkdir()
            self.assertEqual(self.code['container_references'](
                self.detail(str(root / 'old'), '/data', None), str(root / 'new'), None), [])

    def test_namespace_device_alias_is_preserved_for_mounted_target(self):
        rows = [{'path': '/some-alias', 'device': '0:11', 'fstype': 'fuse.dingofs'}]
        with patch.dict(self.code, mountinfo=lambda _: rows):
            self.assertTrue(self.code['container_references'](
                {'info': {'pid': 123}, 'status': {'mounts': []}}, '/mnt/new', '0:11'))

    def test_missing_namespace_information_is_unknown_not_idle(self):
        with self.assertRaises(ValueError):
            self.code['container_references'](self.detail(pid=None), '/mnt/new', None)
        with patch.dict(self.code, mountinfo=lambda _: (_ for _ in ()).throw(PermissionError('denied'))):
            with self.assertRaises(PermissionError):
                self.code['container_references'](self.detail(), '/mnt/new', None)

    def test_empty_and_pseudo_sources_do_not_resolve_to_current_directory(self):
        for source in ['', 'proc', 'tmpfs']:
            self.assertEqual(self.code['container_references'](self.detail(source), '/mnt/new', None), [])


if __name__ == '__main__':
    unittest.main()
