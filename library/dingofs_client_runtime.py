#!/usr/bin/python
"""Inspect one managed client and reconcile an already applied restart."""
import json
import os
import re
import shlex
import stat
import subprocess
from pathlib import Path


DOCUMENTATION = r'''
module: dingofs_client_runtime
short_description: Inspect the running DingoFS client for one managed mount
options:
  unit:
    type: str
    required: true
  base_dir:
    type: path
    required: true
  mount_point:
    type: path
    required: true
  fsname:
    type: str
    required: true
  report_version:
    type: bool
    default: false
  reconcile:
    type: bool
    default: false
    description: Clear a restart marker when its activation is verified.
  usage_script:
    type: path
    description: Optional installed mount usage checker for status reports.
  usage_strict:
    type: bool
    default: false
supports_check_mode: true
'''

UNIT_PROPERTIES = 'LoadState,ActiveState,SubState,MainPID,ExecStart,FragmentPath,DropInPaths,NeedDaemonReload,InvocationID'
VERSION_LINE = re.compile(r'^(?:v[0-9]+(?:\.[0-9]+)+[^\s,]*|[0-9a-f]{7,40})(?:$|[, ])')
PROBE_ERRORS = (OSError, ValueError, IndexError, KeyError, subprocess.TimeoutExpired)


def run(argv, timeout=10):
    return subprocess.run([str(arg) for arg in argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          encoding='utf-8', errors='replace', timeout=timeout,
                          env=dict(os.environ, LC_ALL='C', SYSTEMD_PAGER='cat'))


def unit_properties(unit):
    result = run(['systemctl', 'show', '--all', '--property=' + UNIT_PROPERTIES, '--', unit])
    if result.returncode:
        raise ValueError('systemctl show failed')
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def mount_record(mount_point, fsname):
    result = run(['findmnt', '--kernel', '--nocanonicalize', '--json', '--output',
                  'ID,FSTYPE,SOURCE', '--mountpoint', mount_point])
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return 'missing', None
    if result.returncode:
        return 'unknown', None
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict) or not isinstance(payload.get('filesystems'), list):
        return 'unknown', None
    rows = payload['filesystems']
    if len(rows) == 1 and isinstance(rows[0], dict):
        row = rows[0]
        fstype = row.get('fstype')
        if (isinstance(fstype, str) and (fstype == 'fuse' or fstype.startswith('fuse.'))
                and row.get('source') == 'DingoFS:' + fsname):
            return 'mounted', row.get('id')
    return 'different', None


