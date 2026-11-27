#!/usr/bin/env python3
"""Inspect or replace disconnected DingoFS bind mounts in running containers."""
import argparse
from collections import namedtuple
from contextlib import ExitStack
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import platform
import posixpath
import re
import stat
import subprocess
import sys


PROC = Path('/proc')
Mount = namedtuple('Mount', 'id parent device root path options propagation fstype source super_options')
Plan = namedtuple('Plan', 'pid start namespace host source old destination')
ERRORS = (OSError, ValueError, RuntimeError, TypeError, KeyError, IndexError, subprocess.SubprocessError)
DINGOFS_TYPES = ('fuse', 'fuse.dingofs')
SECURITY_FLAGS = {'nosuid': 0x2, 'nodev': 0x4, 'noexec': 0x8, 'nosymfollow': 0x200000}


class RepairChangedError(RuntimeError):
    """The mount table changed before a repair failed or could be verified."""


def unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), value)


def mountinfo(path):
    rows = []
    for line in path.read_text(errors='surrogateescape').splitlines():
        fields = line.split()
        separator = fields.index('-')
        if separator < 6 or len(fields) != separator + 4:
            raise ValueError('malformed kernel mount table')
        rows.append(Mount(int(fields[0]), int(fields[1]), fields[2], unescape(fields[3]),
                          unescape(fields[4]), frozenset(fields[5].split(',')), tuple(fields[6:separator]),
                          fields[separator + 1], unescape(fields[separator + 2]),
                          frozenset(fields[separator + 3].split(','))))
    if not rows:
        raise ValueError('empty kernel mount table')
    return rows


def at_or_below(path, parent):
    return path == parent or path.startswith(parent.rstrip('/') + '/')


def absolute_path(value):
    return (isinstance(value, str) and value.startswith('/') and not value.startswith('//')
            and posixpath.normpath(value) == value and '\0' not in value)


def readonly(row):
    return 'ro' in row.options or 'ro' in row.super_options


def current_mount(path, fsname):
    rows = mountinfo(PROC / 'self/mountinfo')
    exact = [row for row in rows if row.path == path]
    if (len(exact) != 1 or exact[0].fstype not in DINGOFS_TYPES
            or not exact[0].source.startswith('DingoFS:')
            or (fsname and exact[0].source != 'DingoFS:' + fsname)):
        raise ValueError('host path must contain one mount of the expected DingoFS filesystem')
    return exact[0], rows


def run_json(argv, timeout):
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('{} failed (exit {})'.format(' '.join(argv[:2]), result.returncode))
    return json.loads(result.stdout)


def probe_path(path, read_directory=False, source_path=False):
    # Only this short-lived subprocess accesses FUSE metadata or directory data.
    probe = '''
import json, os, stat, sys
try:
    path = sys.argv[1]
    info = os.stat(path)
    out = {'device': '{}:{}'.format(os.major(info.st_dev), os.minor(info.st_dev)),
           'directory': stat.S_ISDIR(info.st_mode)}
    if sys.argv[3] == 'source':
        out['canonical'] = os.path.realpath(path) == path
    if sys.argv[2] == 'read':
        with os.scandir(path) as contents:
            next(contents, None)
        out['readonly'] = bool(os.statvfs(path).f_flag & os.ST_RDONLY)
except OSError as error:
    out = {'errno': error.errno}
print(json.dumps(out))
'''
    return run_json([sys.executable, '-c', probe, str(path), 'read' if read_directory else 'stat',
                     'source' if source_path else 'target'], 3)


def require_readable(path, device, source_path=False):
    result = probe_path(path, read_directory=True, source_path=source_path)
    if result.get('device') != device or not result.get('directory'):
        raise ValueError('cannot read the expected filesystem at {}'.format(path))
    if source_path and not result.get('canonical'):
        raise ValueError('source path resolves through a symlink: {}'.format(path))
    return result


def container_targets(pod_filter, all_namespaces):
    pods = run_json(['crictl', 'pods', '-o', 'json'], 10)
    containers = run_json(['crictl', 'ps', '--state', 'Running', '-o', 'json'], 10)
    for payload, key in ((pods, 'items'), (containers, 'containers')):
        if not isinstance(payload, dict) or (payload and not isinstance(payload.get(key), list)):
            raise ValueError('unrecognized CRI list response')
    names = {pod['id']: (pod['metadata']['namespace'], pod['metadata']['name']) for pod in pods.get('items', [])}
    for container in containers.get('containers', []):
        namespace, name = names[container['podSandboxId']]
        pod = namespace + '/' + name
        if pod_filter and pod != pod_filter:
            continue
        if not all_namespaces and not namespace.startswith('cci'):
            continue
        yield container['id'], pod, container['metadata']['name']


