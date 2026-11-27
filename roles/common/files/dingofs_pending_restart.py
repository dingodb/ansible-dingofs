#!/usr/bin/env python3
"""Coordinate deferred restarts for explicitly enrolled DingoFS clients."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

# The installed worker also runs read-only on hosts with no cached runtime.
if __name__ == '__main__':
    sys.dont_write_bytecode = True

import dingofs_client_runtime as runtime


DEFAULT_CONFIG = Path('/etc/dingofs/client-pending-restarts.json')
DEFAULT_STATE_DIR = Path('/var/lib/dingofs/client-pending-restarts')
CHECKER = Path(__file__).resolve().with_name('dingofs_mount_usage_check.sh')
USAGE_TIMEOUT = 45
RESTART_TIMEOUT = 240
VERIFY_TIMEOUT = 120
VERIFY_INTERVAL = 3
LAUNCH_TIME_MARGIN_NS = 2 * 10**9
SCHEMA = 1
IDENTITY_KEYS = ('unit', 'base_dir', 'mount_point', 'fsname')
PHASES = ('writing', 'ready', 'unverified', 'attempted', 'completed')
ATTEMPT_RESULTS = ('attempted', 'restart_failed', 'verification_failed')
PROBE_ERRORS = runtime.PROBE_ERRORS
TRANSACTION_ACTIONS = ('begin', 'finish', 'abort')
FAILURE_STATUSES = ('error', 'unknown', 'restart_failed', 'verification_failed')


class LockBusy(Exception):
    pass


class InvalidState(ValueError):
    pass


def identity(item):
    if not isinstance(item, dict) or set(item) != set(IDENTITY_KEYS):
        raise ValueError('instance requires unit, base_dir, mount_point and fsname')
    if any(not isinstance(value, str) or not value or '\0' in value for value in item.values()):
        raise ValueError('instance values must be nonempty strings')
    result = dict(item)
    unit = result['unit']
    if not re.fullmatch(r'[A-Za-z0-9:_.@\\-]+', unit) or unit == '.service':
        raise ValueError(f'invalid systemd service name {unit!r}')
    if not unit.endswith('.service'):
        result['unit'] = unit + '.service'
    for key in ('base_dir', 'mount_point'):
        value = result[key]
        if not value.startswith('/') or '..' in value.split('/'):
            raise ValueError(f'{key} must be an absolute path without parent traversal')
        result[key] = os.path.normpath('/' + value.lstrip('/'))
    return result


def enrolled(config, selected):
    try:
        with config.open(encoding='utf-8') as source:
            entries = json.load(source)
    except FileNotFoundError:
        return []
    if not isinstance(entries, list):
        raise ValueError(f'{config} must contain a JSON array of instances')
    instances = [identity(entry) for entry in entries]
    for key in ('mount_point', 'unit', 'base_dir'):
        if len({item[key] for item in instances}) != len(instances):
            raise ValueError(f'{config} contains a duplicate instance {key}')
    if selected is None:
        return instances
    for item in instances:
        if item['mount_point'] == selected['mount_point']:
            if item != selected:
                raise ValueError(f'enrolled identity differs for {selected["mount_point"]}')
            return [item]
    return []


def state_path(state_dir, instance):
    key = hashlib.sha256(os.fsencode(instance['mount_point'])).hexdigest()
    return state_dir / (key + '.json')


def marker_path(instance):
    return Path(instance['base_dir']) / '.restart-required'


def file_signature(path):
    try:
        return list(runtime.signature(path))
    except FileNotFoundError:
        return None


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ensure_directory(path):
    if not path.is_dir():
        ensure_directory(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)
        sync_directory(path.parent)


@contextmanager
def mount_lock(path, *, blocking=False):
    # Keep the lock inode stable even when there is no pending obligation.
    fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(fd, flags)
        except BlockingIOError as exc:
            raise LockBusy from exc
        yield
    finally:
        os.close(fd)


def read_state(path, instance):
    try:
        with path.open(encoding='utf-8') as source:
            state = json.load(source)
    except FileNotFoundError:
        return None
    except ValueError as exc:
        raise InvalidState(f'state is not valid JSON: {exc}') from exc
    if (not isinstance(state, dict) or state.get('schema') != SCHEMA
            or state.get('identity') != instance or state.get('phase') not in PHASES):
        if isinstance(state, dict) and (state.get('phase') in ('writing', 'attempted')
                                        or state.get('attempt') is not None):
            raise ValueError('existing transaction or attempt has an incompatible state or identity')
        raise InvalidState('state schema, identity or phase is invalid')
    attempt = state.get('attempt')
    if ((state['phase'] == 'attempted' or attempt is not None)
            and (not isinstance(attempt, dict) or not isinstance(attempt.get('marker'), list)
                 or attempt.get('status') not in ATTEMPT_RESULTS or not isinstance(attempt.get('reason'), str))):
        raise ValueError('restart attempt is incomplete')
    if state['phase'] == 'writing' and not state.get('transaction'):
        raise ValueError('writing state lacks a transaction token')
    if state['phase'] == 'ready' and not isinstance(state.get('revision'), dict):
        raise InvalidState('ready state lacks a revision')
    return state


def publish(path, instance, phase, **fields):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.stem + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as target:
            json.dump(dict(schema=SCHEMA, identity=instance, phase=phase, **fields), target,
                      sort_keys=True)
            target.write('\n')
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def configuration(instance, properties):
    base = Path(instance['base_dir'])
    launcher = base / 'dingofs-mount.sh'
    binary = base / 'bin/dingo-client'
    start = re.match(r'^\{\s*path=(.*?)\s*;', properties.get('ExecStart', ''))
    if (properties.get('LoadState') != 'loaded' or properties.get('NeedDaemonReload') != 'no'
            or not start or start[1] not in (str(launcher), str(binary))):
        raise ValueError('expected a loaded client service with its managed ExecStart and no pending reload')
    paths = [Path(properties['FragmentPath'])]
    paths.extend(Path(item) for item in shlex.split(properties['DropInPaths']))
    if start[1] == str(launcher):
        paths.append(launcher)
    if any(not path.is_absolute() for path in paths):
        raise ValueError('unit configuration paths must be absolute')
    files = [[str(path), list(runtime.signature(path))] for path in paths]
    # These managed inputs can affect a remount even though they are not used
    # to establish which version of the running client originally launched.
    for path in (base / 'dingofs-umount.sh', base / 'bin/dingo'):
        files.append([str(path), file_signature(path)])
    return dict(exec_start=start[1], files=files)


def revision(instance, marker, properties):
    return dict(marker=marker,
                binary=list(runtime.signature(Path(instance['base_dir']) / 'bin/dingo-client')),
                configuration=configuration(instance, properties))


def previous_upgrade_config(instance, marker, state, invalid_state):
    if invalid_state:
        return None
    try:
        if marker is None and (state is None or state['phase'] != 'unverified'):
            return configuration(instance, runtime.unit_properties(instance['unit']))
        if state and state['phase'] == 'ready':
            current = revision(instance, marker, runtime.unit_properties(instance['unit']))
            if current == state['revision']:
                return current['configuration']
    except PROBE_ERRORS:
        # A full deploy can establish a known configuration from a partial one.
        pass
    return None


def transact(action, instance, state_dir, token, full_deploy):
    path = state_path(state_dir, instance)
    if action == 'begin':
        ensure_directory(path.parent)
    else:
        # A completed token can never become a writer again. In particular,
        # Ansible's cleanup must not contend with a timer consuming its result.
        prior = read_state(path, instance)
        if not prior or prior['phase'] != 'writing' or prior['transaction'] != token:
            if action == 'abort':
                return dict(status='no_transaction', transaction=None)
            raise ValueError('token does not match the writing transaction')
    with mount_lock(path, blocking=action != 'begin'):
        invalid_state = False
        try:
            state = read_state(path, instance)
        except InvalidState:
            if action != 'begin':
                raise
            state = None
            invalid_state = True
        writing = state is not None and state['phase'] == 'writing'
        attempt = state.get('attempt') if state else None
        if action == 'begin':
            if writing:
                raise ValueError('a writing transaction already exists; finish it or abort its token')
            marker = file_signature(marker_path(instance))
            upgrade_config = previous_upgrade_config(instance, marker, state, invalid_state)
            token = uuid.uuid4().hex
            publish(path, instance, 'writing', transaction=token, upgrade_config=upgrade_config,
                    attempt=attempt)
            return dict(status='writing', transaction=token)
        if not writing or state['transaction'] != token:
            if action == 'abort':
                return dict(status='no_transaction', transaction=None)
            raise ValueError('token does not match the writing transaction')
        if action == 'abort':
            publish(path, instance, 'unverified', attempt=attempt,
                    reason='writing transaction aborted; a full deploy can attest pending files')
            return dict(status='aborted', transaction=token)
        marker = file_signature(marker_path(instance))
        if marker is None:
            if full_deploy or state.get('upgrade_config') is not None:
                path.unlink()
                sync_directory(path.parent)
            else:
                # A no-op upgrade cannot attest files from an aborted deploy
                # even when that deploy had yet to create its marker.
                publish(path, instance, 'unverified', attempt=attempt,
                        reason='a successful full deploy is required to attest the configuration')
            return dict(status='no_marker', transaction=token)
        if attempt and attempt['marker'] == marker:
            publish(path, instance, 'attempted', attempt=attempt)
            return dict(status=attempt['status'], transaction=token, reason=attempt['reason'])
        if not full_deploy and not state.get('upgrade_config'):
            reason = 'a successful full deploy is required to attest a legacy or partial update'
        else:
            current = revision(instance, marker, runtime.unit_properties(instance['unit']))
            if full_deploy or current['configuration'] == state['upgrade_config']:
                publish(path, instance, 'ready', revision=current)
                return dict(status='ready', transaction=token)
            reason = 'configuration changed during the binary upgrade; run a full deploy'
        publish(path, instance, 'unverified', reason=reason)
        return dict(status='unverified', transaction=token, reason=reason)


def run_usage(argv, timeout):
    process = subprocess.Popen([str(arg) for arg in argv], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, encoding='utf-8', errors='replace',
                               env=dict(os.environ, LC_ALL='C', SYSTEMD_PAGER='cat'),
                               start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    except BaseException:
        # Killing only the shell leaves its Python and runtime probes behind.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        process.stdout.close()
        process.stderr.close()


def usage(instance):
    start = time.monotonic_ns()
    result = dict(status='unknown')
    try:
        probe = run_usage(['bash', CHECKER, instance['mount_point'], '--dry-run', '--strict',
                           '--service', instance['unit']], timeout=USAGE_TIMEOUT)
        result['status'] = {0: 'idle', 1: 'busy'}.get(probe.returncode, 'unknown')
        if probe.returncode not in (0, 1):
            result['reason'] = command_failure(probe, 'strict usage check')
    except PROBE_ERRORS as exc:
        result['reason'] = str(exc)
    result['usage_ms'] = (time.monotonic_ns() - start) // 1_000_000
    return result


def command_failure(result, action):
    detail = (result.stderr.strip() or result.stdout.strip()).splitlines()[-1:]
    return f'{action} exited with status {result.returncode}' + (f': {detail[0]}' if detail else '')


def observe(instance, state_dir):
    result = dict(instance)
    try:
        marker = file_signature(marker_path(instance))
        result['marker'] = 'present' if marker is not None else 'absent'
        if marker is not None:
            result['pending_age_seconds'] = max(0, (time.time_ns() - max(marker[-2:])) // 10**9)
    except PROBE_ERRORS as exc:
        result.update(marker='unknown', marker_reason=str(exc))
    try:
        state = read_state(state_path(state_dir, instance), instance)
        result['state'] = state['phase'] if state else ('legacy' if result['marker'] == 'present' else 'absent')
        if state and state['phase'] == 'writing':
            result['transaction'] = state['transaction']
        if state and state.get('attempt'):
            result['last_attempt'] = state['attempt']['status']
        if state and state.get('result'):
            result['last_result'] = state['result']
    except PROBE_ERRORS as exc:
        result.update(state='invalid', state_reason=str(exc))
    result.update(usage(instance))
    return result


def live_revision(instance):
    properties = runtime.unit_properties(instance['unit'])
    if (properties.get('ActiveState') != 'active' or properties.get('SubState') != 'running'
            or int(properties.get('MainPID', '0')) <= 0):
        raise ValueError('expected an active client service with a running main process')
    mount = runtime.mount_record(instance['mount_point'], instance['fsname'])
    if mount[0] != 'mounted' or mount[1] is None:
        raise ValueError(f'cannot verify the intended FUSE mount (mount state: {mount[0]})')
    marker = file_signature(marker_path(instance))
    return properties, mount, revision(instance, marker, properties)


def inspect(instance):
    return runtime.inspect_client(**instance)


def clearable(probe):
    return probe['reconciliation'] == 'clearable'


def consume(instance, path, status, expected_marker):
    marker = marker_path(instance)
    if file_signature(marker) != expected_marker:
        raise ValueError('restart marker changed during verification')
    marker.unlink()
    sync_directory(marker.parent)
    publish(path, instance, 'completed', result=status)


def settle_launch_times(expected):
    # Runtime's launch-time proof uses second-resolution boot time and ticks.
    # Leave room for that lower bound before checking usage for the last time.
    signatures = [expected['marker']]
    signatures.extend(signature for _, signature in expected['configuration']['files'] if signature)
    age = time.time_ns() - max(max(signature[-2:]) for signature in signatures)
    if age < 0:
        raise ValueError('pending marker or configuration has a timestamp in the future')
    if age < LAUNCH_TIME_MARGIN_NS:
        time.sleep((LAUNCH_TIME_MARGIN_NS - age) / 10**9)


def verify_restart(instance, expected):
    deadline = time.monotonic() + VERIFY_TIMEOUT
    while True:
        try:
            before = live_revision(instance)
        except PROBE_ERRORS as exc:
            evidence = str(exc)
        else:
            if before[2] != expected:
                raise ValueError('pending revision or file signatures changed after restart')
            probe = inspect(instance)
            if clearable(probe) and live_revision(instance) == before:
                return
            evidence = str(probe)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('could not prove the active service, intended FUSE mount and exact pending '
                             f'binary/config after restart (runtime evidence: {evidence})')
        time.sleep(min(VERIFY_INTERVAL, remaining))


def apply_locked(instance, path, outcome, may_restart):
    marker = file_signature(marker_path(instance))
    if marker is None:
        return dict(status='no_marker')
    try:
        state = read_state(path, instance)
    except ValueError as exc:
        return dict(status='unverified', reason=str(exc))
    if state and state['phase'] == 'writing':
        return dict(status='writing', reason='Ansible writing transaction is open')
    attempt = state.get('attempt') if state else None
    if attempt and attempt['marker'] == marker:
        return dict(status=attempt['status'], reason=attempt['reason'])
    if state is None or state['phase'] != 'ready':
        return dict(status='unverified', reason='marker has no attested ready revision; run a full deploy')
    try:
        before = live_revision(instance)
        if before[2] != state['revision'] or before[2]['marker'] != marker:
            raise ValueError('pending revision or file signatures changed; run a full deploy')
        probe = inspect(instance)
        already_applied = clearable(probe)
        if not already_applied:
            if (probe['active_state'] != 'active' or probe['mount_state'] != 'mounted' or not probe['pid']
                    or probe['binary_match'] == 'unknown' or probe['config_match'] == 'unknown'):
                raise ValueError('cannot verify the running client process for this mount')
            if not may_restart:
                return dict(status='restart_limit', reason='this invocation already attempted one restart')
        for check_number in range(1 if already_applied else 2):
            if check_number == 1:
                settle_launch_times(before[2])
            check = usage(instance)
            outcome['usage_ms'] = outcome.get('usage_ms', 0) + check.pop('usage_ms')
            if check['status'] != 'idle':
                return check
            if live_revision(instance) != before:
                raise ValueError('service, mount or revision changed during strict usage checks')
        if already_applied and not clearable(inspect(instance)):
            raise ValueError('service activation changed during reconciliation')
    except PROBE_ERRORS as exc:
        return dict(status='unverifiable', reason=str(exc))
    if already_applied:
        consume(instance, path, 'already_applied', marker)
        return dict(status='already_applied')

    attempt = dict(marker=marker, status='attempted', reason='restart outcome was not recorded; '
                   'inspect the service before recovering the pending update')
    publish(path, instance, 'attempted', attempt=attempt)
    outcome['restart_attempted'] = True
    try:
        started = runtime.run(['systemctl', 'restart', '--', instance['unit']], timeout=RESTART_TIMEOUT)
        if started.returncode:
            raise ValueError(command_failure(started, 'systemctl restart'))
    except PROBE_ERRORS as exc:
        attempt.update(status='restart_failed', reason=str(exc))
    else:
        try:
            verify_restart(instance, state['revision'])
        except PROBE_ERRORS as exc:
            attempt.update(status='verification_failed', reason=str(exc))
        else:
            consume(instance, path, 'restarted', marker)
            return dict(status='restarted')
    publish(path, instance, 'attempted', attempt=attempt)
    return dict(status=attempt['status'], reason=attempt['reason'])


def apply(instance, state_dir, may_restart):
    outcome = dict(instance, restart_attempted=False)
    try:
        if file_signature(marker_path(instance)) is None:
            outcome.update(status='no_marker')
        elif not state_dir.is_dir():
            outcome.update(status='unverified', reason='marker has no attested ready revision; run a full deploy')
        else:
            path = state_path(state_dir, instance)
            with mount_lock(path):
                outcome.update(apply_locked(instance, path, outcome, may_restart))
    except LockBusy:
        outcome.update(status='locked', reason='another process holds the mount lock')
    except PROBE_ERRORS as exc:
        outcome.update(status='error', reason=str(exc))
    return outcome


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=TRANSACTION_ACTIONS + ('observe', 'apply'))
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument('--instance', help='JSON object containing the full enrolled identity')
    parser.add_argument('--transaction')
    parser.add_argument('--full-deploy', action='store_true')
    args = parser.parse_args(argv)
    transaction_action = args.action in TRANSACTION_ACTIONS
    try:
        selected = identity(json.loads(args.instance)) if args.instance is not None else None
        if transaction_action and selected is None:
            raise ValueError(f'{args.action} requires --instance')
        if args.action in ('finish', 'abort') and not args.transaction:
            raise ValueError(f'{args.action} requires --transaction')
        instances = enrolled(args.config, selected)
        if transaction_action:
            result = (transact(args.action, instances[0], args.state_dir, args.transaction, args.full_deploy)
                      if instances else dict(status='not_enrolled', transaction=None))
        else:
            result = []
            may_restart = True
            for instance in instances:
                item = (observe(instance, args.state_dir) if args.action == 'observe'
                        else apply(instance, args.state_dir, may_restart))
                result.append(item)
                may_restart = may_restart and not item.get('restart_attempted')
    except LockBusy:
        result = dict(status='error', reason='another process holds the mount lock', transaction=None)
    except PROBE_ERRORS as exc:
        error = dict(status='error', reason=str(exc))
        result = dict(error, transaction=None) if transaction_action else [error]
    print(json.dumps(result, sort_keys=True))
    return int(any(item['status'] in FAILURE_STATUSES
                   for item in ([result] if transaction_action else result)))


if __name__ == '__main__':
    raise SystemExit(main())
