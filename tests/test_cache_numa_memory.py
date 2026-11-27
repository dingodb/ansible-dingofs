"""Cache v5.2 startup budgets and node-local memory snapshots."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from library.dingofs_cache_numa_memory import inspect_memory, pool_budget_mib


class CacheNumaMemoryTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def node(self, node, total, free, file_pages=0, shmem=0, reclaimable=0):
        directory = self.root / 'node{}'.format(node)
        directory.mkdir(exist_ok=True)
        values = dict(MemTotal=total, MemFree=free, FilePages=file_pages, Shmem=shmem, SReclaimable=reclaimable)
        (directory / 'meminfo').write_text(''.join('Node {} {}: {} kB\n'.format(node, name, value * 1024)
                                                  for name, value in values.items()))

    def test_v52_data_and_message_pools_are_rounded_per_shard(self):
        self.assertEqual(pool_budget_mib(8, 512, 256), (4096, 8256))
        self.assertEqual(pool_budget_mib(3, 5, 1), (24, 36))

    def test_insufficient_target_stays_blocked_with_reclaimable_pages_and_other_nodes(self):
        self.node(0, total=65536, free=2048, file_pages=18000, shmem=2000, reclaimable=1000)
        self.node(2, total=65536, free=16384)
        result = inspect_memory(self.root, 0, 8, 512, 256, 0)
        self.assertFalse(result['ready'])
        self.assertEqual(result['required_mib'], 12352)
        self.assertEqual([node['estimated_available_mib'] for node in result['nodes']], [19048, 16384])
        self.assertEqual([node['free_pct'] for node in result['nodes']], [3.12, 25.0])
        self.assertTrue(inspect_memory(self.root, 2, 8, 512, 256, 0)['ready'])

    def test_exact_budget_passes_and_configured_floor_can_block(self):
        self.node(0, total=32768, free=12352)
        self.assertTrue(inspect_memory(self.root, 0, 8, 512, 256, 0)['ready'])
        result = inspect_memory(self.root, 0, 8, 512, 256, 12353)
        self.assertFalse(result['ready'])
        self.assertEqual(result['required_mib'], 12353)

    def test_missing_or_unreliable_target_memory_cannot_pass(self):
        self.node(1, total=32768, free=16384)
        with self.assertRaisesRegex(ValueError, 'NUMA 0 has no sysfs memory information'):
            inspect_memory(self.root, 0, 8, 512, 256, 0)
        (self.root / 'node1/meminfo').write_text('Node 1 MemTotal: 123 kB\n')
        with self.assertRaisesRegex(ValueError, 'invalid or missing MemTotal/MemFree for NUMA 1'):
            inspect_memory(self.root, 1, 8, 512, 256, 0)


if __name__ == '__main__':
    unittest.main()
