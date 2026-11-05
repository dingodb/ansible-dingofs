#!/usr/bin/env bash
# One Python process collects CPU ownership and selects preferred/fallback NUMA.
exec python3 - "$@" <<'PYCPU'
"""Read host CPU ownership once, then select a NUMA-local set."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time


def expand(value):
    result = set()
    for part in value.strip().split(','):
        if not part:
            continue
        bounds = part.split('-')
        if len(bounds) > 2 or not all(x.isdigit() for x in bounds):
            raise ValueError('invalid CPU list: ' + value)
        first, last = int(bounds[0]), int(bounds[-1])
        if last < first:
            raise ValueError('reversed CPU range')
        result.update(range(first, last + 1))
    return result


def compact(values):
    values = sorted(values)
    spans = []
    for value in values:
        if spans and value == spans[-1][1] + 1:
            spans[-1][1] = value
        else:
            spans.append([value, value])
    return ','.join(str(a) if a == b else '%s-%s' % (a, b) for a, b in spans)


def read(path, default=None):
    try:
        return Path(path).read_text().strip()
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def sample():
    result = {}
    for line in read('/proc/stat').splitlines():
        fields = line.split()
        if re.fullmatch(r'cpu\d+', fields[0]):
            values = list(map(int, fields[1:9]))
            result[int(fields[0][3:])] = (sum(values), values[3] + values[4])
    return result


def process_state(process):
    """A zombie leader may still have workers after pthread_exit()."""
    try:
        status = (process / 'status').read_text()
    except FileNotFoundError:
        if process.exists():
            raise ValueError('process status unavailable for pid=' + process.name)
        return None, 0
    state = re.search(r'^State:\s*(\S)', status, re.M)
    threads = re.search(r'^Threads:\s*(\d+)', status, re.M)
    return state.group(1) if state else '?', int(threads.group(1)) if threads else 1


def process_alive(process):
    """Distinguish normal exit races from incomplete data for a live process."""
    state, threads = process_state(process)
    return state is not None and (state not in ('Z', 'X', 'x') or threads > 1)


def declared_cpus(argv):
    """Keep explicit argv reservations without matching text inside arguments.

    Flagfiles are deliberately not reread as runtime truth: their current
    contents can differ from the running process. Thread affinity below also
    covers processes that obtained their CPU settings from a flagfile.
    """
    cpus = set()
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == '--':
            break
        if arg in ('--cpuset', '-cpuset'):
            index += 1
            if index == len(argv):
                raise ValueError('cpuset argument has no value')
            cpus.update(expand(argv[index]))
        elif arg.startswith(('--cpuset=', '-cpuset=')):
            cpus.update(expand(arg.split('=', 1)[1]))
        index += 1
    return cpus


def thread_claims(process, online, minimum):
    """Collect narrow affinities of every live thread, including the leader.

    Whole-NUMA allowed masks retain the existing non-exclusive interpretation.
    Only diagnostic samples of TIDs are kept, so a large thread pool does not
    produce unbounded CONFLICT messages.
    """
    owners = {}
    observed = 0
    task = process / 'task'
    for thread in task.iterdir():
        if not thread.name.isdigit():
            continue
        try:
            status = (thread / 'status').read_text()
        except FileNotFoundError:
            if not thread.exists():
                continue  # this thread exited; keep other threads' claims
            raise
        match = re.search(r'^Cpus_allowed_list:\s*([^\n]*)$', status, re.M)
        if match is None:
            raise ValueError('missing CPU affinity for pid=%s tid=%s' % (process.name, thread.name))
        affinity = expand(match.group(1)) & online
        if not affinity:
            raise ValueError('empty online affinity for pid=%s tid=%s' % (process.name, thread.name))
        observed += 1
        if len(affinity) >= minimum:
            continue
        for cpu in affinity:
            count, tids = owners.setdefault(cpu, [0, []])
            owners[cpu][0] = count + 1
            if len(tids) < 4:
                tids.append(thread.name)
    if not observed and process_alive(process):
        raise ValueError('no readable threads for live pid=' + process.name)
    return owners


def process_claims(args, online, minimum):
    claims = {}
    self_matches = 0
    self_exe = os.path.realpath(args.self_exe) if args.self_exe else ''
    for process in Path('/proc').iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            raw = (process / 'cmdline').read_bytes()
            if not raw:
                if process_state(process)[1] > 1:
                    raise ValueError('cannot identify live worker threads for pid=' + process.name)
                continue  # kernel threads and exited user processes
            argv = [os.fsdecode(value) for value in raw.rstrip(b'\0').split(b'\0')]
            if self_exe:
                executable = re.sub(r' \(deleted\)$', '', os.readlink(process / 'exe'))
                if os.path.realpath(executable) == self_exe:
                    self_matches += 1
                    continue
            try:
                explicit = declared_cpus(argv)
            except ValueError as error:
                raise ValueError('invalid cpuset reservation for pid=' + process.name) from error
            affinities = thread_claims(process, online, minimum)
        except FileNotFoundError as error:
            if not process_alive(process):
                continue
            raise ValueError('CPU ownership incomplete for live pid=' + process.name) from error
        for cpu in explicit:
            claims.setdefault(cpu, set()).add('process ' + process.name + ' --cpuset')
        for cpu, (count, tids) in affinities.items():
            reason = 'thread-affinity pid=%s tids=%s' % (process.name, ','.join(sorted(tids, key=int)))
            if count > len(tids):
                reason += ' (+%s more)' % (count - len(tids))
            claims.setdefault(cpu, set()).add(reason)
    if self_matches > 1:
        raise ValueError('multiple instances share the self executable; use distinct instance binaries')
    return claims


def collect(args):
    online = expand(read('/sys/devices/system/cpu/online'))
    nodes = {}
    for path in Path('/sys/devices/system/node').glob('node[0-9]*/cpulist'):
        nodes[int(path.parent.name[4:])] = expand(read(path)) & online
    if not nodes:
        nodes = {0: online}
    siblings = {cpu: expand(read('/sys/devices/system/cpu/cpu%s/topology/thread_siblings_list' % cpu)) & online for cpu in online}
    claims = {}
    def claim(cpus, reason):
        for cpu in cpus:
            claims.setdefault(cpu, set()).add(reason)
    claim(expand(read('/sys/devices/system/cpu/isolated', '')), 'kernel-isolated')
    minimum = min(len(cpus) for cpus in nodes.values() if cpus)
    v2 = Path('/sys/fs/cgroup/cgroup.controllers').exists()
    base = Path('/sys/fs/cgroup') if v2 else Path('/sys/fs/cgroup/cpuset')
    effective = 'cpuset.cpus.effective' if v2 else 'cpuset.effective_cpus'
    for directory, _, files in os.walk(base):
        if effective not in files or Path(directory) == base:
            continue
        if v2 and not read(Path(directory) / 'cpuset.cpus', ''):
            continue
        cpus = expand(read(Path(directory) / effective))
        if len(cpus) < minimum:
            claim(cpus, 'cgroup ' + directory)
    state = Path('/var/lib/kubelet/cpu_manager_state')
    if state.exists():
        for containers in json.loads(read(state)).get('entries', {}).values():
            for cpus in containers.values():
                claim(expand(cpus), 'kubelet-exclusive')
    for path in ['/etc/kubernetes/kubelet-conf.yml', '/var/lib/kubelet/config.yaml']:
        match = re.search(r'^\s*reservedSystemCPUs:\s*[\'"]?([0-9,-]+)', read(path, ''), re.M)
        if match:
            claim(expand(match.group(1)), 'kubelet-reserved')
    for cpu, reasons in process_claims(args, online, minimum).items():
        claims.setdefault(cpu, set()).update(reasons)
    usage = {}
    if not args.no_busy:
        before = sample()
        time.sleep(1)
        after = sample()
        for cpu in online:
            if cpu not in before or cpu not in after:
                continue
            total = after[cpu][0] - before[cpu][0]
            idle = after[cpu][1] - before[cpu][1]
            usage[cpu] = 100 * (total - idle) / total if total > 0 else 0
            if usage[cpu] > args.busy:
                claim({cpu}, 'busy %.1f%%' % usage[cpu])
    claim(expand(args.exclude), 'assigned-this-run')
    return online, nodes, siblings, claims, usage


def choose(nodes, siblings, unavailable, shards, preferred):
    cores = {}
    for node, cpus in nodes.items():
        cores[node] = sorted(cpu for cpu in cpus if cpu == min(siblings[cpu]) and not (siblings[cpu] & unavailable))
    node = preferred
    if node not in cores or len(cores[node]) < shards:
        node = max(cores, key=lambda n: (len(cores[n]), -n))
    return node, cores[node][-shards:] if len(cores[node]) >= shards else [], cores[node]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-s', dest='shards', type=int, default=8)
    parser.add_argument('-n', dest='node', type=int)
    parser.add_argument('-c', dest='check', default='')
    parser.add_argument('-b', dest='busy', type=float, default=30)
    parser.add_argument('-x', dest='exclude', default='')
    parser.add_argument('-S', dest='legacy_self_name', default='')
    parser.add_argument('--self-exe', default='')
    parser.add_argument('--siblings', action='store_true')
    parser.add_argument('--no-busy', action='store_true')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    if args.shards < 1:
        raise ValueError('shards must be positive')
    online, nodes, siblings, claims, usage = collect(args)
    if args.check:
        cpus = expand(args.check)
        if not cpus:
            raise ValueError('empty cpuset')
        bad = False
        for cpu in sorted(cpus):
            if cpu not in online:
                print('CONFLICT %s NOT-ONLINE' % cpu)
                bad = True
                continue
            occupied = siblings[cpu] & claims.keys()
            if occupied:
                bad = True
                reasons = set().union(*(claims[sibling] for sibling in occupied))
                print('CONFLICT %s SMT_CPUS=%s %s' % (cpu, compact(occupied), '; '.join(sorted(reasons))))
        print('NODES=' + ' '.join(str(n) for n, members in sorted(nodes.items()) if members & cpus))
        if not bad:
            print('OK ' + args.check)
        return int(bad)
    node, selected, cores = choose(nodes, siblings, set(claims), args.shards, args.node)
    pick = set().union(*(siblings[c] for c in selected)) if args.siblings else set(selected)
    print('NODE=%s' % node)
    print('FREE=' + compact(nodes[node] - claims.keys()))
    print('CORES=' + compact(cores))
    print('SUGGEST=' + (compact(pick) or 'NONE'))
    forced, forced_node = set(), ''
    if not pick and args.force:
        excluded = expand(args.exclude)
        forced_node, candidates, all_cores = choose(nodes, siblings, excluded, args.shards, args.node)
        if candidates:
            selected = sorted(all_cores, key=lambda c: (max(usage.get(s, 0) for s in siblings[c]), -c))[:args.shards]
            forced = set().union(*(siblings[c] for c in selected)) if args.siblings else set(selected)
    print('FORCED=' + (compact(forced) or 'NONE'))
    print('FORCED_NODE=' + str(forced_node))
    print('FORCED_USAGE=' + ','.join('%s:%.1f' % (c, usage.get(c, 0)) for c in sorted(forced)))
    return 0 if pick else 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError) as error:
        print('CPU detection failed: ' + str(error), file=sys.stderr)
        sys.exit(3)

PYCPU
