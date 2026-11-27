#!/usr/bin/python
"""Check currently free NUMA memory against a v5.2 Cache startup budget."""
from pathlib import Path
import re


DOCUMENTATION = r'''
module: dingofs_cache_numa_memory
short_description: Check free NUMA memory for the Cache RDMA pools
options:
  numa_node:
    description: NUMA node assigned to the Cache process.
    type: int
    required: true
  shards:
    description: Number of Cache shards.
    type: int
    required: true
  buffer_pool_mb:
    description: Data buffer pool size in MiB per shard.
    type: int
    required: true
  rdma_max_connections:
    description: Maximum RDMA connections per shard.
    type: int
    required: true
  min_free_mb:
    description: Optional absolute free memory floor in MiB.
    type: int
    default: 0
  enforce:
    description: Fail the task when the target NUMA node has insufficient free memory.
    type: bool
    default: false
supports_check_mode: true
'''

NODE_ROOT = Path('/sys/devices/system/node')
MEMINFO_ROW = re.compile(r'^Node\s+(\d+)\s+(\w+):\s+(\d+)\s+kB$')
SUPERBLOCK_MIB = 4
KIB_PER_MIB = 1024


def pool_budget_mib(shards, buffer_pool_mb, rdma_max_connections):
    if any(not 0 < value <= 4294967295 for value in (shards, buffer_pool_mb, rdma_max_connections)):
        raise ValueError('shards, buffer_pool_mb and rdma_max_connections must be positive uint32 values')
    # Match BufferPool::InitOnAllShards and MessagePoolSuperblockCount in v5.2.
    # The Cache role supplies these three flags. v5.2 supplies a 4096-byte
    # message size and 256 in-flight RPCs per connection by default.
    superblock_bytes = SUPERBLOCK_MIB * KIB_PER_MIB**2
    message_budget = 2 * 256 + 2
    message_bytes = 2 * message_budget * 4096 * rdma_max_connections
    message_superblocks = (message_bytes + superblock_bytes - 1) // superblock_bytes + 1
    data_superblocks = (buffer_pool_mb + SUPERBLOCK_MIB - 1) // SUPERBLOCK_MIB
    return (shards * data_superblocks * SUPERBLOCK_MIB,
            shards * message_superblocks * SUPERBLOCK_MIB)


def read_node(node_dir, node):
    values = {}
    for line in (node_dir / 'meminfo').read_text().splitlines():
        row = MEMINFO_ROW.fullmatch(line)
        if row and int(row[1]) == node:
            values[row[2]] = int(row[3])
    total, free = values.get('MemTotal'), values.get('MemFree')
    if total is None or free is None or free > total:
        raise ValueError('invalid or missing MemTotal/MemFree for NUMA {}'.format(node))
    # Report the same reclaimable-memory estimate used for the locality alert.
    # The startup gate uses MemFree because RDMA registers and pins pages.
    available = max(0, min(total, free + values.get('FilePages', 0) - values.get('Shmem', 0)
                           + values.get('SReclaimable', 0)))
    return total, free, available


def inspect_memory(node_root, numa_node, shards, buffer_pool_mb, rdma_max_connections, min_free_mb):
    if numa_node < 0 or min_free_mb < 0:
        raise ValueError('numa_node and min_free_mb must be nonnegative')
    data_mib, messages_mib = pool_budget_mib(shards, buffer_pool_mb, rdma_max_connections)
    required_mib = max(data_mib + messages_mib, min_free_mb)
    snapshots = {int(path.name[4:]): read_node(path, int(path.name[4:]))
                 for path in node_root.glob('node[0-9]*') if path.name[4:].isdigit()}
    if numa_node not in snapshots:
        raise ValueError('NUMA {} has no sysfs memory information'.format(numa_node))
    nodes = []
    for node, (total, free, available) in sorted(snapshots.items()):
        nodes.append(dict(numa_node=node, total_mib=round(total / KIB_PER_MIB, 2),
                          free_mib=round(free / KIB_PER_MIB, 2),
                          free_pct=round(free * 100 / total, 2) if total else 0,
                          estimated_available_mib=round(available / KIB_PER_MIB, 2),
                          estimated_available_pct=round(available * 100 / total, 2) if total else 0))
    return dict(numa_node=numa_node, ready=snapshots[numa_node][1] >= required_mib * KIB_PER_MIB,
                required_mib=required_mib, data_pool_mib=data_mib, rdma_message_pool_mib=messages_mib,
                min_free_mb=min_free_mb, nodes=nodes)


def insufficient_message(result):
    node_summary = '. '.join('NUMA {numa_node} free {free_mib:.2f} MiB ({free_pct:.2f}%), '
                            'estimated available {estimated_available_mib:.2f} MiB '
                            '({estimated_available_pct:.2f}%)'.format(**node) for node in result['nodes'])
    return ('Cache startup on NUMA {numa_node} needs {required_mib} MiB free '
            '(data pools {data_pool_mib} MiB, RDMA message pools {rdma_message_pool_mib} MiB, '
            'configured floor {min_free_mb} MiB). {node_summary}. Free memory or reduce the pool budget. '
            'For a deliberate per-host NUMA override, set dingofs_numa_require_ib=false and choose '
            'matching dingo_cache_cpuset_numa_node and dingo_cache_numactl_node. '
            'Set dingo_cache_numa_mem_gate=false to opt out for that host.').format(node_summary=node_summary, **result)


def main():
    from ansible.module_utils.basic import AnsibleModule

    module = AnsibleModule(argument_spec=dict(
        numa_node=dict(type='int', required=True), shards=dict(type='int', required=True),
        buffer_pool_mb=dict(type='int', required=True), rdma_max_connections=dict(type='int', required=True),
        min_free_mb=dict(type='int', default=0), enforce=dict(type='bool', default=False),
    ), supports_check_mode=True)
    params = module.params
    try:
        result = inspect_memory(NODE_ROOT, params['numa_node'], params['shards'], params['buffer_pool_mb'],
                                params['rdma_max_connections'], params['min_free_mb'])
    except (OSError, ValueError) as error:
        module.fail_json(msg='Cannot check Cache NUMA memory: {}'.format(error), changed=False)
    if params['enforce'] and not result['ready']:
        module.fail_json(msg=insufficient_message(result), changed=False, **result)
    module.exit_json(changed=False, **result)


if __name__ == '__main__':
    main()
