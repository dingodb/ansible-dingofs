"""Exercise client outcome reporting through real Ansible and isolated fixtures."""
import csv
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


REPO = Path(__file__).resolve().parents[1]
UPGRADE = REPO / "roles/dingo_client_upgrade/tasks/upgrade_instance.yml"
DEPLOY = REPO / "roles/dingo_client/tasks/instance.yml"
CONFIGURE = REPO / "roles/dingo_client/tasks/instance_configure.yml"


@unittest.skipUnless(shutil.which("ansible-playbook"), "requires Ansible")
class ClientReportIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="client-report-integration-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reports = self.root / "reports"
        self.library = self.root / "library"
        self.library.mkdir()
        self.fakebin = self.root / "fakebin"
        self.fakebin.mkdir()
        self.events = self.root / "service-events.jsonl"
        self.candidate = self.root / "candidate"
        self.candidate.write_text("new client binary\n")
        # No real service manager or mount command can be called by these plays.
        (self.library / "systemd.py").write_text('''from ansible.module_utils.basic import AnsibleModule
import json, os
m = AnsibleModule(argument_spec=dict(name=dict(), state=dict(), enabled=dict(type='bool'),
                                    daemon_reload=dict(type='bool')), supports_check_mode=True)
if m.params['state'] == 'restarted' and not m.check_mode:
    with open(os.environ['CLIENT_REPORT_SERVICE_EVENTS'], 'a') as stream:
        stream.write(json.dumps(m.params) + '\\n')
m.exit_json(changed=m.params['state'] == 'restarted', status={'ActiveState': 'active'})
''')
        for name in ("bash", "cp"):
            (self.fakebin / name).symlink_to(shutil.which(name))
        self.executable("mountpoint", "#!/bin/sh\nexit 0\n")
        self.executable("systemctl", "#!/bin/sh\necho active\nexit 0\n")
        self.executable("fuser", "#!/bin/sh\nexit 1\n")
        probe = self.root / "usage_probe.py"
        probe.write_text('''import json, pathlib, sys
base = pathlib.Path(sys.argv[1]).parent
counter = base / 'usage-count'
attempt = int(counter.read_text()) if counter.exists() else 0
plan = json.loads((base / 'usage-plan.json').read_text())
counter.write_text(str(attempt + 1))
code = plan[min(attempt, len(plan) - 1)]
print({0: 'IDLE fixture', 1: 'BUSY fixture', 2: 'UNKNOWN fixture'}[code])
sys.exit(code)
''')
        self.checker = self.root / "usage-check.sh"
        self.checker.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable)
                                + " " + shlex.quote(str(probe)) + ' "$@"\n')
        self.checker.chmod(0o755)
        self.hosts = {}

    def executable(self, name, content):
        path = self.fakebin / name
        path.write_text(content)
        path.chmod(0o755)

    def client(self, host, fsname, usage=(1,)):
        base = self.root / host / fsname
        (base / "bin").mkdir(parents=True)
        (base / "bin/dingo-client").write_text("old client binary\n")
        (base / "usage-plan.json").write_text(json.dumps(usage))
        client = dict(fsname=fsname, base_dir=str(base), mount_point=str(base / "mount"),
                      service_name="fixture-" + host + "-" + fsname)
        self.hosts.setdefault(host, {"dingo_clients": []})["dingo_clients"].append(client)
        return client

    def service_restarts(self):
        if not self.events.exists():
            return []
        return [json.loads(line)["name"] for line in self.events.read_text().splitlines()]

    def run_play(self, tasks, check=False, extra=None):
        variables = dict(ansible_connection="local", ansible_become=False,
                         ansible_python_interpreter=sys.executable,
                         dingo_client_upgrade_bin_src=str(self.candidate),
                         dingo_client_mount_check_script=str(self.checker),
                         dingo_client_upgrade_dry_run=False)
        variables.update(extra or {})
        inventory = self.root / "inventory.yml"
        inventory.write_text(yaml.safe_dump({"all": {"hosts": self.hosts}}))
        play = self.root / "fixture.yml"
        play.write_text(yaml.safe_dump([dict(
            hosts="all", gather_facts=False, become=False, serial=1, vars=variables,
            environment={"PATH": str(self.fakebin),
                         "CLIENT_REPORT_SERVICE_EVENTS": str(self.events)}, tasks=tasks)]))
        env = dict(os.environ, ANSIBLE_CONFIG=str(REPO / "ansible.cfg"),
                   ANSIBLE_LIBRARY=str(self.library), ANSIBLE_STDOUT_CALLBACK="default",
                   ANSIBLE_CALLBACK_PLUGINS=str(REPO / "callback_plugins"),
                   ANSIBLE_CALLBACKS_ENABLED="dingofs_client_report",
                   DINGO_CLIENT_REPORT_DIR=str(self.reports))
        result = subprocess.run(["ansible-playbook", "-i", str(inventory), str(play)]
                                + (["--check"] if check else []),
                                cwd=REPO, env=env, text=True, capture_output=True, timeout=90)
        diagnostic = result.stdout + "\n" + result.stderr
        paths = list(self.reports.rglob("*.json"))
        self.assertEqual(len(paths), 1, diagnostic)
        report = json.loads(paths[0].read_text())
        tsv = list(self.reports.rglob("*.tsv"))
        self.assertEqual(len(tsv), 1, diagnostic)
        with tsv[0].open(newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter="\t"))
        self.assertEqual(len(rows), len(report["skipped"]), diagnostic)
        self.assertTrue(report["complete"], diagnostic)
        return result, report

    @staticmethod
    def upgrade_tasks():
        return [{"include_tasks": str(UPGRADE), "loop": "{{ dingo_clients }}",
                 "loop_control": {"loop_var": "client_instance"}}]

    @staticmethod
    def deployment_tasks():
        return [{"include_tasks": str(DEPLOY), "loop": "{{ dingo_clients }}",
                 "loop_control": {"loop_var": "client_instance"},
                 "vars": {"dingo_client_fsname": "{{ client_instance.fsname }}",
                          "dingo_client_mount_point": "{{ client_instance.mount_point }}",
                          "dingo_client_service_name": "{{ client_instance.service_name }}"}}]

    def test_serial_upgrade_keeps_all_hosts_and_filesystems_in_one_report(self):
        clients = [self.client("node-a", "alpha", (1,)),
                   self.client("node-a", "beta", (0,)),
                   self.client("node-b", "alpha", (0,)),
                   self.client("node-b", "beta", (1,))]
        result, report = self.run_play(self.upgrade_tasks())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        records = {(r["host"], r["fsname"]): r for r in report["records"]}
        self.assertEqual(set(records), {("node-a", "alpha"), ("node-a", "beta"),
                                       ("node-b", "alpha"), ("node-b", "beta")})
        busy = {("node-a", "alpha"), ("node-b", "beta")}
        self.assertEqual({(r["host"], r["fsname"]) for r in report["skipped"]}, busy)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["host_errors"], [])
        self.assertEqual(set(self.service_restarts()), {"fixture-node-a-beta", "fixture-node-b-alpha"})
        for client in clients:
            host = Path(client["base_dir"]).parent.name
            row = records[(host, client["fsname"])]
            deferred = (host, client["fsname"]) in busy
            self.assertEqual(row["status"], "skipped_busy_before_change" if deferred else "completed")
            self.assertEqual(row["changed_on_disk"], not deferred)
            self.assertEqual((Path(client["base_dir"]) / "bin/dingo-client").read_text(),
                             "old client binary\n" if deferred else "new client binary\n")

    def test_serial_deployment_busy_prechecks_leave_every_instance_unchanged(self):
        clients = [self.client(host, fsname) for host in ["node-a", "node-b"]
                   for fsname in ["alpha", "beta"]]
        result, report = self.run_play(self.deployment_tasks())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(report["records"]), 4)
        self.assertEqual(len(report["skipped"]), 4)
        self.assertEqual(report["errors"], [])
        self.assertEqual(self.service_restarts(), [])
        for row in report["records"]:
            self.assertEqual(row["operation"], "deploy")
            self.assertEqual(row["status"], "skipped_busy_before_change")
            self.assertIs(row["changed_on_disk"], False)
        for client in clients:
            self.assertEqual((Path(client["base_dir"]) / "bin/dingo-client").read_text(),
                             "old client binary\n")

    def test_unknown_upgrade_usage_is_an_error_not_a_busy_skip(self):
        client = self.client("node-a", "alpha", (2,))
        result, report = self.run_play(self.upgrade_tasks())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["skipped"], [])
        self.assertEqual(report["records"][0]["status"], "check_failed")
        self.assertTrue(report["errors"])
        self.assertIn("node-a", report["failed_hosts"])
        self.assertIs(report["records"][0]["changed_on_disk"], False)
        self.assertEqual((Path(client["base_dir"]) / "bin/dingo-client").read_text(),
                         "old client binary\n")
        self.assertEqual(self.service_restarts(), [])

    def test_ansible_check_mode_does_not_report_a_busy_skip_or_replace_a_binary(self):
        client = self.client("node-a", "alpha", (1,))
        result, report = self.run_play(self.upgrade_tasks(), check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(report["check_mode"])
        self.assertEqual(report["skipped"], [])
        self.assertEqual(report["records"][0]["status"], "dry_run")
        self.assertIs(report["records"][0]["changed_on_disk"], False)
        self.assertEqual((Path(client["base_dir"]) / "bin/dingo-client").read_text(),
                         "old client binary\n")
        self.assertFalse((Path(client["base_dir"]) / ".restart-required").exists())
        self.assertEqual(self.service_restarts(), [])

    def test_usage_becoming_busy_after_copy_preserves_failure_and_restart_obligation(self):
        client = self.client("node-a", "alpha", (0, 1))
        result, report = self.run_play(self.upgrade_tasks())
        self.assertNotEqual(result.returncode, 0)
        row = report["records"][0]
        self.assertEqual(row["status"], "restart_deferred_busy")
        self.assertIs(row["changed_on_disk"], True)
        self.assertIs(row["pending_restart"], True)
        self.assertIs(row["task_failed"], True)
        self.assertEqual(len(report["skipped"]), 1)
        self.assertTrue(report["errors"])
        self.assertIn("node-a", report["failed_hosts"])
        self.assertEqual((Path(client["base_dir"]) / "bin/dingo-client").read_text(),
                         "new client binary\n")
        self.assertTrue((Path(client["base_dir"]) / ".restart-required").exists())
        self.assertEqual(self.service_restarts(), [])

    def configure_tail(self):
        tasks = yaml.safe_load(CONFIGURE.read_text())
        start = next(i for i, task in enumerate(tasks)
                     if task.get("name", "").startswith("Set whether a restart is needed"))

        def absolute_imports(value):
            if isinstance(value, list):
                return [absolute_imports(item) for item in value]
            if isinstance(value, dict):
                return {key: str((CONFIGURE.parent / item).resolve())
                        if key == "import_tasks" else absolute_imports(item)
                        for key, item in value.items()}
            return value

        return absolute_imports(tasks[start:])

    def deployment_tail_variables(self, client):
        return dict(
            dingo_client_fsname=client["fsname"], dingo_client_mount_point=client["mount_point"],
            dingo_client_service_name=client["service_name"], dingo_client_mds_addrs="fixture:7000",
            _dingofs_client_report_context=dict(operation="deploy", fsname=client["fsname"],
                                               mount_point=client["mount_point"], service=client["service_name"]),
            _mount_sh={"changed": False}, _mount_service={"changed": False},
            _client_bin={"changed": True}, _dingo_bin={"changed": False})

    def test_deployment_postcheck_busy_reports_updated_files_and_deferred_restart(self):
        client = self.client("node-a", "alpha")
        self.executable("fuser", "#!/bin/sh\nexit 0\n")
        result, report = self.run_play(self.configure_tail(), extra=self.deployment_tail_variables(client))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = report["records"][0]
        self.assertEqual(row["status"], "restart_deferred_busy")
        self.assertIs(row["changed_on_disk"], True)
        self.assertIs(row["pending_restart"], True)
        self.assertEqual(len(report["skipped"]), 1)
        self.assertEqual(report["errors"], [])
        self.assertEqual(self.service_restarts(), [])

    def test_deployment_missing_fuser_is_distinct_from_busy(self):
        client = self.client("node-a", "alpha")
        (self.fakebin / "fuser").unlink()
        result, report = self.run_play(self.configure_tail(), extra=self.deployment_tail_variables(client))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = report["records"][0]
        self.assertEqual(row["status"], "restart_deferred_unknown")
        self.assertIs(row["changed_on_disk"], True)
        self.assertIs(row["pending_restart"], True)
        self.assertEqual(len(report["skipped"]), 1)
        self.assertEqual(report["errors"], [])
        self.assertEqual(self.service_restarts(), [])


if __name__ == "__main__":
    unittest.main()