def container_mounts(container_id):
    detail = run_json(['crictl', 'inspect', '-o', 'json', container_id], 10)
    if detail['status']['id'] != container_id or detail['status']['state'] != 'CONTAINER_RUNNING':
        raise ValueError('container identity changed during inspection')
    mounts = detail['status'].get('mounts') or detail['info']['runtimeSpec']['mounts']
    pairs = [(mount.get('hostPath') or mount.get('source'),
              mount.get('containerPath') or mount.get('destination')) for mount in mounts]
    return int(detail['info']['pid']), pairs


def process_start(process):
    fields = (process / 'stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] in ('Z', 'X'):
        raise ValueError('container process exited')
    return fields[19]


def read_mount_id(data):
    for line in data.splitlines():
        if line.startswith('mnt_id:'):
            return int(line.split()[1])
    raise ValueError('fdinfo has no mount ID')


def fd_mount_id(fd, fdinfo_dir=None):
    if fdinfo_dir is None:
        return read_mount_id((PROC / 'self/fdinfo' / str(fd)).read_text())
    with os.fdopen(os.open(str(fd), os.O_RDONLY, dir_fd=fdinfo_dir)) as stream:
        return read_mount_id(stream.read())


def handle_users(pid, namespace, device, mounts):
    ids = {mount.id for mount in mounts if mount.device == device}
    dev_numbers = tuple(int(value) for value in device.split(':'))
    blockers = []
    for process in PROC.iterdir():
        if not process.name.isdigit():
            continue
        try:
            if os.readlink(process / 'ns/mnt') != namespace:
                continue
        except FileNotFoundError:
            continue
        # Verify task-level cwd and descriptors as threads can unshare them.
        try:
            for task in (process / 'task').iterdir():
                try:
                    if os.readlink(task / 'ns/mnt') != namespace:
                        continue
                    for name in ('cwd', 'root'):
                        fd = os.open(task / name, os.O_PATH | os.O_CLOEXEC)
                        try:
                            if fd_mount_id(fd) in ids:
                                blockers.append({'pid': int(process.name), 'tid': int(task.name), 'kind': name})
                        finally:
                            os.close(fd)
                    for info in (task / 'fdinfo').iterdir():
                        try:
                            if read_mount_id(info.read_text()) in ids:
                                blockers.append({'pid': int(process.name), 'tid': int(task.name), 'kind': 'fd'})
                                break
                        except FileNotFoundError:
                            continue  # Descriptor was closed.
                except FileNotFoundError:
                    if (task / 'ns/mnt').is_symlink():
                        raise
            for line in (process / 'maps').read_text().splitlines():
                device_field = line.split()[3]
                if tuple(int(value, 16) for value in device_field.split(':')) == dev_numbers:
                    blockers.append({'pid': int(process.name), 'kind': 'mmap'})
                    break
        except FileNotFoundError:
            if (process / 'ns/mnt').is_symlink():
                raise
    # A disappearing init cannot provide a complete scan of the target namespace.
    if os.readlink(PROC / str(pid) / 'ns/mnt') != namespace:
        raise ValueError('container mount namespace changed')
    return blockers


def expected_root(host, source):
    suffix = posixpath.relpath(source, host.path)
    return posixpath.normpath(posixpath.join(host.root, suffix))


def check_layout(host, host_rows, mounts, old, source):
    parent = next((row for row in mounts if row.id == old.parent), None)
    if old.propagation or parent is None or parent.propagation:
        return 'target and its parent must have private propagation'
    if any(row.path != old.path and at_or_below(row.path, old.path) for row in mounts):
        return 'target has child mounts'
    if any(row.id != host.id and (at_or_below(row.path, source)
                                  or (at_or_below(source, row.path) and at_or_below(row.path, host.path)))
           for row in host_rows):
        return 'source has an overlapping or nested mount'
    if readonly(host) and not readonly(old):
        return 'host source is read-only and the container expects a writable mount'
    return ''


def inspect_reference(host, host_rows, pid, source, destination):
    if pid <= 0 or not absolute_path(destination) or destination == '/':
        raise ValueError('invalid container PID or mount destination')
    process = PROC / str(pid)
    start = process_start(process)
    namespace = os.readlink(process / 'ns/mnt')
    mounts = mountinfo(process / 'mountinfo')
    exact = [row for row in mounts if row.path == destination]
    if len(exact) != 1:
        return dict(state='UNKNOWN', result='skipped', reason='container mount missing or stacked'), None
    old = exact[0]
    fields = dict(old_device=old.device, host_device=host.device, readonly=readonly(old))
    if (old.fstype not in DINGOFS_TYPES or old.source != host.source
            or old.root != expected_root(host, source)):
        return dict(fields, state='UNKNOWN', result='skipped', reason='filesystem identity or mount root differs'), None
    if old.device == host.device:
        return dict(fields, state='LIVE', result='current'), None
    path = process / 'root' / destination.lstrip('/')
    if probe_path(path).get('errno') != errno.ENOTCONN:
        return dict(fields, state='UNKNOWN', result='skipped', reason='disconnect unconfirmed'), None
    result = dict(fields, state='STALE', result='skipped')
    reason = check_layout(host, host_rows, mounts, old, source)
    if not reason and namespace == os.readlink(PROC / 'self/ns/mnt'):
        reason = 'container uses the host mount namespace'
    if not reason and os.readlink(process / 'ns/user') != os.readlink(PROC / 'self/ns/user'):
        reason = 'container and host use different user namespaces'
    if reason:
        return dict(result, reason=reason), None
    users = handle_users(pid, namespace, old.device, mounts)
    if users:
        return dict(result, reason='processes retain references to the disconnected mount', users=users), None
    require_readable(source, host.device, source_path=True)
    if (process_start(process) != start or os.readlink(process / 'ns/mnt') != namespace
            or mountinfo(process / 'mountinfo') != mounts):
        raise ValueError('container or mounts changed during inspection')
    return dict(fields, state='STALE', result='repairable'), Plan(pid, start, namespace, host, source, old, destination)


class MountAttr(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ('attr_set', 'attr_clr', 'propagation', 'userns_fd')]


def mount_attr(host, old):
    # Also retain the source's security restrictions when the old bind was looser.
    flags = int(readonly(old))
    for option, bit in SECURITY_FLAGS.items():
        if option in host.options or option in old.options:
            flags |= bit
    return MountAttr(flags, 0, 1 << 18, 0)  # MS_PRIVATE


class MountAPI:
    """Linux mount API syscall numbers shared by x86_64 and aarch64."""
    def __init__(self):
        if platform.machine() not in ('x86_64', 'aarch64'):
            raise ValueError('mount repair supports Linux x86_64 and aarch64')
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.syscall.restype = ctypes.c_long
        self.libc.setns.argtypes = (ctypes.c_int, ctypes.c_int)
        self.libc.umount2.argtypes = (ctypes.c_char_p, ctypes.c_int)

    @staticmethod
    def check(result, operation):
        if result < 0:
            error = ctypes.get_errno()
            raise OSError(error, '{}: {}'.format(operation, os.strerror(error)))
        return result

    def clone(self, fd):
        flags = 0x1000 | 1 | os.O_CLOEXEC  # AT_EMPTY_PATH | OPEN_TREE_CLONE
        return self.check(self.libc.syscall(428, fd, b'', flags), 'open_tree')

    def attributes(self, tree, attrs):
        self.check(self.libc.syscall(442, tree, b'', 0x1000, ctypes.byref(attrs), ctypes.sizeof(attrs)),
                   'mount_setattr')

    def enter(self, namespace):
        self.check(self.libc.setns(namespace, 0x00020000), 'setns')  # CLONE_NEWNS

    def detach(self, target):
        self.check(self.libc.umount2(os.fsencode(target), 2), 'umount2')  # MNT_DETACH

    def attach(self, tree, destination):
        self.check(self.libc.syscall(429, tree, b'', destination, b'', 0x04 | 0x40),
                   'move_mount')  # MOVE_MOUNT_F_EMPTY_PATH | MOVE_MOUNT_T_EMPTY_PATH


def replace_mount(plan):
    """Run only in a child process, keeping the controller in the host namespace."""
    api = MountAPI()
    process = PROC / str(plan.pid)
    flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as stack:
        def hold(fd):
            stack.callback(os.close, fd)
            return fd

        namespace = hold(os.open(process / 'ns/mnt', os.O_RDONLY | os.O_CLOEXEC))
        root = hold(os.open(process / 'root', os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC))
        source = hold(os.open(plan.source, flags))
        fdinfo = hold(os.open(PROC / 'self/fdinfo', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
        if (fd_mount_id(source, fdinfo) != plan.host.id
                or os.readlink(PROC / 'self/fd' / str(source)) != plan.source
                or process_start(process) != plan.start
                or os.readlink(PROC / 'self/fd' / str(namespace)) != plan.namespace):
            raise ValueError('host mount or container changed before repair')
        replacement = hold(api.clone(source))
        api.attributes(replacement, mount_attr(plan.host, plan.old))
        host_rows = mountinfo(PROC / 'self/mountinfo')
        target_rows = mountinfo(process / 'mountinfo')
        if ([mount for mount in host_rows if mount.path == plan.host.path] != [plan.host]
                or [mount for mount in target_rows if mount.path == plan.destination] != [plan.old]
                or check_layout(plan.host, host_rows, target_rows, plan.old, plan.source)
                or process_start(process) != plan.start):
            raise ValueError('mount layout changed before entering container')
        api.enter(namespace)
        os.fchdir(root)
        os.chroot('.')
        os.chdir('/')
        old = hold(os.open(plan.destination, flags))
        parent = hold(os.open(posixpath.dirname(plan.destination), flags))
        if fd_mount_id(old, fdinfo) != plan.old.id or fd_mount_id(parent, fdinfo) != plan.old.parent:
            raise ValueError('container mount or parent changed before repair')
        rollback = hold(api.clone(old))
        api.detach(plan.destination)
        try:
            underlying = hold(os.open(posixpath.basename(plan.destination), flags, dir_fd=parent))
            if fd_mount_id(underlying, fdinfo) != plan.old.parent:
                raise ValueError('another mount appeared at the destination')
            api.attach(replacement, underlying)
        except ERRORS as error:
            try:
                underlying = hold(os.open(posixpath.basename(plan.destination), flags, dir_fd=parent))
                if fd_mount_id(underlying, fdinfo) != plan.old.parent:
                    raise ValueError('destination already has a replacement mount')
                api.attach(rollback, underlying)
            except ERRORS as restore_error:
                raise RepairChangedError('replacement failed and previous mount could not be restored: {}'.format(restore_error)) from error
            raise RepairChangedError('replacement failed; previous mount restored: {}'.format(error)) from error


def apply_repair(container_id, plan):
    with os.fdopen(os.open('/run/dingofs-stale-mount-repair.lock',
                          os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        host, rows = current_mount(plan.host.path, plan.host.source.split(':', 1)[1])
        if host != plan.host:
            raise ValueError('host mount changed before repair')
        pid, pairs = container_mounts(container_id)
        if pid != plan.pid or (plan.source, plan.destination) not in pairs:
            raise ValueError('CRI container or hostPath changed before repair')
        report, current = inspect_reference(host, rows, plan.pid, plan.source, plan.destination)
        if current != plan:
            raise ValueError(report.get('reason', 'container mount changed before repair'))
        read_fd, write_fd = os.pipe()
        try:
            child = os.fork()
        except OSError:
            os.close(read_fd)
            os.close(write_fd)
            raise
        if child == 0:
            os.close(read_fd)
            try:
                replace_mount(plan)
            except ERRORS as error:
                os.write(write_fd, str(error).encode('utf-8', 'replace')[:2048])
                os._exit(2 if isinstance(error, RepairChangedError) else 1)
            os._exit(0)
        os.close(write_fd)
        with os.fdopen(read_fd) as stream:
            error = stream.read()
        _, status = os.waitpid(child, 0)
        if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 2:
            raise RepairChangedError(error or 'mount repair failed after detaching')
        if status:
            raise RuntimeError(error or 'mount repair process failed')
        try:
            return verify_replacement(plan)
        except ERRORS as error:
            raise RepairChangedError('replacement attached; verification failed: {}'.format(error)) from error


def verify_replacement(plan):
    process = PROC / str(plan.pid)
    if process_start(process) != plan.start or os.readlink(process / 'ns/mnt') != plan.namespace:
        raise ValueError('container identity changed')
    host, _ = current_mount(plan.host.path, plan.host.source.split(':', 1)[1])
    if host != plan.host:
        raise ValueError('host mount changed')
    exact = [row for row in mountinfo(process / 'mountinfo') if row.path == plan.destination]
    if (len(exact) != 1 or exact[0].device != host.device or exact[0].source != host.source
            or exact[0].fstype not in DINGOFS_TYPES or exact[0].parent != plan.old.parent
            or exact[0].root != plan.old.root or exact[0].propagation
            or readonly(exact[0]) != readonly(plan.old)):
        raise ValueError('mount identity or access mode differs')
    probe = require_readable(process / 'root' / plan.destination.lstrip('/'), host.device)
    if probe['readonly'] != readonly(plan.old):
        raise ValueError('read-only flag did not verify')
    return exact[0].id


def scan(args, report):
    host, rows = current_mount(args.mount_point, args.fsname)
    require_readable(host.path, host.device)
    report['host_device'] = host.device
    selected_pod = False
    repairs = []
    for container_id, pod, name in container_targets(args.pod, args.all_namespaces):
        selected_pod = True
        identity = dict(pod=pod, container=name)
        try:
            pid, pairs = container_mounts(container_id)
        except ERRORS as error:
            report['findings'].append(dict(identity, state='UNKNOWN', result='skipped', reason=str(error)))
            continue
        for source, destination in pairs:
            if not absolute_path(source) or not at_or_below(source, host.path):
                continue
            entry = dict(identity, source=source, destination=destination, pid=pid)
            try:
                result, plan = inspect_reference(host, rows, pid, source, destination)
                entry.update(result)
            except ERRORS as error:
                entry.update(state='UNKNOWN', result='skipped', reason=str(error))
            else:
                if plan is not None:
                    repairs.append((container_id, plan, entry))
            report['findings'].append(entry)
    if args.pod and not selected_pod:
        raise ValueError('selected Pod has no running container in the requested namespace scope')
    if args.apply and not report['findings']:
        raise ValueError('selected Pod has no matching direct hostPath mount')
    if not args.apply:
        return
    if any(entry['state'] == 'UNKNOWN' for entry in report['findings']):
        raise ValueError('selected Pod has an unresolved mount reference')
    for container_id, plan, entry in repairs:
        try:
            entry.update(new_mount_id=apply_repair(container_id, plan), result='repaired')
        except ERRORS as error:
            entry.update(result='failed', reason=str(error))
            report['changed'] = report['changed'] or isinstance(error, RepairChangedError)
        else:
            report['changed'] = True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mount-point', required=True, help='absolute host DingoFS mount point')
    parser.add_argument('--fsname', help='expected filesystem name')
    parser.add_argument('--pod', help='namespace/name of a single Pod')
    parser.add_argument('--all-namespaces', action='store_true', help='include namespaces outside cci*')
    parser.add_argument('--apply', action='store_true', help='replace verified disconnected mounts of --pod')
    parser.add_argument('--json', action='store_true', help='print a JSON report')
    args = parser.parse_args(argv)
    report = dict(mode='apply' if args.apply else 'dry-run', mount_point=args.mount_point,
                  changed=False, findings=[])
    try:
        if sys.platform != 'linux' or os.geteuid() != 0:
            raise ValueError('run as root on the Linux container host')
        if not absolute_path(args.mount_point) or args.mount_point == '/':
            raise ValueError('--mount-point must be a normalized absolute path below /')
        if args.pod and not re.fullmatch(r'[a-z0-9][a-z0-9.-]*/[a-z0-9][a-z0-9.-]*', args.pod):
            raise ValueError('--pod must use namespace/name')
        if args.apply and not args.pod:
            raise ValueError('--apply requires --pod namespace/name')
        scan(args, report)
    except ERRORS as error:
        report['error'] = str(error)
    if args.json:
        print(json.dumps(report))
    else:
        print('{} {}'.format(report['mode'], report['mount_point']))
        for entry in report['findings']:
            print('{} {} {}'.format(entry['state'], entry['result'], json.dumps(entry, sort_keys=True)))
        if not report['findings'] and 'error' not in report:
            print('No matching direct hostPath mounts.')
        if 'error' in report:
            print(report['error'], file=sys.stderr)
    if 'error' in report or any(entry['state'] == 'UNKNOWN' for entry in report['findings']):
        return 2
    return int(args.apply and any(entry['result'] in ('failed', 'skipped') for entry in report['findings']))


if __name__ == '__main__':
    sys.exit(main())
