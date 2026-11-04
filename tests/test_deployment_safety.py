"""Regression fixtures for mount ownership, CPU allocation and RDMA mapping."""
import argparse
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
            (port / 'state').write_text('4: ACTIVE')
            parent = root / 'sys/devices/pci0'
            (parent / 'sf1').mkdir(parents=True)
            (parent / 'sf1/numa_node').write_text('-1')
            (parent / 'numa_node').write_text('0')
            (device / 'device').symlink_to(parent / 'sf1')
            def mapped(value):
                value = str(value)
                return root / value.lstrip('/') if value.startswith('/sys/') else Path(value)
            with patch.dict(code, Path=mapped):
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
    def run_cri(self, mode):
        source = (REPO / 'roles/common/files/dingofs_mount_usage_check.sh').read_text()
        function = source[source.index('container_users_busy() {'):source.index('# --- Main')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / 'crictl'
            responses = {'pods': {'items': [{'id': 'pod', 'metadata': {'namespace': 'default', 'name': 'job'}}]},
                         'ps': {'containers': [{'id': 'container', 'podSandboxId': 'pod'}]},
                         'inspect': {'status': {'mounts': [{'hostPath': '/fixture/data', 'containerPath': '/data'}]}}}
            if mode == 'empty':
                responses = {'pods': {}, 'ps': {}}
            binary.write_text('#!' + sys.executable + '\nimport sys,json\nresponses=' + repr(responses)
                              + '\nmode=' + repr(mode) + '\n'
                              + 'if mode == "query_error" or (mode == "inspect_error" and sys.argv[1] == "inspect"): sys.exit(1)\n'
                              + 'print(json.dumps(responses[sys.argv[1]]))\n')
            binary.chmod(0o755)
            result = subprocess.run(['/bin/bash'], input='STRICT=true\nlog() { echo "$*"; }\n' + function
                                    + '\ncontainer_users_busy /fixture\nexit $?\n', text=True,
                                    capture_output=True, env=dict(os.environ, PATH=str(root) + ':' + os.environ['PATH']))
            return result

    def test_default_namespace_container_is_busy(self):
        result = self.run_cri('mounted')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('default/job', result.stdout)

    def test_empty_runtime_is_idle(self):
        self.assertEqual(self.run_cri('empty').returncode, 1)

    def test_runtime_and_inspect_errors_are_unknown(self):
        for mode in ['query_error', 'inspect_error']:
            with self.subTest(mode=mode):
                self.assertEqual(self.run_cri(mode).returncode, 2)


if __name__ == '__main__':
    unittest.main()
