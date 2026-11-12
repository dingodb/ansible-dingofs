"""Regression fixtures for mount ownership, CPU allocation and RDMA mapping."""
import argparse
import contextlib
import errno
import io
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
            self.write('/proc/%s/task/%s/status' % (pid, pid), 'Cpus_allowed_list:\t' + cpus)
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

    def readiness_fixture(self):
        """One ACTIVE RDMA port with an UP netdev carrying a business IPv4."""
        code = embedded('roles/common/files/check-ib-ready.sh', 'PYRDMA')
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        device = root / 'sys/class/infiniband/mlx5_5'
        port = device / 'ports/2'
        (port / 'gid_attrs/ndevs').mkdir(parents=True)
        (port / 'gid_attrs/ndevs/0').write_text('eth5')
        (port / 'state').write_text('4: ACTIVE')
        (device / 'device').symlink_to(root / 'sys/devices/pci0')
        (root / 'sys/devices/pci0').mkdir(parents=True)
        (root / 'sys/devices/pci0/numa_node').write_text('0')

        def mapped(value):
            value = str(value)
            return root / value.lstrip('/') if value.startswith('/sys/') else Path(value)

        links = [{'ifname': 'eth5', 'flags': ['UP'],
                  'addr_info': [{'family': 'inet', 'local': '192.168.10.5'}]}]
        return code, mapped, json.dumps(links).encode()

    def test_readiness_ignores_the_configured_device_name(self):
        code, mapped, links = self.readiness_fixture()
        with patch.dict(code, Path=mapped), \
                patch.dict(os.environ, {'IB_WAIT_TIMEOUT': '0', 'IB_IP_PREFIX': '192.168.',
                                        'RDMA_DEVICE': 'mlx5_missing', 'RDMA_PORT_NUM': '1'}, clear=True), \
                patch.object(sys, 'argv', ['check-ib-ready.sh']), \
                patch('subprocess.check_output', return_value=links):
            self.assertEqual(code['main'](), 0)

    def test_readiness_fails_when_no_interface_matches_the_prefix(self):
        code, mapped, _ = self.readiness_fixture()
        links = json.dumps([{'ifname': 'eth5', 'flags': ['UP'],
                             'addr_info': [{'family': 'inet', 'local': '10.0.0.5'}]}]).encode()
        with patch.dict(code, Path=mapped), \
                patch.dict(os.environ, {'IB_WAIT_TIMEOUT': '0', 'IB_IP_PREFIX': '192.168.'}, clear=True), \
                patch.object(sys, 'argv', ['check-ib-ready.sh']), \
                patch('subprocess.check_output', return_value=links), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(code['main'](), 1)
        self.assertIn('IB_IP_PREFIX', stderr.getvalue())

    def test_readiness_rejects_multiple_ready_ports(self):
        code = embedded('roles/common/files/check-ib-ready.sh', 'PYRDMA')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for device_name, netdev in [('mlx5_5', 'eth5'), ('mlx5_6', 'eth6')]:
                port = root / ('sys/class/infiniband/%s/ports/1' % device_name)
                (port / 'gid_attrs/ndevs').mkdir(parents=True)
                (port / 'gid_attrs/ndevs/0').write_text(netdev)
                (port / 'state').write_text('4: ACTIVE')
                parent = root / ('sys/devices/%s' % device_name)
                parent.mkdir(parents=True)
                (parent / 'numa_node').write_text('0')
                (root / ('sys/class/infiniband/%s/device' % device_name)).symlink_to(parent)
            links = json.dumps([
                {'ifname': 'eth5', 'flags': ['UP'],
                 'addr_info': [{'family': 'inet', 'local': '192.168.10.5'}]},
                {'ifname': 'eth6', 'flags': ['UP'],
                 'addr_info': [{'family': 'inet', 'local': '192.168.11.5'}]}]).encode()

            def mapped(value):
                value = str(value)
                return root / value.lstrip('/') if value.startswith('/sys/') else Path(value)

            with patch.dict(code, Path=mapped), \
                    patch.dict(os.environ, {'IB_WAIT_TIMEOUT': '0', 'IB_IP_PREFIX': '192.168.'}, clear=True), \
                    patch.object(sys, 'argv', ['check-ib-ready.sh']), \
                    patch('subprocess.check_output', return_value=links), \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(code['main'](), 1)
        self.assertIn('found 2', stderr.getvalue())

    def test_readiness_retries_until_the_fabric_appears(self):
        code, mapped, links = self.readiness_fixture()
        with patch.dict(code, Path=mapped), \
                patch.dict(os.environ, {'IB_WAIT_TIMEOUT': '2', 'IB_POLL_INTERVAL': '1',
                                        'IB_IP_PREFIX': '192.168.'}, clear=True), \
                patch.object(sys, 'argv', ['check-ib-ready.sh']), \
                patch('subprocess.check_output',
                      side_effect=[subprocess.TimeoutExpired(['ip'], 10), links]):
            self.assertEqual(code['main'](), 0)

    def test_service_units_do_not_pass_a_device_name(self):
        for path in ['roles/dingo_client/templates/dingofs-mount.service.j2',
                     'roles/dingo_cache/templates/dingo-cache.service.j2']:
            with self.subTest(path=path):
                unit = (REPO / path).read_text()
                self.assertNotIn('RDMA_DEVICE', unit)
                self.assertNotIn('RDMA_PORT_NUM', unit)

    def discovery_fixture(self, devices, expose_netdev_link=True, with_gid=True):
        """Build sysfs for (device, port, netdev) rows and return mapped Path."""
        code = embedded('roles/common/files/check-ib-ready.sh', 'PYRDMA')
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for name, port_num, netdev in devices:
            port = root / ('sys/class/infiniband/%s/ports/%d' % (name, port_num))
            (port / 'gid_attrs/ndevs').mkdir(parents=True)
            if with_gid:
                (port / 'gid_attrs/ndevs/0').write_text(netdev)
            (port / 'state').write_text('4: ACTIVE')
            parent = root / ('sys/devices/%s' % name)
            parent.mkdir(parents=True)
            (parent / 'numa_node').write_text('0')
            (root / ('sys/class/infiniband/%s/device' % name)).symlink_to(parent)
            if expose_netdev_link:
                link = root / ('sys/class/net/%s/device/infiniband/%s' % (netdev, name))
                link.mkdir(parents=True)

        def mapped(value):
            value = str(value)
            return root / value.lstrip('/') if value.startswith('/sys/') else Path(value)

        return code, mapped

    @staticmethod
    def link(name, address):
        return {'ifname': name, 'flags': ['UP'],
                'addr_info': [{'family': 'inet', 'local': address}]}

    def test_device_for_prefix_follows_the_interface_to_its_rdma_device(self):
        code, mapped = self.discovery_fixture([('mlx5_1', 1, 'roce2003')])
        links = json.dumps([self.link('roce2003', '11.11.2.16')]).encode()
        with patch.dict(code, Path=mapped), patch('subprocess.check_output', return_value=links):
            mapping = code['device_for_prefix']('11.11')
        self.assertEqual((mapping['device'], mapping['port']), ('mlx5_1', 1))
        self.assertEqual(mapping['netdevs'], ['roce2003'])

    def test_device_for_prefix_uses_the_netdev_sysfs_link_without_gid_data(self):
        code, mapped = self.discovery_fixture([('mlx5_1', 1, 'roce2003')], with_gid=False)
        links = json.dumps([self.link('roce2003', '11.11.2.16')]).encode()
        with patch.dict(code, Path=mapped), patch('subprocess.check_output', return_value=links):
            mapping = code['device_for_prefix']('11.11')
        self.assertEqual((mapping['device'], mapping['port']), ('mlx5_1', 1))

    def test_device_for_prefix_requires_one_interface_in_the_prefix(self):
        code, mapped = self.discovery_fixture([('mlx5_1', 1, 'roce2003')])
        links = json.dumps([self.link('roce2003', '10.0.0.5')]).encode()
        with patch.dict(code, Path=mapped), patch('subprocess.check_output', return_value=links), \
                self.assertRaisesRegex(ValueError, 'IB_IP_PREFIX 11.11'):
            code['device_for_prefix']('11.11')

    def test_device_for_prefix_rejects_two_rdma_devices(self):
        code, mapped = self.discovery_fixture(
            [('mlx5_1', 1, 'roce2003'), ('mlx5_2', 1, 'roce2004')])
        links = json.dumps([self.link('roce2003', '11.11.2.16'),
                            self.link('roce2004', '11.11.3.16')]).encode()
        with patch.dict(code, Path=mapped), patch('subprocess.check_output', return_value=links), \
                self.assertRaisesRegex(ValueError, '2 RDMA'):
            code['device_for_prefix']('11.11')

    def test_device_for_prefix_cli_prints_json(self):
        code, mapped = self.discovery_fixture([('mlx5_1', 1, 'roce2003')])
        links = json.dumps([self.link('roce2003', '11.11.2.16')]).encode()
        output = io.StringIO()
        with patch.object(sys, 'argv', ['check-ib-ready.sh', '--device-for-prefix', '11.11']), \
                patch.dict(code, Path=mapped), \
                contextlib.redirect_stdout(output), \
                patch('subprocess.check_output', return_value=links):
            self.assertEqual(code['main'](), 0)
        self.assertEqual(json.loads(output.getvalue())['device'], 'mlx5_1')

    def device_status_fixture(self, status_calls):
        """Run device_status() against a sysfs fixture and patched ibv_devices."""
        code = embedded('roles/common/files/check-ib-ready.sh', 'PYRDMA')
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / 'sys/class/infiniband/ib6s200p0').mkdir(parents=True)

        def mapped(value):
            value = str(value)
            return root / value.lstrip('/') if value.startswith('/sys/') else Path(value)

        with patch.dict(code, Path=mapped), patch('subprocess.check_output', **status_calls):
            return code, code['device_status']('ib6s200p0')

    def test_device_status_uses_kernel_inventory_and_optional_ibv_devices(self):
        self.assertEqual(self.device_status_fixture(
            {'side_effect': FileNotFoundError('ibv_devices')})[1], 'present-no-ibv-devices')
        self.assertEqual(self.device_status_fixture(
            {'return_value': b'device node GUID\nib6s200p0 1234\n'})[1], 'present')
        self.assertEqual(self.device_status_fixture(
            {'return_value': b'device node GUID\nmlx5_0 1234\n'})[1], 'missing-verbs')

    def test_unknown_sysfs_device_is_missing_regardless_of_ibv_devices(self):
        code = embedded('roles/common/files/check-ib-ready.sh', 'PYRDMA')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def mapped(value):
                value = str(value)
                return root / value.lstrip('/') if value.startswith('/sys/') else Path(value)

            with patch.dict(code, Path=mapped), patch(
                    'subprocess.check_output', return_value=b'ib0 1234\n'):
                self.assertEqual(code['device_status']('ib0'), 'missing-kernel')

    def test_ibv_devices_failure_is_distinct_from_an_absent_utility(self):
        for error in [subprocess.CalledProcessError(1, ['ibv_devices'], stderr=b'no permission'),
                      subprocess.TimeoutExpired(['ibv_devices'], 10),
                      PermissionError(errno.EACCES, 'denied', 'ibv_devices')]:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(self.device_status_fixture({'side_effect': error})[1],
                                 'present-ibv-devices-error')

    def test_device_status_cli_prints_the_status(self):
        code = embedded('roles/common/files/check-ib-ready.sh', 'PYRDMA')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sys/class/infiniband/ib6s200p0').mkdir(parents=True)

            def mapped(value):
                value = str(value)
                return root / value.lstrip('/') if value.startswith('/sys/') else Path(value)

            output = io.StringIO()
            with patch.object(sys, 'argv', ['check-ib-ready.sh', '--device-status', 'ib6s200p0']), \
                    patch.dict(code, Path=mapped), \
                    contextlib.redirect_stdout(output), \
                    patch('subprocess.check_output', return_value=b'ib6s200p0 1234\n'):
                self.assertEqual(code['main'](), 0)
            self.assertEqual(output.getvalue().strip(), 'present')


