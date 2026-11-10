"""Exercise required IB locality through the real detector and role tasks."""
import contextlib
import copy
import importlib.util
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("numa_cpu_fixtures", Path(__file__).with_name("test_deployment_safety.py"))
fixtures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)


class DetectorLocalityTests(unittest.TestCase):
    def setUp(self):
        self.code = fixtures.embedded("roles/dingo_cache/files/cpuset-detect.sh", "PYCPU")
        self.nodes = {0: set(range(4)), 1: set(range(4, 8))}
        self.siblings = {cpu: {cpu // 2 * 2, cpu // 2 * 2 + 1} for cpu in range(8)}

    def run_detector(self, arguments, claims=None):
        snapshot = (set(range(8)), self.nodes, self.siblings, claims or {}, {cpu: cpu for cpu in range(8)})
        output = io.StringIO()
        with patch.dict(self.code, collect=lambda args: snapshot), \
                patch.object(sys, "argv", ["detector", *arguments]), contextlib.redirect_stdout(output):
            rc = self.code["main"]()
        return rc, output.getvalue()

    def test_required_node_does_not_fall_back_when_another_node_is_free(self):
        choose = self.code["choose"]
        node, selected, _ = choose(self.nodes, self.siblings, set(range(4)), 2, 0, True)
        self.assertEqual((node, selected), (0, []))
        node, selected, _ = choose(self.nodes, self.siblings, set(range(4)), 2, 0)
        self.assertEqual((node, selected), (1, [4, 6]))

    def test_force_stays_local_and_cannot_steal_excluded_sibling_cores(self):
        claimed = {cpu: {"fixture owner"} for cpu in range(8)}
        args = ["--force", "--preferred-only", "-n", "0", "-s", "2"]
        rc, output = self.run_detector(args, claimed)
        self.assertEqual(rc, 2)
        self.assertIn("FORCED=0,2\n", output)
        self.assertIn("FORCED_NODE=0\n", output)
        rc, output = self.run_detector(args + ["-x", "1"], claimed)
        self.assertEqual(rc, 2)
        self.assertIn("FORCED=NONE\n", output)
        self.assertIn("FORCED_NODE=0\n", output)
        # With locality explicitly disabled, the original fallback is retained.
        rc, output = self.run_detector(["--force", "-n", "0", "-s", "2", "-x", "1"], claimed)
        self.assertIn("FORCED=4,6\n", output)

    def test_manual_and_retained_remote_masks_are_not_forceable_locality_conflicts(self):
        for cpus in ("4,6", "0,4"):
            with self.subTest(cpus=cpus):
                rc, output = self.run_detector(["--preferred-only", "--force", "-n", "0", "-c", cpus])
                self.assertEqual(rc, 1)
                self.assertIn("NOT-LOCAL-NUMA", output)
        rc, output = self.run_detector(["--preferred-only", "-n", "0", "-c", "0,2"])
        self.assertEqual(rc, 0, output)

    def test_required_node_cannot_be_omitted_or_invented(self):
        with self.assertRaisesRegex(ValueError, "requires an explicit"):
            self.run_detector(["--preferred-only"])
        with self.assertRaisesRegex(ValueError, "no CPU topology"):
            self.run_detector(["--preferred-only", "-n", "2"])


@unittest.skipUnless(shutil.which("ansible-playbook"), "requires Ansible")
class RoleLocalityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ansible-numa-locality-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cpu = fixtures.CpuTests()
        self.cpu.setUp()
        self.addCleanup(self.cpu.doCleanups)
        self.fakebin = self.root / "fakebin"
        self.fakebin.mkdir()
        numactl = self.fakebin / "numactl"
        numactl.write_text("#!/bin/sh\nexit 0\n")
        numactl.chmod(0o755)
        source = (REPO / "roles/dingo_cache/files/cpuset-detect.sh").read_text()
        source = source.split("<<'PYCPU'\n", 1)[1].rsplit("\nPYCPU", 1)[0]
        self.detector = self.root / "detector"
        # Substitute only the kernel filesystem boundary. Selection, ownership,
        # command-line parsing and role decisions all use their real code.
        shim = self.root / "detector.py"
        shim.write_text(
            "#!" + sys.executable + "\nimport sys\nfrom pathlib import Path\n"
            + "code=" + repr(source) + "\nns={'__name__':'fixture'}\nexec(code,ns)\n"
            + "root=Path(" + repr(str(self.cpu.root)) + ")\n"
            + "original=ns['Path']\n"
            + "def mapped(value):\n value=str(value)\n return root/value.lstrip('/') if value.startswith(('/sys/','/proc','/etc/kubernetes/','/var/lib/kubelet/')) else original(value)\n"
            + "ns['Path']=mapped\nsys.argv.insert(1,'--no-busy')\nsys.exit(ns['main']())\n"
        )
        self.detector.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(shim)) + ' "$@"\n')
        self.detector.chmod(0o755)

    def reserve_local_cpus(self):
        self.cpu.write("/proc/900001/cmdline", "/opt/other/cache\0--cpuset=0-3\0")
        self.cpu.write("/proc/900001/status", "State:\tS\nThreads:\t1\nCpus_allowed_list:\t0-7\n")
        self.cpu.write("/proc/900001/task/900001/status", "State:\tS\nCpus_allowed_list:\t0-3\n")
        (self.cpu.root / "proc/900001/exe").symlink_to("/opt/other/cache")

    def render(self, component, settings=None, existing="", expected=None, succeeds=True):
        prefix = "_cache" if component == "dingo_cache" else "_client"
        short = "cache" if component == "dingo_cache" else "client"
        base = self.root / (short + "-" + str(len(list(self.root.glob('play-*.yml')))))
        base.mkdir()
        installed = base / ("dingo-cache.conf" if short == "cache" else "dingofs-mount.sh")
        if existing:
            installed.write_text("--cpuset=" + existing + "\n")
        variables = {
            "ansible_connection": "local", "ansible_become": False,
            "ansible_python_interpreter": sys.executable,
            prefix + "_ib_numa_raw": {"stdout": "0"},
            prefix + "_node_mem": {"0": 85, "1": 10},
            component + "_base_dir": str(base), component + "_shards": 2,
            component + "_mds_addrs": "127.0.0.1:7000",
            "client_instance": {"fsname": "fixture"},
            "dingo_client_fsname": "fixture", "dingo_client_cpusets_claimed": [],
            "dingo_client_scripts_processed": [],
        }
        variables.update(settings or {})
        defaults = yaml.safe_load((REPO / ("roles/" + component + "/defaults/main.yml")).read_text())
        variables = dict(defaults, **variables)
        main = yaml.safe_load((REPO / ("roles/" + component + "/tasks/main.yml")).read_text())
        start = next(i for i, task in enumerate(main) if task.get("name", "").startswith("Decide preferred NUMA node"))
        end = next(i for i, task in enumerate(main) if task.get("name", "").startswith("Log preferred NUMA node"))
        tasks = copy.deepcopy(main[start:end + 1])
        cpu_source = main if short == "cache" else yaml.safe_load((REPO / "roles/dingo_client/tasks/instance_configure.yml").read_text())
        block = copy.deepcopy(next(task for task in cpu_source if task.get("name", "").startswith("Resolve cpuset for")))
        fixture_block = []
        for task in block["block"]:
            if task.get("name", "").startswith("Deploy cpuset detection script"):
                continue
            if task.get("name", "").startswith("Collect cpusets from other active mount scripts"):
                fixture_block.append({"set_fact": {"_client_other_mounted_cpusets": {"stdout_lines": []}}})
                continue
            if "shell" in task:
                task["shell"] = task["shell"].replace("/tmp/dingo-cpuset-detect.sh", str(self.detector))
            fixture_block.append(task)
        block["block"] = fixture_block
        tasks.append(block)
        if short == "cache":
            tasks.append({"include_tasks": str(REPO / "roles/dingo_cache/tasks/numa_binding.yml")})
        else:
            binding_start = next(i for i, task in enumerate(cpu_source) if task.get("name", "").startswith("Reset CPU-node metadata"))
            binding_end = next(i for i, task in enumerate(cpu_source) if task.get("name", "").startswith("Preview the replacement mount script"))
            tasks.extend(copy.deepcopy(cpu_source[binding_start:binding_end]))
        if expected:
            tasks.append({"assert": {"that": expected}})
        output = base / "start.sh"
        template = "dingo-cache-start.sh.j2" if short == "cache" else "dingofs-mount.sh.j2"
        tasks.append({"template": {"src": str(REPO / ("roles/" + component + "/templates/" + template)), "dest": str(output)}})
        play = [{"hosts": "localhost", "gather_facts": False, "become": False,
                 "vars": variables, "environment": {"PATH": str(self.fakebin) + ":" + os.environ["PATH"]},
                 "tasks": tasks}]
        path = self.root / ("play-" + str(len(list(self.root.glob('play-*.yml')))) + ".yml")
        path.write_text(yaml.safe_dump(play, sort_keys=False))
        result = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO,
                                env=dict(os.environ, ANSIBLE_CONFIG=str(REPO / "ansible.cfg")),
                                text=True, capture_output=True, timeout=60)
        if succeeds:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            subprocess.run(["/bin/bash", "-n", str(output)], check=True)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(output.exists())
        return result.stdout + result.stderr, output.read_text() if output.exists() else ""

    def test_hot_ib_node_and_old_remote_cpuset_move_to_ib_for_both_roles(self):
        for component in ("dingo_cache", "dingo_client"):
            with self.subTest(component=component):
                output, script = self.render(component, existing="4,6", expected=[
                    component + "_cpuset_effective == '0,2'",
                    component + "_numactl_node_effective == '0'",
                ])
                self.assertIn("numactl --cpunodebind=0 --preferred=0", script)
                self.assertIn("exceeds 80%", output)
                source = "_cpuset_source" if component == "dingo_cache" else "_client_cpuset_source"
                expected_source = "kept from existing config" if component == "dingo_cache" else "kept from existing mount script"
                self.render(component, existing="0,2", expected=[source + " == '" + expected_source + "'"])

    def test_force_stays_local_and_client_preserves_sibling_reservations(self):
        self.reserve_local_cpus()
        for component in ("dingo_cache", "dingo_client"):
            self.render(component, {component + "_cpuset_on_insufficient": "force"}, expected=[component + "_cpuset_effective == '0,2'"])
        self.render("dingo_client", {"dingo_client_cpuset_on_insufficient": "force", "dingo_client_cpusets_claimed": ["0"]}, succeeds=False)

    def test_default_shares_claimed_local_cpus_but_explicit_fail_is_respected(self):
        self.reserve_local_cpus()
        for component in ("dingo_cache", "dingo_client"):
            _, script = self.render(component, expected=[component + "_cpuset_effective == '0,2'"])
            self.assertIn('numactl --cpunodebind=0 --preferred=0', script)
            self.render(component, {component + '_cpuset_on_insufficient': 'fail'}, succeeds=False)

    def test_required_locality_rejects_manual_remote_masks_and_unknown_topology(self):
        for component in ("dingo_cache", "dingo_client"):
            prefix = "_cache" if component == "dingo_cache" else "_client"
            output, _ = self.render(component, {component + "_cpuset": "4,6", component + "_cpuset_on_insufficient": "force"}, succeeds=False)
            self.assertIn("must stay on IB NUMA", output)
            output, _ = self.render(component, {prefix + "_ib_numa_raw": {"stdout": "unknown"}}, succeeds=False)
            self.assertIn("requires a known IB NUMA node", output)

    def test_switch_off_restores_fallback_and_cross_node_manual_sets_stay_unbound(self):
        for component in ("dingo_cache", "dingo_client"):
            _, script = self.render(component, {"dingofs_numa_require_ib": False}, expected=[component + "_cpuset_effective == '4,6'"])
            self.assertIn("numactl --cpunodebind=1 --preferred=1", script)
            _, script = self.render(component, {"dingofs_numa_require_ib": False, component + "_cpuset": "0,4"})
            self.assertNotIn("numactl --cpunodebind=", script)

    def test_warn_and_pin_cpu_off_cannot_escape_required_ib_node(self):
        self.reserve_local_cpus()
        for component in ("dingo_cache", "dingo_client"):
            for settings in ({component + "_cpuset_on_insufficient": "warn"}, {component + "_pin_cpu": False}):
                _, script = self.render(component, settings)
                self.assertIn("numactl --cpunodebind=0 --preferred=0", script)
            _, script = self.render(component, {component + "_numactl_membind": True, component + "_pin_cpu": False})
            self.assertIn("numactl --cpunodebind=0 --membind=0", script)

    def test_remote_overrides_fail_and_rdma_disabled_hosts_keep_working(self):
        for component in ("dingo_cache", "dingo_client"):
            prefix = "_cache" if component == "dingo_cache" else "_client"
            output, _ = self.render(component, {component + "_numactl_node": 1}, succeeds=False)
            self.assertIn("matching CPU/memory overrides", output)
            self.render(component, {component + "_use_rdma": False, prefix + "_ib_numa_raw": {"stdout": "unknown"}},
                        expected=[component + "_cpuset_effective == '4,6'"])
        output, _ = self.render("dingo_client", {"client_instance": {"fsname": "fixture", "cpuset_numa_node": 1}}, succeeds=False)
        self.assertIn("must use IB NUMA", output)

    def test_example_inventory_uses_ib_local_80_percent_preferred_defaults(self):
        inventory = yaml.safe_load((REPO / "inventory/group_vars/all.yml").read_text())
        for component in ("dingo_cache", "dingo_client"):
            keys = ["dingofs_numa_require_ib", component + "_ib_node_mem_max_pct",
                    component + "_numactl_enabled", component + "_numactl_node",
                    component + "_numactl_membind"]
            output, script = self.render(component, {key: inventory[key] for key in keys if key in inventory})
            self.assertIn("exceeds 80%", output)
            self.assertIn("numactl --cpunodebind=0 --preferred=0", script)


if __name__ == "__main__":
    unittest.main()
