"""Regression coverage for shared CLI and additional-cluster deployment."""
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import pwd
import grp
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

import yaml

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dingo_filters", REPO / "filter_plugins/dingo_cluster.py")
filters = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(filters)


def topology(root="/mnt/disk1/new", port=700):
    return {
        "kind": "dingofs",
        "global": {
            "container_image": "registry/new:release",
            "log_dir": root + "/logs/${service_role}/${service_replica_sequence}",
            "source_core_dir": "/mnt/shared/core",
            "target_core_dir": "/mnt/shared/core",
            "variable": {"machine1": "meta1", "home": root + "/work"},
        },
        "mds_services": {
            "config": {"server.port": str(port) + "${service_replica_sequence}",
                       "mds_storage_engine": "tikv", "storage_url": "list://127.0.0.1:2379",
                       "mds_compact_chunk_enable": True},
            "deploy": [{"host": "${machine1}", "replicas": 3}],
        },
    }


def status(name="new", count=3, state="Up 3 minutes"):
    return "cluster name : " + name + "\n" + "\n".join(
        "id%d mds meta1 1/3 container%d %s" % (i, i, state) for i in range(count)
    )


class FilterTests(unittest.TestCase):
    def test_cluster_names_are_exact_and_preserve_default(self):
        result = filters.cluster_list("\x1b[32m* v5.1\x1b[0m\n  v5.2\n  v5.2-tikv\n")
        self.assertEqual(result["names"], ["v5.1", "v5.2", "v5.2-tikv"])
        self.assertEqual(result["current"], "v5.1")

    def test_ambiguous_or_unexpected_cluster_output_fails(self):
        for output in ["* a\n* b", "a\na", "Error-Code: 100000", "Cluster Name"]:
            with self.subTest(output=output), self.assertRaises(ValueError):
                filters.cluster_list(output)

    def test_host_address_user_and_port_are_read(self):
        hosts = filters.hosts_list("Host Hostname User Port\n---- ---- ---- ----\nmeta1 127.0.0.1 service 22 key N -\n")
        self.assertEqual(hosts["meta1"], {"address": "127.0.0.1", "user": "service", "port": 22})
        with self.assertRaises(ValueError):
            filters.hosts_list("Error: database unavailable")

    def test_shared_config_validation_never_returns_secret_values(self):
        content = '[defaults]\nengine=podman\nauto_upgrade=false\n[database]\nurl="rqlite://secret@host:4001"\n'
        self.assertTrue(filters.cli_config(content, "rqlite://secret@host:4001", "podman")["valid"])
        result = filters.cli_config(content, "other", "docker")
        self.assertFalse(result["valid"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertFalse(filters.cli_config(content.replace("false", "true"), "rqlite://secret@host:4001", "podman")["valid"])

    def test_expanded_plan_tracks_each_replica_port_and_path(self):
        plan = filters.cluster_plan(topology())
        self.assertEqual(plan["ports"]["meta1"], [7000, 7001, 7002])
        self.assertEqual(plan["instances"], {"mds": {"meta1": 3}})
        self.assertIn("/mnt/disk1/new/logs/mds/2", plan["directories"]["meta1"])

    def test_duplicate_ports_fail_before_deployment(self):
        doc = topology()
        doc["mds_services"]["config"]["server.port"] = 7000
        with self.assertRaises(ValueError):
            filters.cluster_plan(doc)

    def test_stopped_cluster_still_reserves_ports_and_directories(self):
        new = filters.cluster_plan(topology())
        old = filters.cluster_plan(topology("/mnt/disk1/old", 700))
        self.assertFalse(filters.plan_conflicts(new, old)["compatible"])
        doc = topology("/mnt/disk1/old", 690)
        doc["global"]["log_dir"] = "/mnt/disk1/new/logs"
        old = filters.cluster_plan(doc)
        self.assertFalse(filters.plan_conflicts(new, old)["compatible"])

    def test_independent_paths_can_share_core_dump_directory(self):
        new = filters.cluster_plan(topology())
        old = filters.cluster_plan(topology("/mnt/disk1/old", 690))
        self.assertTrue(filters.plan_conflicts(new, old)["compatible"])
        self.assertFalse(filters.managed_directory("/mnt/shared/core", ["/mnt/shared"], new))
        self.assertTrue(filters.managed_directory("/mnt/disk1/new/logs/mds/0", ["/mnt/disk1"], new))
        self.assertFalse(filters.managed_directory("/mnt/disk1/new/logs/mds/0", ["/mnt/disk2"], new))

    def test_healthy_status_requires_all_replicas_and_correct_cluster(self):
        plan = filters.cluster_plan(topology())
        self.assertTrue(filters.cluster_status(status(), plan, "new")["healthy"])
        self.assertFalse(filters.cluster_status(status(count=2), plan, "new")["healthy"])
        with self.assertRaises(ValueError):
            filters.cluster_status(status("other"), plan, "new")

    def test_cleaned_partial_or_failed_queries_cannot_trigger_initialization(self):
        plan = filters.cluster_plan(topology())
        for state in ["Cleaned", "Exited (1)", "Lost", "Restarting"]:
            self.assertFalse(filters.cluster_status(status(state=state), plan, "new")["healthy"])
        for output in ["", "cluster name : new\nError: SSH failed"]:
            with self.assertRaises(ValueError):
                filters.cluster_status(output, plan, "new")

    def test_tiup_health_checks_every_component_and_requires_rows(self):
        healthy = "127.0.0.1:2379 pd 127.0.0.1 2379/2380 linux/x86_64 Up|L /data /deploy"
        self.assertTrue(filters.tiup_health(healthy)["healthy"])
        self.assertFalse(filters.tiup_health(healthy + "\n" + healthy.replace("Up|L", "Starting"))["healthy"])
        self.assertFalse(filters.tiup_health("unrecognised response")["healthy"])


MOCK_CLI = r'''
import json, os, pathlib, sys
state_file = pathlib.Path(STATE)
state = json.loads(state_file.read_text())
args = sys.argv[1:]
target = os.environ.get("DINGO_CLUSTER") or os.environ.get("dingo_cluster") or state["current"]
with pathlib.Path(CALLS).open("a") as f:
    f.write(json.dumps({"args": args, "target": target, "version": VERSION}) + "\n")
if args == ["--version"]:
    if state.get("bad_candidate") and VERSION == "candidate":
        sys.exit(12)
    if state.get("bad_installed_candidate") and VERSION == "candidate" and pathlib.Path(sys.argv[0]).name == "dingo":
        sys.exit(13)
    print("dingo Version 3.1 commit: " + VERSION)
elif args == ["cluster", "list"]:
    if state.get("fail_list"):
        sys.exit(17)
    for name in sorted(state["topologies"]):
        print(("* " if name == state["current"] else "  ") + name)
elif args == ["hosts", "list"]:
    print("Host Hostname User Port Private")
    print("---- ---- ---- ---- ----")
    print("meta1 127.0.0.1 " + USER + " 22 key")
elif args == ["config", "show"]:
    print(state["topologies"][target])
elif args[:2] == ["cluster", "add"]:
    name = args[2]
    if name in state["topologies"]:
        sys.exit(14)
    state["topologies"][name] = pathlib.Path(args[args.index("-f") + 1]).read_text()
    state["statuses"][name] = "Cleaned"
    state_file.write_text(json.dumps(state))
    print("Added cluster " + name)
elif args[:2] == ["cluster", "deploy"]:
    state["statuses"][target] = "Up"
    if state.get("fail_deploy"):
        state["statuses"][target] = "partial"
        state_file.write_text(json.dumps(state))
        sys.exit(15)
    state_file.write_text(json.dumps(state))
    print("deployed")
elif args[:2] == ["cluster", "status"]:
    if state.get("fail_status"):
        sys.exit(16)
    print("cluster name : " + target)
    health = state["statuses"][target]
    count = 2 if health == "partial" else 3
    for i in range(count):
        print("id%d mds meta1 1/3 container%d %s" % (i, i, health))
else:
    # No checkout, config commit, hosts commit, cluster rm, or lifecycle command
    # for an old service is allowed in this regression fixture.
    print("UNEXPECTED MUTATION", args)
    sys.exit(97)
'''


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@unittest.skipUnless(shutil.which("ansible-playbook"), "requires Ansible")
class AdditionalClusterIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ansible-add-cluster-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.user = pwd.getpwuid(os.getuid()).pw_name
        self.group = grp.getgrgid(os.getgid()).gr_name
        self.home = self.root / "service"
        self.bin = self.home / ".dingo/bin"
        self.bin.mkdir(parents=True)
        self.state_path = self.root / "state.json"
        self.calls_path = self.root / "calls.jsonl"
        self.cli = self.bin / "dingo"
        self.config = self.home / ".dingo/dingocli.cfg"
        self.config.write_text('[defaults]\nengine=fake-container\nauto_upgrade=false\n[database]\nurl="rqlite://127.0.0.1:4001"\n')
        self.initial_config = self.config.read_bytes()
        self.mount = self.root / "disk"
        self.mount.mkdir()
        self.old_topology = yaml.safe_dump(topology(str(self.mount / "old"), 690))
        self.state = {"current": "v5.1", "topologies": {"v5.1": self.old_topology},
                      "statuses": {"v5.1": "Up"}}
        self.write_state()
        self.old_binary = self.mock_cli("installed")
        self.cli.write_text(self.old_binary)
        self.cli.chmod(0o755)
        self.fakebin = self.root / "fakebin"
        self.fakebin.mkdir()
        for name, content in {
            "mountpoint": "#!/bin/sh\nexit 0\n",
            "ss": "#!/bin/sh\nexit 0\n",
            "fake-container": "#!/bin/sh\nif [ \"$1\" = pull ]; then echo pulled; fi\nexit 0\n",
        }.items():
            p = self.fakebin / name
            p.write_text(content)
            p.chmod(0o755)
        getent = self.fakebin / "getent"
        getent.write_text("#!/bin/sh\necho '" + self.user + ":x:" + str(os.getuid()) + ":" + str(os.getgid()) + "::" + str(self.home) + ":/bin/sh'\n")
        getent.chmod(0o755)
        self.play_dir = self.root / "playbooks"
        self.play_dir.mkdir()
        (self.root / "VERSION").write_text("v5.2\n")
        self.inventory = self.root / "hosts.yml"
        self.inventory.write_text(yaml.safe_dump({
            "all": {"children": {
                "meta_servers": {"hosts": {"meta1": {"ansible_host": "127.0.0.1"}}},
                "admin": {"hosts": {"meta1": {}}},
            }}
        }))
        self.variables = {
            "ansible_connection": "local", "ansible_python_interpreter": sys.executable,
            "ansible_become": False,
            "dingofs_site_version": "v5.2", "dingofs_meta_existing_hosts": True,
            "dingofs_user": self.user, "dingofs_home": str(self.home),
            "dingofs_group": self.group,
            "dingofs_pkg_dir": str(self.home / "pkg"),
            "dingofs_cluster_name": "new", "dingofs_mds_storage_engine": "tikv",
            "dingofs_mds_storage_url": "list://127.0.0.1:2379",
            "dingofs_container_image": "registry/new:release",
            "dingofs_container_version": "v5.2",
            "dingofs_log_dir": str(self.mount / "new/logs/${service_role}/${service_replica_sequence}"),
            "dingofs_core_dir": "/mnt/shared/core",
            "dingofs_home_tmp": str(self.mount / "new/work"),
            "dingofs_existing_mounts": [str(self.mount)],
            "dingofs_dir_list": [str(self.mount / ("new/logs/mds/" + str(i))) for i in range(3)],
            "mds_base_port": 700, "mds_replicas": 3,
            "container_engine": "fake-container", "dingo_cli_engine": "fake-container",
            "dingo_cli_db_url": "rqlite://127.0.0.1:4001",
            "dingo_cli_bin_dir": str(self.bin),
            "dingo_cli_pkg_path": str(self.root / "controller/dingo"),
        }
        self.env = os.environ.copy()
        self.env["ANSIBLE_CONFIG"] = str(REPO / "ansible.cfg")
        self.env["ANSIBLE_ROLES_PATH"] = str(REPO / "roles")
        self.env["ANSIBLE_FILTER_PLUGINS"] = str(REPO / "filter_plugins")

    def write_state(self):
        self.state_path.write_text(json.dumps(self.state))

    def read_state(self):
        return json.loads(self.state_path.read_text())

    def mock_cli(self, version):
        preamble = "#!" + sys.executable + "\n"
        preamble += "STATE = " + repr(str(self.state_path)) + "\n"
        preamble += "CALLS = " + repr(str(self.calls_path)) + "\n"
        preamble += "USER = " + repr(self.user) + "\n"
        preamble += "VERSION = " + repr(version) + "\n"
        return preamble + MOCK_CLI

    def calls(self, command):
        if not self.calls_path.exists():
            return []
        return [row for row in map(json.loads, self.calls_path.read_text().splitlines())
                if row["args"][:len(command)] == command]

    def run_role(self, name, extra=None, succeeds=True):
        variables = dict(self.variables)
        variables.update(extra or {})
        tasks = [{"ansible.builtin.include_role": {"name": name}}]
        play = [{"hosts": "admin", "gather_facts": False, "become": False,
                 "vars": variables,
                 "environment": {"PATH": str(self.fakebin) + ":" + os.environ["PATH"]},
                 "tasks": tasks}]
        p = self.play_dir / "test.yml"
        p.write_text(yaml.safe_dump(play, sort_keys=False))
        result = subprocess.run(["ansible-playbook", "-i", str(self.inventory), str(p)],
                                cwd=REPO, env=self.env, text=True, capture_output=True, timeout=90)
        if succeeds and result.returncode:
            self.fail(result.stdout + "\n" + result.stderr)
        if not succeeds and not result.returncode:
            self.fail("Expected the deployment to fail")
        return result

    def test_first_deploy_and_repeat_preserve_old_cluster_and_default(self):
        self.run_role("dingofs_cluster")
        self.assertEqual(len(self.calls(["cluster", "deploy"])), 1)
        data = self.read_state()
        self.assertEqual(data["current"], "v5.1")
        self.assertEqual(data["topologies"]["v5.1"], self.old_topology)
        self.assertEqual(self.config.read_bytes(), self.initial_config)
        self.run_role("dingofs_cluster")
        self.assertEqual(len(self.calls(["cluster", "deploy"])), 1)
        self.assertFalse(self.calls(["cluster", "checkout"]))
        self.assertFalse(self.calls(["config", "commit"]))
        self.assertFalse(self.calls(["hosts", "commit"]))

    def test_partial_deploy_is_not_reinitialized_on_repeat(self):
        self.state["fail_deploy"] = True
        self.write_state()
        self.run_role("dingofs_cluster", succeeds=False)
        self.assertEqual(len(self.calls(["cluster", "deploy"])), 1)
        self.run_role("dingofs_cluster", succeeds=False)
        self.assertEqual(len(self.calls(["cluster", "deploy"])), 1)
        self.assertEqual(self.read_state()["current"], "v5.1")
        self.assertEqual(self.read_state()["topologies"]["v5.1"], self.old_topology)

    def test_registered_topology_drift_and_failed_status_are_not_overwritten(self):
        self.run_role("dingofs_cluster")
        result = self.run_role("dingofs_cluster", {"mds_base_port": 701}, succeeds=False)
        self.assertIn("different", result.stdout)
        self.state = self.read_state()
        self.state["fail_status"] = True
        self.write_state()
        self.run_role("dingofs_cluster", succeeds=False)
        self.assertEqual(len(self.calls(["cluster", "deploy"])), 1)

    def test_conflicting_port_is_reserved_even_when_old_service_is_down(self):
        self.state["topologies"]["v5.1"] = yaml.safe_dump(topology(str(self.mount / "old"), 700))
        self.state["statuses"]["v5.1"] = "Exited"
        self.write_state()
        self.run_role("dingofs_cluster", succeeds=False)
        self.assertFalse(self.calls(["cluster", "add"]))

    def test_first_install_entry_refuses_failed_registration_query(self):
        self.state["fail_list"] = True
        self.write_state()
        self.run_role("dingofs_cluster", {"dingofs_meta_existing_hosts": False}, succeeds=False)
        self.assertFalse(self.calls(["cluster", "deploy"]))
        self.assertFalse(self.calls(["cluster", "add"]))
        self.assertFalse(self.calls(["hosts", "commit"]))

    def test_first_install_entry_verifies_registered_target_without_reinitialization(self):
        self.run_role("dingofs_cluster")
        self.run_role("dingofs_cluster", {"dingofs_meta_existing_hosts": False})
        self.assertEqual(len(self.calls(["cluster", "deploy"])), 1)
        self.assertFalse(self.calls(["config", "commit"]))
        self.state = self.read_state()
        self.state["statuses"]["new"] = "partial"
        self.write_state()
        self.run_role("dingofs_cluster", {"dingofs_meta_existing_hosts": False}, succeeds=False)
        self.assertEqual(len(self.calls(["cluster", "deploy"])), 1)

    def prepare_server_cli(self):
        candidate = Path(self.variables["dingo_cli_pkg_path"])
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(self.mock_cli("candidate"))
        # Server operations must succeed with no usable download URL.
        self.variables["dingo_cli_download_url"] = "http://127.0.0.1:1/must-not-be-used"

    def test_cli_update_backs_up_binary_preserves_config_and_rolls_back_failed_probe(self):
        self.prepare_server_cli()
        self.state["bad_candidate"] = True
        self.write_state()
        self.run_role("dingo_cli", {"dingo_cli_update_existing": True}, succeeds=False)
        self.assertEqual(self.cli.read_text(), self.old_binary)
        self.state.pop("bad_candidate")
        self.write_state()
        self.run_role("dingo_cli", {"dingo_cli_update_existing": True})
        self.assertEqual(self.cli.read_text(), self.mock_cli("candidate"))
        self.assertEqual(self.config.read_bytes(), self.initial_config)
        old_hash = hashlib.sha256(self.old_binary.encode()).hexdigest()
        self.assertEqual((self.bin / ("backups/dingo-" + old_hash)).read_text(), self.old_binary)
        # Restore the fixture's old executable, then make only the candidate's
        # post-install version probe fail (the candidate-stage probe succeeds).
        self.cli.write_text(self.old_binary)
        self.state = self.read_state()
        self.state["bad_installed_candidate"] = True
        self.write_state()
        self.run_role("dingo_cli", {"dingo_cli_update_existing": True}, succeeds=False)
        self.assertEqual(self.cli.read_text(), self.old_binary)
        self.assertEqual(self.config.read_bytes(), self.initial_config)
        self.assertFalse(list(self.bin.glob(".dingo-update-*")))

    def test_missing_manual_server_cli_fails_without_downloading_or_changing_installation(self):
        result = self.run_role("dingo_cli", {"dingo_cli_update_existing": True,
                                              "dingo_cli_download_url": "http://127.0.0.1:1/unused"},
                               succeeds=False)
        self.assertIn("Prepare the compatible server dingo binary", result.stdout)
        self.assertEqual(self.cli.read_text(), self.old_binary)
        self.assertEqual(self.config.read_bytes(), self.initial_config)
        self.assertFalse(Path(self.variables["dingo_cli_pkg_path"]).exists())

    def test_isolated_server_cli_preserves_legacy_binary_and_cleans_up_failed_first_install(self):
        self.prepare_server_cli()
        isolated = self.home / ".dingo/bin/server-new"
        variables = {"dingo_cli_update_existing": True, "dingo_cli_bin_dir": str(isolated)}
        self.state["bad_installed_candidate"] = True
        self.write_state()
        self.run_role("dingo_cli", variables, succeeds=False)
        self.assertFalse((isolated / "dingo").exists())
        self.assertEqual(self.cli.read_text(), self.old_binary)
        self.state.pop("bad_installed_candidate")
        self.write_state()
        self.run_role("dingo_cli", variables)
        self.assertEqual((isolated / "dingo").read_text(), self.mock_cli("candidate"))
        self.assertEqual(self.cli.read_text(), self.old_binary)
        self.assertEqual(self.config.read_bytes(), self.initial_config)

    def test_check_mode_checks_tikv_without_mutations_and_honors_inventory_store_count(self):
        stores = [9]

        class Handler(QuietHandler):
            def do_GET(self):
                data = {"stores": [{"store": {"state_name": "Up"}} for _ in range(stores[0])]}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tiup = self.fakebin / "tiup"
        tiup.write_text("#!/bin/sh\nif [ \"$2\" = list ]; then echo dingofs-tikv; else echo 'Cluster version: v8.5.8'; echo '127.0.0.1:2379 pd 127.0.0.1 2379/2380 linux/x86_64 Up /data /deploy'; fi\n")
        tiup.chmod(0o755)
        variables = dict(self.variables, tikv_cluster_name="dingofs-tikv", tikv_min_stores=9,
                         tikv_pd_addr="127.0.0.1:" + str(server.server_port))
        args_file = self.root / "args.yml"
        args_file.write_text(yaml.safe_dump(variables))
        env = dict(self.env, PATH=str(self.fakebin) + ":" + os.environ["PATH"])

        def run(playbook):
            return subprocess.run(["ansible-playbook", "-i", str(self.inventory),
                                   "playbooks/" + playbook, "-e", "@" + str(args_file), "--check"],
                                  cwd=REPO, env=env, text=True, capture_output=True, timeout=90)

        result = run("meta_add_cluster.yml")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.cli.read_text(), self.old_binary)
        self.assertEqual(self.read_state()["topologies"], {"v5.1": self.old_topology})
        self.assertEqual(self.config.read_bytes(), self.initial_config)
        self.assertFalse(self.calls(["cluster", "add"]))
        self.assertFalse((self.home / "cluster-config").exists())
        stores[0] = 8
        result = run("meta_add_cluster.yml")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected at least", result.stdout)
        result = run("meta_site.yml")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Run meta_add_cluster.yml", result.stdout)


if __name__ == "__main__":
    unittest.main()