@unittest.skipUnless(shutil.which('ansible-playbook'), 'requires Ansible')
class RdmaRoleContractTests(unittest.TestCase):
    def run_status(self, status, stderr=''):
        tasks = yaml.safe_load((REPO / 'roles/dingo_client/tasks/main.yml').read_text())
        start = next(i for i, t in enumerate(tasks) if t.get('name') == 'Check RDMA device exists (client hosts)')
        end = next(i for i, t in enumerate(tasks) if t.get('name') == 'Fail when RDMA device is missing (client hosts)')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / 'check-ib-ready.sh'
            script = '#!/bin/bash\n'
            if stderr:
                script += "printf '%s\\n' '" + stderr + "' >&2\n"
            script += 'printf "%s\\n" "' + status + '"\n'
            helper.write_text(script)
            helper.chmod(0o755)
            play = root / 'play.yml'
            play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False,
                'vars': {'ansible_connection': 'local', 'ansible_become': False,
                         'ansible_python_interpreter': sys.executable,
                         'IB_CHECK_SCRIPT': str(helper),
                         'dingo_client_use_rdma': True,
                         'dingo_client_rdma_device': 'ib6s200p0',
                         'dingo_client_rdma_device_effective': 'ib6s200p0'},
                'tasks': tasks[start:end + 1]}]))
            return subprocess.run(['ansible-playbook', '-i', 'localhost,', str(play)], cwd=REPO,
                env=dict(os.environ, ANSIBLE_CONFIG=str(REPO / 'ansible.cfg')),
                capture_output=True, text=True, timeout=60)

    def test_kernel_presence_is_enough_when_the_utility_is_absent(self):
        result = self.run_status('present-no-ibv-devices')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('ibv_devices is not installed', result.stdout)

    def test_a_failing_utility_warns_with_its_reason_but_does_not_block(self):
        result = self.run_status('present-ibv-devices-error', 'ibv_devices: permission denied')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('ibv_devices could not run', result.stdout)
        self.assertIn('permission denied', result.stdout)

    def test_a_confirmed_device_needs_no_warning(self):
        result = self.run_status('present')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('WARNING', result.stdout)

    def test_missing_devices_still_fail_the_play(self):
        for status in ['missing-kernel', 'missing-verbs']:
            with self.subTest(status=status):
                result = self.run_status(status)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(status, result.stdout)


