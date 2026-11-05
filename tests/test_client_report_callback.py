"""Controller report aggregation without reading host variables or task output."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "dingofs_client_report", REPO / "callback_plugins/dingofs_client_report.py")
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)


class Display:
    def __init__(self):
        self.messages = []
        self.warnings = []

    def display(self, message):
        self.messages.append(message)

    def warning(self, message):
        self.warnings.append(message)


class Host:
    def __init__(self, name):
        self.name = name

    def get_name(self):
        return self.name


def result(host="host-a", data=None, no_log=False):
    return types.SimpleNamespace(_host=Host(host), _result=data or {},
                                 _task=types.SimpleNamespace(no_log=no_log))


def event(host="host-a", fs="fs-a", status="skipped_busy_before_change", stage="precheck", **values):
    payload = dict(schema_version=1, operation="deploy", fsname=fs, mount_point="/mnt/" + fs,
                   service="dingofs-mount-" + fs, stage=stage, status=status,
                   reason="Mount is in use.", changed_on_disk=False, pending_restart=False)
    payload.update(values)
    return result(host, {"ansible_stats": {"per_host": True, "aggregate": False,
                                           "data": {"dingofs_client_event": payload}}})


class Stats:
    def __init__(self, entries=None):
        self.entries = entries or {}
        self.processed = dict.fromkeys(self.entries, 1)

    def summarize(self, host):
        return self.entries[host]


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="client-report-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "reports"
        self.env = patch.dict(os.environ, {"DINGO_CLIENT_REPORT_DIR": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.callback = self.make_callback()

    def make_callback(self):
        callback = report.CallbackModule()
        callback._display = Display()
        return callback

    def read(self, callback=None):
        return json.loads((callback or self.callback)._json_path.read_text())

    def start_client(self):
        self.callback.v2_playbook_on_start(types.SimpleNamespace(_file_name="/repo/playbooks/client_04_deploy.yml"))

    def test_multiple_hosts_filesystems_and_serial_batches_accumulate(self):
        self.start_client()
        for host in ("host-a", "host-b", "host-c"):
            for fs in ("alpha", "beta"):
                self.callback.v2_runner_on_ok(event(host, fs))
        self.callback.v2_playbook_on_stats(Stats({host: {"ok": 2} for host in ("host-a", "host-b", "host-c")}))
        payload = self.read()
        self.assertEqual(len(payload["records"]), 6)
        self.assertEqual(len(payload["skipped"]), 6)
        self.assertTrue(payload["complete"])
        self.assertEqual(len(self.callback._tsv_path.read_text().splitlines()), 7)
        self.assertIn("6 skipped instance(s) on 3 host(s)", self.callback._display.messages[0])

    def test_empty_skipped_list_writes_fresh_report_and_header(self):
        self.start_client()
        self.callback.v2_runner_on_ok(event(status="completed", stage="complete", reason="No restart needed."))
        self.callback.v2_playbook_on_stats(Stats({"host-a": {"ok": 1}}))
        self.assertEqual(self.read()["skipped"], [])
        self.assertEqual(len(self.callback._tsv_path.read_text().splitlines()), 1)

    def test_phase_changes_keep_previous_terminal_record_until_completion(self):
        self.callback.v2_runner_on_ok(event(status="restart_deferred_busy", stage="restart_check"))
        self.callback.v2_runner_on_ok(event(status="started", stage="restart"))
        self.assertEqual(self.read()["skipped"][0]["status"], "restart_deferred_busy")
        self.callback.v2_runner_on_ok(event(status="completed", stage="complete"))
        self.assertEqual(self.read()["skipped"], [])

    def test_failure_at_final_check_keeps_busy_and_actual_task_failure(self):
        self.callback.v2_runner_on_ok(event(status="started", stage="restart_check", changed_on_disk=True,
                                           pending_restart=True, operation="upgrade"))
        self.callback.v2_runner_on_failed(result(data={"rc": 1, "stdout": "SECRET", "stderr": "SECRET"}))
        payload = self.read()
        self.assertEqual(payload["skipped"][0]["status"], "restart_deferred_busy")
        self.assertTrue(payload["skipped"][0]["task_failed"])
        self.assertEqual(len(payload["errors"]), 1)
        self.assertNotIn("SECRET", self.callback._json_path.read_text())

    def test_unknown_rc_and_missing_rc_are_not_busy(self):
        for rc in (2, None):
            callback = self.make_callback()
            callback.v2_runner_on_ok(event(status="started", stage="restart_check"))
            callback.v2_runner_on_failed(result(data={"rc": rc}))
            payload = self.read(callback)
            self.assertEqual(payload["skipped"], [])
            self.assertEqual(payload["errors"][0]["status"], "check_failed")

    def test_restart_and_verification_errors_keep_instance_identity(self):
        for stage, status in (("restart", "restart_failed"), ("verify", "verification_failed")):
            callback = self.make_callback()
            callback.v2_runner_on_ok(event(fs="x", status="started", stage=stage))
            callback.v2_runner_on_failed(result(data={"rc": 1}))
            row = self.read(callback)["errors"][0]
            self.assertEqual((row["fsname"], row["status"]), ("x", status))

    def test_gather_facts_failure_is_reported_without_inventing_filesystem(self):
        self.start_client()
        self.callback.v2_runner_on_unreachable(result("host-unreachable", {"msg": "secret credentials"}))
        self.callback.v2_playbook_on_stats(Stats({"host-unreachable": {"unreachable": 1}}))
        payload = self.read()
        self.assertEqual(payload["skipped"], [])
        self.assertIsNone(payload["host_errors"][0]["fsname"])
        self.assertEqual(payload["unreachable_hosts"], ["host-unreachable"])
        self.assertNotIn("secret credentials", self.callback._json_path.read_text())

    def test_unrelated_failure_after_completed_instance_is_not_attached_to_it(self):
        self.callback.v2_runner_on_ok(event(status="completed", stage="complete"))
        self.callback.v2_runner_on_failed(result(data={"msg": "later label failed"}))
        payload = self.read()
        self.assertEqual(payload["records"][0]["status"], "completed")
        self.assertIsNone(payload["host_errors"][0]["fsname"])

    def test_cached_error_survives_relevance_detection_in_later_play(self):
        self.callback.v2_runner_on_failed(result("early-host", {"msg": "SECRET"}))
        self.assertFalse(self.root.exists())
        role = types.SimpleNamespace(get_name=lambda: "dingo_client_upgrade")
        play = types.SimpleNamespace(get_roles=lambda: [role], get_path=lambda: "/custom.yml:1")
        self.callback.v2_playbook_on_play_start(play)
        self.callback.v2_playbook_on_stats(Stats({"early-host": {"failures": 1}}))
        self.assertEqual(self.read()["failed_hosts"], ["early-host"])
        self.assertEqual(self.read()["host_errors"][0]["host"], "early-host")

    def test_check_mode_and_dry_run_are_not_skipped_busy(self):
        self.callback._check_mode = True
        self.callback.v2_runner_on_ok(event(status="dry_run", stage="complete", pending_restart=None))
        self.callback.v2_playbook_on_stats(Stats({"host-a": {"skipped": 1}}))
        self.assertTrue(self.read()["check_mode"])
        self.assertEqual(self.read()["skipped"], [])

    def test_unknown_disk_change_during_configuration_is_preserved(self):
        self.callback.v2_runner_on_ok(event(status="started", stage="configure",
                                           changed_on_disk=None, pending_restart=None))
        self.callback.v2_runner_on_failed(result(data={"rc": 1}))
        row = self.read()["errors"][0]
        self.assertIsNone(row["changed_on_disk"])
        self.assertIsNone(row["pending_restart"])

    def test_event_carries_check_mode_when_callback_started_without_cli_context(self):
        self.callback.v2_runner_on_ok(event(status="dry_run", check_mode=True))
        self.assertTrue(self.read()["check_mode"])
        self.assertTrue(self.read()["records"][0]["check_mode"])

    def test_check_mode_skip_events_are_predictions_not_actual_skips(self):
        for status in report.SKIPPED:
            callback = self.make_callback()
            callback.v2_runner_on_ok(event(status=status, check_mode=True))
            payload = self.read(callback)
            self.assertEqual(payload["skipped"], [])
            self.assertEqual(payload["records"][0]["status"], "dry_run")
            self.assertEqual(payload["records"][0]["reason"], "check_mode")
            self.assertEqual(payload["records"][0]["planned_status"], status)

    def test_check_mode_final_check_rc_one_preserves_failure_without_actual_skip(self):
        self.callback.v2_runner_on_ok(event(status="started", stage="restart_check", check_mode=True))
        self.callback.v2_runner_on_failed(result(data={"rc": 1}))
        payload = self.read()
        self.assertEqual(payload["skipped"], [])
        self.assertEqual(payload["errors"][0]["status"], "check_failed")
        self.assertTrue(payload["errors"][0]["task_failed"])

    def test_cli_check_mode_cannot_be_downgraded_by_an_event(self):
        self.callback._check_mode = True
        self.callback.v2_runner_on_ok(event(check_mode=False))
        self.assertEqual(self.read()["skipped"], [])
        self.assertEqual(self.read()["records"][0]["status"], "dry_run")

    def test_host_errors_do_not_copy_raw_output_or_hostvars(self):
        self.start_client()
        self.callback.v2_runner_on_failed(result(data={
            "msg": "PASSWORD", "stdout": "STDOUT_SECRET", "stderr": "STDERR_SECRET",
            "hostvars": {"ansible_password": "HOST_SECRET"},
        }))
        text = self.callback._json_path.read_text()
        for secret in ("PASSWORD", "STDOUT_SECRET", "STDERR_SECRET", "HOST_SECRET"):
            self.assertNotIn(secret, text)

    def test_no_log_results_are_not_collected(self):
        self.start_client()
        item = event(fs="SECRET")
        item._result["_ansible_no_log"] = True
        self.callback.v2_runner_on_ok(item)
        self.callback.v2_runner_on_failed(result("private-host", {"msg": "SECRET"}, no_log=True))
        self.callback.v2_playbook_on_stats(Stats())
        self.assertEqual(self.read()["records"], [])
        self.assertEqual(self.read()["host_errors"], [])
        self.assertNotIn("SECRET", self.callback._json_path.read_text())

    def test_unknown_event_keys_are_not_serialized(self):
        self.callback.v2_runner_on_ok(event(password="SECRET", ansible_host="SECRET"))
        self.assertNotIn("SECRET", self.callback._json_path.read_text())

    def test_files_are_private_and_concurrent_runs_have_distinct_paths(self):
        second = self.make_callback()
        for callback in (self.callback, second):
            callback.v2_runner_on_ok(event())
            callback.v2_playbook_on_stats(Stats())
            self.assertEqual(stat.S_IMODE(callback._run_directory.stat().st_mode), 0o700)
            for path in (callback._json_path, callback._tsv_path):
                self.assertTrue(path.is_absolute())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertFalse(any(path.name.startswith(".") for path in callback._run_directory.iterdir()))
        self.assertNotEqual(self.callback._json_path, second._json_path)
        self.assertEqual(len(list(self.root.rglob("*.json"))), 2)

    def test_existing_parent_directory_permissions_are_not_changed(self):
        self.root.mkdir(mode=0o755)
        self.root.chmod(0o755)
        self.callback.v2_runner_on_ok(event())
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.callback._run_directory.stat().st_mode), 0o700)

    def test_write_failure_warns_and_does_not_print_success_paths(self):
        self.root.write_text("not a directory")
        self.start_client()
        self.callback.v2_playbook_on_stats(Stats())
        self.assertTrue(self.callback._display.warnings)
        self.assertEqual(self.callback._display.messages, [])

    def test_non_client_runs_do_not_create_reports(self):
        self.callback.v2_playbook_on_start(types.SimpleNamespace(_file_name="/repo/playbooks/cache_05_deploy.yml"))
        self.callback.v2_runner_on_failed(result())
        self.callback.v2_playbook_on_stats(Stats({"host-a": {"failures": 1}}))
        self.assertFalse(self.root.exists())

    def test_tsv_strings_do_not_create_extra_rows_or_columns(self):
        self.callback.v2_runner_on_ok(event(fs="a\nb\tc"))
        self.callback.v2_playbook_on_stats(Stats())
        lines = self.callback._tsv_path.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(len(lines[1].split("\t")), len(report.FIELDS))
        self.assertIn("a\\nb\\tc", lines[1])


if __name__ == "__main__":
    unittest.main()
