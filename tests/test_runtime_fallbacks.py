"""Single-node RDMA inference and container-runtime fallback safety."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('runtime_fixtures', Path(__file__).with_name('test_deployment_safety.py'))
fixtures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)


class SingleNodeRdmaTests(unittest.TestCase):
    def test_only_a_single_online_node_can_supply_missing_device_locality(self):
        code = fixtures.embedded('roles/common/files/check-ib-ready.sh', 'PYRDMA')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = root / 'sys/class/infiniband/mlx5_0'
            (device / 'ports/1/gid_attrs/ndevs').mkdir(parents=True)
            (device / 'ports/1/gid_attrs/ndevs/0').write_text('ib0')
            pci = root / 'sys/devices/pci0'
            pci.mkdir(parents=True)
            (pci / 'numa_node').write_text('-1')
            (device / 'device').symlink_to(pci)
            topology = root / 'sys/devices/system/node'
            topology.mkdir(parents=True)
            def mapped(value):
                value = str(value)
                return root / value.lstrip('/') if value.startswith('/sys/') else Path(value)
            with patch.dict(code, Path=mapped):
                for node in [0, 2]:
                    (topology / 'online').write_text(str(node))
                    (topology / ('node%s' % node)).mkdir(exist_ok=True)
                    (topology / ('node%s/cpulist' % node)).write_text('0-63')
                    result = code['resolve']('ib0', 1)
                    self.assertEqual(result['numa_node'], node)
                    self.assertEqual(result['numa_source'], 'single-online-node')
                (topology / 'online').write_text('0-1')
                self.assertIsNone(code['resolve']('ib0', 1)['numa_node'])
                (pci / 'numa_node').write_text('1')
                self.assertEqual(code['resolve']('ib0', 1)['numa_node'], 1)


class RuntimeFallbackTests(unittest.TestCase):
    def setUp(self):
        self.code = fixtures.embedded('roles/common/files/dingofs_mount_usage_check.sh', 'PY')
        self.code['mounted_device'] = lambda target: None
        self.code['log'] = lambda message: None

    def backend(self, runtime_namespace='k8s.io', namespace='cci-job', source='/mnt/public-fs/data', destination='/data'):
        def ctr(*args):
            if args == ('namespaces', 'list', '-q'):
                return runtime_namespace + '\n'
            if args[-2:] == ('tasks', 'list'):
                return 'TASK PID STATUS\ncontainer1 123 RUNNING\n'
            if args[-3:] == ('containers', 'info', 'container1'):
                labels = {'io.kubernetes.pod.namespace': namespace} if namespace else {}
                return json.dumps({'Labels': labels, 'Spec': {'mounts': [{'source': source, 'destination': destination}]}})
            raise AssertionError(args)
        return ctr

    def test_ctr_preserves_kubernetes_namespace_scope(self):
        for namespace, expected in [('cci-job', 1), ('cci123', 1), ('default', 0), ('gpu-operator', 0)]:
            with self.subTest(namespace=namespace), patch.dict(self.code, ctr=self.backend(namespace=namespace)):
                self.assertEqual(self.code['check_ctr']('/mnt/public-fs'), expected)
                self.assertEqual(self.code['check_ctr']('/mnt/public-fs', True), 1)

    def test_standalone_containerd_tasks_are_checked_without_kubernetes_labels(self):
        with patch.dict(self.code, ctr=self.backend('default', '')):
            self.assertEqual(self.code['check_ctr']('/mnt/public-fs'), 1)

    def test_ctr_root_bind_uses_the_live_task_mount_namespace(self):
        rows = [{'device': '0:10', 'path': '/host/mnt/public-fs', 'fstype': 'fuse.dingofs'}]
        with patch.dict(self.code, ctr=self.backend(source='/', destination='/host'), mountinfo=lambda path: rows):
            self.assertEqual(self.code['check_ctr']('/mnt/public-fs'), 1)

    def test_unknown_pod_namespace_and_runtime_errors_cannot_become_idle(self):
        backend = self.backend()
        with patch.dict(self.code, ctr=lambda *a: backend(*a).replace('RUNNING', 'UNKNOWN')):
            with self.assertRaisesRegex(ValueError, 'unrecognized ctr task state'):
                self.code['check_ctr']('/mnt/public-fs')
        with patch.dict(self.code, ctr=self.backend(namespace='')):
            with self.assertRaisesRegex(ValueError, 'cannot determine Kubernetes namespace'):
                self.code['check_ctr']('/mnt/public-fs')
        with patch.object(self.code['shutil'], 'which', return_value='/fake/tool'), \
                patch.dict(self.code, check_cri=lambda *a: (_ for _ in ()).throw(ValueError('bad CRI')),
                           ctr=lambda *a: (_ for _ in ()).throw(ValueError('bad ctr'))):
            with self.assertRaisesRegex(ValueError, 'bad ctr'):
                self.code['check']('/mnt/public-fs')

    def test_missing_or_failed_crictl_falls_back_to_ctr(self):
        for available in [{'ctr'}, {'ctr', 'crictl'}]:
            with patch.object(self.code['shutil'], 'which', side_effect=lambda name: '/fake/' + name if name in available else None), \
                    patch.dict(self.code, check_cri=lambda *a: (_ for _ in ()).throw(ValueError('bad CRI')), ctr=self.backend()):
                self.assertEqual(self.code['check']('/mnt/public-fs'), 1)

    def test_successful_cri_does_not_hide_standalone_containerd_users(self):
        with patch.object(self.code['shutil'], 'which', return_value='/fake/tool'), \
                patch.dict(self.code, check_cri=lambda *a: 0, ctr=self.backend('default', '')):
            self.assertEqual(self.code['check']('/mnt/public-fs'), 1)

    def test_plain_host_is_checked_but_active_uninspectable_runtime_is_unknown(self):
        with patch.object(self.code['shutil'], 'which', return_value=None), \
                patch.dict(self.code, runtime_present=lambda: False, check_plain_namespaces=lambda target: 0):
            self.assertEqual(self.code['check']('/mnt/public-fs'), 0)
        with patch.object(self.code['shutil'], 'which', return_value=None), \
                patch.dict(self.code, runtime_present=lambda: True):
            with self.assertRaisesRegex(ValueError, 'active'):
                self.code['check']('/mnt/public-fs')

    def test_plain_namespace_retained_aliases_are_not_missed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'proc/self/ns').mkdir(parents=True)
            (root / 'proc/self/ns/mnt').symlink_to('mnt:[host]')
            (root / 'proc/1/ns').mkdir(parents=True)
            (root / 'proc/1/ns/mnt').symlink_to('mnt:[host]')
            process = root / 'proc/123'
            (process / 'ns').mkdir(parents=True)
            (process / 'ns/mnt').symlink_to('mnt:[other]')
            info = process / 'mountinfo'
            def mapped(value):
                value = str(value)
                return root / value.lstrip('/') if value.startswith('/proc') else Path(value)
            readlink = os.readlink
            with patch.dict(self.code, Path=mapped), patch.object(os, 'readlink', side_effect=lambda path: readlink(mapped(path))):
                info.write_text('1 0 8:1 / / rw - ext4 /dev/root rw\n')
                self.assertEqual(self.code['check_plain_namespaces']('/mnt/public-fs'), 0)
                info.write_text('1 0 0:10 / /data rw - fuse.dingofs DingoFS:fs rw\n')
                with self.assertRaisesRegex(ValueError, 'cannot correlate'):
                    self.code['check_plain_namespaces']('/mnt/public-fs')
                with patch.dict(self.code, mounted_device=lambda target: '0:10'):
                    self.assertEqual(self.code['check_plain_namespaces']('/mnt/public-fs'), 1)


if __name__ == '__main__':
    unittest.main()