def signature(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('expected a regular local file')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def client_process(proc, main_pid, binary):
    """Follow the service main process through shell and numactl launchers."""
    pending = [main_pid]
    seen = set()
    clients = []
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        path = proc / str(pid)
        try:
            exe = os.readlink(path / 'exe')
            if exe in (str(binary), str(binary) + ' (deleted)'):
                clients.append(path)
                if len(clients) > 1:
                    return None
                continue
            pending.extend(int(value) for value in (path / 'task' / str(pid) / 'children').read_text().split())
        except FileNotFoundError:
            if path.exists():
                raise
    return clients[0] if clients else None


def start_ticks(process):
    # The comm field is parenthesized and may itself contain ')' or spaces.
    fields = (process / 'stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] in ('Z', 'X'):
        raise ValueError('client process exited')
    return int(fields[19])


def launch_time_ns(proc, ticks):
    # /proc/stat exposes boot time at second resolution. Use that lower bound
    # and the process start tick to avoid approving an ambiguous equal timestamp.
    boot = next((int(line.split()[1]) for line in (proc / 'stat').read_text().splitlines()
                 if line.startswith('btime ')), None)
    if boot is None:
        raise ValueError('missing procfs boot time')
    return boot * 10**9 + ticks * 10**9 // os.sysconf('SC_CLK_TCK')


def compare_binary(exe, binary):
    if os.path.samefile(exe, binary):
        return 'current'
    return {0: 'current', 1: 'stale'}.get(run(['cmp', '-s', exe, binary]).returncode, 'unknown')


def running_version(exe):
    try:
        result = run([exe, '--version'])
    except PROBE_ERRORS:
        return 'unknown'
    # Some released clients print --version and exit 1.
    for line in (result.stdout + '\n' + result.stderr).splitlines():
        if VERSION_LINE.match(line):
            return line[:160]
    return 'unknown'


def inspect_client(unit, base_dir, mount_point, fsname, report_version=False,
                   reconcile=False, proc=Path('/proc')):
    state = dict(changed=False, reconciled=False, active_state='unknown', mount_state='unknown',
                 pid=None, running_version='unknown', binary_match='unknown', config_match='unknown',
                 restart_marker='unknown', reconciliation='unknown')
    base_dir = Path(base_dir)
    binary = base_dir / 'bin/dingo-client'
    launcher = base_dir / 'dingofs-mount.sh'
    marker = base_dir / '.restart-required'
    marker_signature = None
    try:
        marker_signature = signature(marker)
        state.update(restart_marker='present', reconciliation='pending')
    except FileNotFoundError:
        state.update(restart_marker='absent', reconciliation='none')
    except PROBE_ERRORS:
        pass

    try:
        properties = unit_properties(unit)
        state['active_state'] = properties.get('ActiveState', 'unknown')
    except PROBE_ERRORS:
        return state
    try:
        mount = mount_record(mount_point, fsname)
        state['mount_state'] = mount[0]
    except PROBE_ERRORS:
        mount = ('unknown', None)
    if state['active_state'] != 'active':
        return state
    try:
        main_pid = int(properties['MainPID'])
        if main_pid <= 0:
            return state
        main = proc / str(main_pid)
        main_ticks = start_ticks(main)
        client = client_process(proc, main_pid, binary)
        if client is None:
            return state
        ticks = start_ticks(client)
        exe = client / 'exe'
        state['pid'] = int(client.name)
        state['binary_match'] = compare_binary(exe, binary)
        if report_version:
            state['running_version'] = running_version(exe)

        if properties.get('NeedDaemonReload') == 'yes':
            state['config_match'] = 'stale'
            return state
        if (properties.get('LoadState') != 'loaded' or properties.get('SubState') != 'running'
                or properties.get('NeedDaemonReload') != 'no'):
            return state
        exec_start = re.match(r'^\{\s*path=(.*?)\s*;', properties.get('ExecStart', ''))
        if not exec_start or exec_start[1] not in (str(launcher), str(binary)):
            return state
        fragment = Path(properties['FragmentPath'])
        drop_ins = [Path(path) for path in shlex.split(properties['DropInPaths'])]
        config_files = [fragment] + drop_ins
        if exec_start[1] == str(launcher):
            config_files.append(launcher)
        if any(not path.is_absolute() for path in config_files):
            return state
        argv = (client / 'cmdline').read_bytes().decode('utf-8', 'replace').split('\0')
        if not any(url.startswith(('mds://', 'local://')) and url.rsplit('/', 1)[-1] == fsname
                   and path == mount_point for url, path in zip(argv, argv[1:])):
            return state
        launch = launch_time_ns(proc, main_ticks)
        config_signatures = [signature(path) for path in config_files]
        state['config_match'] = ('current' if all(max(info[-2:]) < launch for info in config_signatures)
                                 else 'stale')
        if not (marker_signature is not None and max(marker_signature[-2:]) < launch
                and mount[0] == 'mounted' and mount[1] is not None
                and state['binary_match'] == 'current' and state['config_match'] == 'current'):
            return state

        # Revalidate the service, files and mount before consuming an obligation.
        binary_signature = signature(binary)
        if (unit_properties(unit) != properties or start_ticks(main) != main_ticks
                or client_process(proc, main_pid, binary) != client or start_ticks(client) != ticks
                or mount_record(mount_point, fsname) != mount
                or compare_binary(exe, binary) != 'current'
                or signature(binary) != binary_signature
                or [signature(path) for path in config_files] != config_signatures
                or signature(marker) != marker_signature):
            state['reconciliation'] = 'unknown'
            return state
        state['reconciliation'] = 'clearable'
        if reconcile:
            marker.unlink()
            state.update(changed=True, reconciled=True, restart_marker='absent', reconciliation='cleared')
    except PROBE_ERRORS:
        if state['restart_marker'] == 'present':
            state['reconciliation'] = 'unknown'
    return state


def main():
    from ansible.module_utils.basic import AnsibleModule
    module = AnsibleModule(
        argument_spec=dict(unit=dict(type='str', required=True), base_dir=dict(type='path', required=True),
                           mount_point=dict(type='path', required=True), fsname=dict(type='str', required=True),
                           report_version=dict(type='bool', default=False), reconcile=dict(type='bool', default=False),
                           usage_script=dict(type='path'), usage_strict=dict(type='bool', default=False)),
        supports_check_mode=True,
    )
    params = module.params
    unit = params['unit']
    if not unit.endswith('.service'):
        unit += '.service'
    state = inspect_client(unit, params['base_dir'], params['mount_point'], params['fsname'],
                           report_version=params['report_version'],
                           reconcile=params['reconcile'] and not module.check_mode)
    state['mount_usage'] = 'not_checked'
    if params['usage_script']:
        argv = ['bash', params['usage_script'], params['mount_point'], '--dry-run']
        if params['usage_strict']:
            argv.append('--strict')
        try:
            state['mount_usage'] = {0: 'IDLE', 1: 'BUSY'}.get(run(argv, timeout=45).returncode, 'UNKNOWN')
        except PROBE_ERRORS:
            state['mount_usage'] = 'UNKNOWN'
    module.exit_json(**state)


if __name__ == '__main__':
    main()
