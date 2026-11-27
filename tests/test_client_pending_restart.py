"""Deferred restart coordination with isolated state and simulated Linux probes."""
import io
import json
import signal
import subprocess
import sys
import tempfile
import unittest
from collections import deque
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from library import dingofs_client_runtime as runtime

# On a client host the Ansible role installs these modules beside one another.
with patch.dict(sys.modules, {'dingofs_client_runtime': runtime}):
    from roles.common.files import dingofs_pending_restart as worker


class PendingRestartTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / 'enrollment.json'
        self.state_dir = self.root / 'state'
        self.instances = []
        self.properties = {}
        self.mounts = {}
        self.applied = set()
        self.usage_results = deque()
        self.usage_calls = []
        self.restarts = []
        self.restart_effect = self.activate
        self.client = self.add_client('alpha')
        for name, effect in (
            ('unit_properties', lambda unit: dict(self.properties[unit])),
            ('mount_record', lambda mount_point, fsname: self.mounts[mount_point]),
            ('inspect_client', self.inspect_client),
            ('run', self.run_command),
        ):
            mock = patch.object(runtime, name, side_effect=effect)
            mock.start()
            self.addCleanup(mock.stop)
        for mock in (patch.object(worker, 'run_usage', side_effect=self.run_usage),
                     patch.object(worker, 'LAUNCH_TIME_MARGIN_NS', 0)):
            mock.start()
            self.addCleanup(mock.stop)

    def add_client(self, fsname):
        base = self.root / fsname
        (base / 'bin').mkdir(parents=True)
        binary = base / 'bin/dingo-client'
        binary.write_text('new client')
        (base / 'dingofs-mount.sh').write_text('mount launcher')
        (base / 'dingofs-umount.sh').write_text('unmount launcher')
        unit = 'dingofs-' + fsname + '.service'
        fragment = self.root / unit
        fragment.write_text('unit configuration')
        client = dict(unit=unit, base_dir=str(base), mount_point=str(self.root / 'mounts' / fsname),
                      fsname=fsname)
        self.properties[unit] = dict(LoadState='loaded', ActiveState='active', SubState='running',
                                     MainPID='100', ExecStart=f'{{ path={base}/dingofs-mount.sh ; }}',
                                     FragmentPath=str(fragment), DropInPaths='', NeedDaemonReload='no',
                                     InvocationID='original')
        self.mounts[client['mount_point']] = ('mounted', 45)
        self.instances.append(client)
        self.config.write_text(json.dumps(self.instances))
        return client

    def inspect_client(self, **instance):
        current = instance['unit'] in self.applied
        return dict(active_state=self.properties[instance['unit']]['ActiveState'],
                    mount_state=self.mounts[instance['mount_point']][0], pid=205,
                    binary_match='current' if current else 'stale', config_match='current',
                    reconciliation='clearable' if current else 'pending')

    def activate(self, argv):
        unit = argv[-1]
        self.properties[unit].update(MainPID='300', InvocationID='restarted')
        client = next(instance for instance in self.instances if instance['unit'] == unit)
        self.mounts[client['mount_point']] = ('mounted', 67)
        self.applied.add(unit)
        return subprocess.CompletedProcess(argv, 0, '', '')

    def run_usage(self, argv, timeout):
        self.usage_calls.append((argv, timeout))
        result = self.usage_results.popleft() if self.usage_results else 0
        result = result() if callable(result) else result
        if isinstance(result, Exception):
            raise result
        return subprocess.CompletedProcess(argv, result, '', 'strict usage result')

    def run_command(self, argv, timeout):
        self.assertEqual(argv[:3], ['systemctl', 'restart', '--'])
        self.restarts.append(argv[-1])
        return self.restart_effect(argv)

    def cli(self, action, client=None, token=None, full_deploy=False):
        argv = [action, '--config', str(self.config), '--state-dir', str(self.state_dir)]
        if client is not None:
            argv.extend(['--instance', json.dumps(client)])
        if token is not None:
            argv.extend(['--transaction', token])
        if full_deploy:
            argv.append('--full-deploy')
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = worker.main(argv)
        return code, json.loads(stdout.getvalue())

    def begin(self, client):
        code, result = self.cli('begin', client)
        self.assertEqual(code, 0, result)
        return result['transaction']

    def ready(self, client):
        self.marker(client).write_text('restart required\n')
        token = self.begin(client)
        code, result = self.cli('finish', client, token, full_deploy=True)
        self.assertEqual((code, result['status']), (0, 'ready'), result)
        return token

    def apply(self, client=None):
        code, results = self.cli('apply', client or self.client)
        return code, results[0]['status']

    def state(self, client):
        return worker.state_path(self.state_dir, client)

    def marker(self, client):
        return Path(client['base_dir']) / '.restart-required'

    def test_observe_checks_usage_without_writing_even_for_a_legacy_marker(self):
        other = self.add_client('beta')
        self.marker(self.client).write_text('pending')
        self.usage_results.extend((1, 0))
        code, results = self.cli('observe')
        self.assertEqual(code, 0)
        self.assertEqual([item['status'] for item in results], ['busy', 'idle'])
        self.assertEqual(results[0]['state'], 'legacy')
        self.assertTrue(self.marker(self.client).exists())
        self.assertFalse(self.state_dir.exists())
        for client, (argv, timeout), result in zip((self.client, other), self.usage_calls, results):
            self.assertEqual(argv[2:], [client['mount_point'], '--dry-run', '--strict', '--service', client['unit']])
            self.assertGreater(timeout, 0)
            self.assertGreaterEqual(result['usage_ms'], 0)
        _, selected = self.cli('observe', other)
        self.assertEqual([item['mount_point'] for item in selected], [other['mount_point']])

    def test_absent_or_unmatched_enrollment_does_not_create_state(self):
        self.config.unlink()
        code, result = self.cli('begin', self.client)
        self.assertEqual(code, 0)
        self.assertIsNone(result['transaction'])
        self.config.write_text('[]')
        code, result = self.cli('begin', self.client)
        self.assertEqual(code, 0)
        self.assertIsNone(result['transaction'])
        self.config.write_text(json.dumps(self.instances))
        code, result = self.cli('begin', dict(self.client, fsname='other'))
        self.assertNotEqual(code, 0)
        self.assertIsNone(result['transaction'])
        self.assertFalse(self.state_dir.exists())

    def test_no_marker_does_not_create_lock_or_run_probes(self):
        self.assertEqual(self.apply(), (0, 'no_marker'))
        self.assertFalse(self.state_dir.exists())
        token = self.begin(self.client)
        _, result = self.cli('finish', self.client, token, full_deploy=True)
        self.assertEqual(result['status'], 'no_marker')
        self.assertFalse(self.state(self.client).exists())
        self.assertFalse(self.usage_calls or self.restarts)

    def test_writing_token_excludes_apply_and_competing_writers(self):
        self.marker(self.client).write_text('pending')
        token = self.begin(dict(self.client, unit=self.client['unit'][:-len('.service')]))
        self.assertEqual(self.apply(), (0, 'writing'))
        self.assertNotEqual(self.cli('begin', self.client)[0], 0)
        self.assertNotEqual(self.cli('finish', self.client, 'wrong', full_deploy=True)[0], 0)
        self.assertEqual(self.cli('abort', self.client, 'wrong')[0], 0)
        self.assertEqual(self.apply(), (0, 'writing'))
        self.assertEqual(self.cli('finish', self.client, token, full_deploy=True)[1]['status'], 'ready')
        prior = self.state(self.client).read_bytes()
        with worker.mount_lock(self.state(self.client)):
            code, result = self.cli('abort', self.client, token)
        self.assertEqual((code, result['status']), (0, 'no_transaction'))
        self.assertEqual(self.state(self.client).read_bytes(), prior)
        self.assertFalse(self.usage_calls or self.restarts)

    def test_enrollment_change_cannot_supersede_an_open_writer(self):
        self.marker(self.client).write_text('pending')
        token = self.begin(self.client)
        updated = dict(self.client, fsname='renamed')
        self.config.write_text(json.dumps([updated]))
        self.assertNotEqual(self.cli('begin', updated)[0], 0)
        self.assertEqual(self.apply(updated), (0, 'unverified'))
        self.config.write_text(json.dumps(self.instances))
        self.assertEqual(self.apply(), (0, 'writing'))
        self.assertEqual(self.cli('abort', self.client, token)[1]['status'], 'aborted')

    def test_abort_leaves_pending_marker_but_requires_full_deploy_to_attest(self):
        token = self.begin(self.client)
        self.marker(self.client).write_text('partial deploy')
        self.assertEqual(self.cli('abort', self.client, token)[0], 0)
        self.assertEqual(self.cli('abort', self.client, token)[0], 0)
        self.assertEqual(self.apply(), (0, 'unverified'))
        later = self.begin(self.client)
        self.assertNotEqual(later, token)
        _, result = self.cli('finish', self.client, later)
        self.assertEqual(result['status'], 'unverified')
        self.assertTrue(self.marker(self.client).exists())
        self.assertFalse(self.usage_calls or self.restarts)
        self.ready(self.client)

    def test_noop_upgrade_does_not_attest_an_abort_before_marker_creation(self):
        token = self.begin(self.client)
        self.assertEqual(self.cli('abort', self.client, token)[0], 0)
        token = self.begin(self.client)
        self.assertEqual(self.cli('finish', self.client, token)[1]['status'], 'no_marker')
        token = self.begin(self.client)
        self.marker(self.client).write_text('a later binary update')
        self.assertEqual(self.cli('finish', self.client, token)[1]['status'], 'unverified')
        self.assertEqual(self.apply(), (0, 'unverified'))

    def test_legacy_or_incomplete_state_cannot_be_used_by_timer_or_upgrade(self):
        self.marker(self.client).write_text('legacy marker')
        self.assertEqual(self.apply(), (0, 'unverified'))
        token = self.begin(self.client)
        self.assertEqual(self.cli('finish', self.client, token)[1]['status'], 'unverified')
        self.state(self.client).write_text(json.dumps(dict(schema=worker.SCHEMA, phase='ready',
                                                           identity=self.client)))
        self.assertEqual(self.apply(), (0, 'unverified'))
        self.assertTrue(self.marker(self.client).exists())
        self.assertFalse(self.usage_calls or self.restarts)

    def test_upgrade_can_attest_fresh_marker_and_update_a_verified_revision(self):
        binary = Path(self.client['base_dir']) / 'bin/dingo-client'
        token = self.begin(self.client)
        self.marker(self.client).write_text('fresh marker')
        binary.write_text('new binary')
        self.assertEqual(self.cli('finish', self.client, token)[1]['status'], 'ready')
        token = self.begin(self.client)
        binary.write_text('different binary without a replacement marker')
        self.assertEqual(self.cli('finish', self.client, token)[1]['status'], 'ready')
        token = self.begin(self.client)
        (Path(self.client['base_dir']) / 'dingofs-mount.sh').write_text('unattested configuration')
        self.assertEqual(self.cli('finish', self.client, token)[1]['status'], 'unverified')
        self.assertTrue(self.marker(self.client).exists())

    def test_busy_unknown_and_timeout_preserve_ready_state_in_both_checks(self):
        self.ready(self.client)
        state = self.state(self.client).read_bytes()
        for checks, expected in (([1], (0, 'busy')), ([0, 1], (0, 'busy')),
                                 ([2], (1, 'unknown')),
                                 ([0, subprocess.TimeoutExpired('checker', 1)], (1, 'unknown'))):
            with self.subTest(checks=checks):
                self.usage_results.extend(checks)
                self.assertEqual(self.apply(), expected)
                self.assertTrue(self.marker(self.client).exists())
                self.assertEqual(self.state(self.client).read_bytes(), state)
        self.assertFalse(self.restarts)

    def test_recent_marker_settles_before_the_fresh_usage_check(self):
        self.ready(self.client)
        newest = max(worker.file_signature(self.marker(self.client))[-2:])
        with patch.object(worker, 'LAUNCH_TIME_MARGIN_NS', 2 * 10**9), \
                patch.object(worker.time, 'time_ns', return_value=newest), \
                patch.object(worker.time, 'sleep') as wait:
            def turn_busy():
                wait.assert_called_once_with(2)
                return 1

            self.usage_results.extend((0, turn_busy))
            self.assertEqual(self.apply(), (0, 'busy'))
        self.assertTrue(self.marker(self.client).exists())
        self.assertFalse(self.restarts)

    def test_inactive_wrong_mount_and_changed_files_are_unverifiable(self):
        self.ready(self.client)
        properties = self.properties[self.client['unit']]
        properties['ActiveState'] = 'inactive'
        self.assertEqual(self.apply(), (0, 'unverifiable'))
        properties['ActiveState'] = 'active'
        mount_point = self.client['mount_point']
        self.mounts[mount_point] = ('different', None)
        self.assertEqual(self.apply(), (0, 'unverifiable'))
        self.mounts[mount_point] = ('mounted', 45)
        (Path(self.client['base_dir']) / 'dingofs-umount.sh').write_text('changed unmount script')
        self.assertEqual(self.apply(), (0, 'unverifiable'))
        self.assertTrue(self.marker(self.client).exists())
        self.assertFalse(self.usage_calls or self.restarts)

    def test_mount_change_during_last_usage_check_blocks_restart(self):
        self.ready(self.client)

        def replace_mount():
            self.mounts[self.client['mount_point']] = ('mounted', 99)
            return 0

        self.usage_results.extend((0, replace_mount))
        self.assertEqual(self.apply(), (0, 'unverifiable'))
        self.assertTrue(self.marker(self.client).exists())
        self.assertFalse(self.restarts)

    def test_already_applied_after_new_invocation_consumes_without_restart(self):
        self.ready(self.client)
        self.activate(['systemctl', 'restart', '--', self.client['unit']])
        self.assertEqual(self.apply(), (0, 'already_applied'))
        self.assertFalse(self.marker(self.client).exists())
        self.assertFalse(self.restarts)
        self.assertEqual(self.apply(), (0, 'no_marker'))

    def test_already_applied_keeps_the_marker_when_usage_is_busy_or_unknown(self):
        self.ready(self.client)
        self.activate(['systemctl', 'restart', '--', self.client['unit']])
        self.usage_results.extend((1, 2))
        self.assertEqual(self.apply(), (0, 'busy'))
        self.assertEqual(self.apply(), (1, 'unknown'))
        self.assertTrue(self.marker(self.client).exists())
        self.assertFalse(self.restarts)

    def test_locks_exclude_writers_during_usage_restart_and_verification(self):
        self.ready(self.client)
        with worker.mount_lock(self.state(self.client)):
            self.assertEqual(self.apply(), (0, 'locked'))
            self.assertNotEqual(self.cli('begin', self.client)[0], 0)

        def assert_locked():
            self.assertNotEqual(self.cli('begin', self.client)[0], 0)
            self.assertEqual(self.apply(), (0, 'locked'))
            return 0

        def activate_while_locked(argv):
            assert_locked()
            return self.activate(argv)

        def inspect_while_locked(**instance):
            assert_locked()
            return self.inspect_client(**instance)

        self.usage_results.extend((assert_locked, assert_locked))
        self.restart_effect = activate_while_locked
        with patch.object(runtime, 'inspect_client', side_effect=inspect_while_locked):
            self.assertEqual(self.apply(), (0, 'restarted'))
        self.assertEqual(self.restarts, [self.client['unit']])
        self.assertFalse(self.marker(self.client).exists())
        self.assertEqual(self.apply(), (0, 'no_marker'))

    def test_marker_replaced_during_restart_is_not_consumed(self):
        self.ready(self.client)

        def activate_and_replace_marker(argv):
            result = self.activate(argv)
            replacement = self.marker(self.client).with_name('replacement')
            replacement.write_text('a different obligation')
            replacement.replace(self.marker(self.client))
            return result

        self.restart_effect = activate_and_replace_marker
        self.assertEqual(self.apply(), (1, 'verification_failed'))
        self.assertEqual(self.marker(self.client).read_text(), 'a different obligation')
        self.assertEqual(self.apply(), (0, 'unverified'))
        self.assertEqual(self.restarts, [self.client['unit']])

    def test_interrupted_attempt_survives_new_process_and_boot(self):
        self.ready(self.client)

        def worker_killed(argv):
            raise KeyboardInterrupt

        self.restart_effect = worker_killed
        with self.assertRaises(KeyboardInterrupt):
            self.apply()
        self.properties[self.client['unit']].update(MainPID='425', InvocationID='after-boot')
        self.mounts[self.client['mount_point']] = ('mounted', 95)
        self.restart_effect = self.activate
        self.assertEqual(self.apply(), (0, 'attempted'))
        token = self.begin(self.client)
        self.assertEqual(self.cli('abort', self.client, token)[0], 0)
        token = self.begin(self.client)
        _, result = self.cli('finish', self.client, token, full_deploy=True)
        self.assertEqual(result['status'], 'attempted')
        self.assertEqual(self.apply(), (0, 'attempted'))
        self.assertTrue(self.marker(self.client).exists())
        self.assertEqual(self.restarts, [self.client['unit']])

    def test_failed_restart_uses_only_attempt_for_invocation(self):
        other = self.add_client('beta')
        self.ready(self.client)
        self.ready(other)
        self.restart_effect = lambda argv: subprocess.CompletedProcess(argv, 1, '', 'unit failed')
        code, results = self.cli('apply')
        self.assertNotEqual(code, 0)
        self.assertEqual([item['status'] for item in results], ['restart_failed', 'restart_limit'])
        self.assertEqual(self.restarts, [self.client['unit']])
        prior = self.state(self.client).read_bytes()
        self.assertEqual(self.cli('abort', self.client, 'old-token')[0], 0)
        self.assertEqual(self.state(self.client).read_bytes(), prior)
        self.assertTrue(self.marker(self.client).exists())
        self.assertTrue(self.marker(other).exists())

    def test_unverified_restart_preserves_marker_across_apply_invocations(self):
        self.ready(self.client)
        self.restart_effect = lambda argv: subprocess.CompletedProcess(argv, 0, '', '')
        with patch.object(worker, 'VERIFY_TIMEOUT', 0):
            self.assertEqual(self.apply(), (1, 'verification_failed'))
        self.assertEqual(self.apply(), (1, 'verification_failed'))
        self.assertTrue(self.marker(self.client).exists())
        self.assertEqual(self.restarts, [self.client['unit']])


class UsageProcessTests(unittest.TestCase):
    def test_timeout_terminates_checker_group(self):
        with patch.object(worker.subprocess, 'Popen') as popen, patch.object(worker.os, 'killpg') as kill:
            process = popen.return_value
            process.pid = 434
            process.communicate.side_effect = subprocess.TimeoutExpired('checker', 1)
            with self.assertRaises(subprocess.TimeoutExpired):
                worker.run_usage(['bash', 'checker'], timeout=1)
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
            kill.assert_called_once_with(434, signal.SIGKILL)


if __name__ == '__main__':
    unittest.main()