@unittest.skipUnless(shutil.which('ansible-playbook'), 'requires Ansible')
class RdmaAutoDiscoveryTests(unittest.TestCase):
    def run_play(self, configured):
        tasks = yaml.safe_load((REPO / 'roles/dingo_client/tasks/main.yml').read_text())
        start = next(i for i, t in enumerate(tasks)
                     if t.get('name') == 'Discover the RDMA device from the IB network prefix (client hosts)')
        end = next(i for i, t in enumerate(tasks) if t.get('name') == 'Report missing RDMA NUMA information')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / 'check-ib-ready.sh'
            helper.write_text(
                '#!/bin/bash\n'
                'if [ "$1" = "--device-for-prefix" ]; then\n'
                '  printf \'%s\\n\' \'{"device":"mlx5_1","port":2,"netdevs":["roce2003"],"numa_node":0}\'\n'
                'else\n'
                '  printf \'%s\\n\' \'{"device":"mlx5_9","port":1,"netdevs":["eth9"],"numa_node":1}\'\n'
                'fi\n')
            helper.chmod(0o755)
            selected = root / 'selected'
            play = root / 'play.yml'
            play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False,
                'vars': {'ansible_connection': 'local', 'ansible_become': False,
                         'ansible_python_interpreter': sys.executable,
                         'IB_CHECK_SCRIPT': str(helper), 'IB_IP_PREFIX': '11.11',
                         'dingo_client_use_rdma': True,
                         'dingo_client_rdma_device': configured,
                         'dingo_client_rdma_port_num': 1},
                'tasks': tasks[start:end + 1] + [
                    {'copy': {'content': '{{ dingo_client_rdma_device_effective }} '
                                         '{{ dingo_client_rdma_port_num_effective }}',
                              'dest': str(selected)}}]}]))
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(play)], cwd=REPO,
                env=dict(os.environ, ANSIBLE_CONFIG=str(REPO / 'ansible.cfg')),
                capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return selected.read_text().strip()

    def test_auto_mode_selects_the_device_that_carries_the_prefix(self):
        self.assertEqual(self.run_play(''), 'mlx5_1 2')

    def test_explicit_device_still_overrides_discovery(self):
        self.assertEqual(self.run_play('mlx5_9'), 'mlx5_9 1')


class MountTests(unittest.TestCase):
    def run_cri(self, mode, strict=True, namespace='default'):
        source = (REPO / 'roles/common/files/dingofs_mount_usage_check.sh').read_text()
        function = source[source.index('container_users_busy() {'):source.index('# --- Main')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / 'crictl'
            (root / 'python3').symlink_to(sys.executable)
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
                                    capture_output=True, env=dict(os.environ, PATH=str(root)))
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
