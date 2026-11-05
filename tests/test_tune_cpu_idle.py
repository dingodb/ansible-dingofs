"""CPU idle tuning regressions using isolated sysfs fixtures, never host sysfs."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
MODULE = REPO / "roles/tune/library/dingofs_cpu_idle.py"
SPEC = importlib.util.spec_from_file_location("dingofs_cpu_idle", MODULE)
idle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(idle)


class IdleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cpu-idle-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def state(self, cpu, index, name, latency, disabled=0):
        path = self.root / "cpu{}".format(cpu) / "cpuidle" / "state{}".format(index)
        path.mkdir(parents=True)
        for key, value in {"name": name, "latency": latency, "disable": disabled}.items():
            (path / key).write_text(str(value) + "\n")
        return path / "disable"

    def test_partial_configuration_and_different_c1e_indexes(self):
        done = self.state(0, 2, "C1E", 2, 1)
        pending = self.state(1, 7, "C1E", 2)
        c6 = self.state(1, 9, "C6", 170, 1)
        result = idle.configure(self.root)
        self.assertEqual(result["online_cpus"], 2)
        self.assertEqual(result["states_changed"], [str(pending)])
        self.assertEqual([p.read_text().strip() for p in [done, pending, c6]], ["1"] * 3)

    def test_names_latency_and_existing_disabled_states(self):
        poll = self.state(0, 0, "POLL", 0)
        c1 = self.state(0, 1, "C1", 1)
        c1e = self.state(0, 5, "C1E-SNB", 2)
        boundary = self.state(1, 3, "C2", 3)
        c6 = self.state(1, 4, "C6", 100)
        disabled_c1 = self.state(1, 1, "C1", 1, 1)
        result = idle.configure(self.root)
        self.assertTrue(result["changed"])
        self.assertEqual([p.read_text().strip() for p in [poll, c1]], ["0", "0"])
        self.assertEqual([p.read_text().strip() for p in [c1e, boundary, c6, disabled_c1]], ["1"] * 4)
        self.assertFalse(idle.configure(self.root)["changed"])

    def test_check_mode_never_writes_and_reports_changes(self):
        path = self.state(0, 2, "C1E", 2)
        with patch.object(Path, "write_text", side_effect=AssertionError("unexpected write")):
            result = idle.configure(self.root, check_mode=True)
        self.assertTrue(result["changed"])
        self.assertEqual(result["states_to_change"], [str(path)])
        self.assertEqual(result["states_changed"], [])
        self.assertEqual(path.read_text().strip(), "0")

    def test_offline_cpu_is_skipped_and_cpu0_without_online_file_is_included(self):
        first = self.state(0, 2, "C1E", 2)
        offline = self.state(1, 2, "C1E", 2)
        (self.root / "cpu1/online").write_text("0\n")
        result = idle.configure(self.root)
        self.assertEqual(result["online_cpus"], 1)
        self.assertEqual(first.read_text().strip(), "1")
        self.assertEqual(offline.read_text().strip(), "0")

    def test_no_cpuidle_and_missing_c1e_are_explicit(self):
        (self.root / "cpu0").mkdir()
        c2 = self.state(1, 2, "C2", 10)
        result = idle.configure(self.root)
        self.assertEqual(result["cpus_without_cpuidle"], ["cpu0"])
        self.assertEqual(result["c1e_states"], 0)
        self.assertEqual(len(result["warnings"]), 2)
        self.assertEqual(c2.read_text().strip(), "1")
        json.dumps(result)

    def test_missing_cpu_sysfs_reports_unverified(self):
        result = idle.configure(self.root)
        self.assertFalse(result["changed"])
        self.assertIn("not verified", result["warnings"][0])

    def test_c1e_opt_out_preserves_latency_policy_and_existing_disables(self):
        c1e = self.state(0, 2, "C1E", 2)
        deep = self.state(0, 3, "C6", 100)
        existing = self.state(1, 2, "C1E", 2, 1)
        idle.configure(self.root, disable_c1e=False)
        self.assertEqual(c1e.read_text().strip(), "0")
        self.assertEqual(deep.read_text().strip(), "1")
        self.assertEqual(existing.read_text().strip(), "1")

    def test_malformed_snapshot_fails_before_any_write(self):
        good = self.state(0, 2, "C1E", 2)
        bad = self.state(1, 2, "C1E", 2)
        bad.write_text("invalid\n")
        with self.assertRaises(idle.IdleError) as ctx:
            idle.configure(self.root)
        self.assertIn(str(bad), str(ctx.exception))
        self.assertFalse(ctx.exception.result["changed"])
        self.assertEqual(good.read_text().strip(), "0")

    def test_missing_target_attribute_fails_before_any_write(self):
        good = self.state(0, 2, "C1E", 2)
        bad = self.state(1, 2, "C1E", 2)
        bad.unlink()
        with self.assertRaises(idle.IdleError):
            idle.configure(self.root)
        self.assertEqual(good.read_text().strip(), "0")

    def test_write_failure_is_not_reported_as_success(self):
        path = self.state(0, 2, "C1E", 2)
        with patch.object(Path, "write_text", side_effect=PermissionError("denied: " + str(path))):
            with self.assertRaises(idle.IdleError) as ctx:
                idle.configure(self.root)
        self.assertIn(str(path), str(ctx.exception))
        self.assertFalse(ctx.exception.result["changed"])

    def test_failed_readback_reports_partial_change(self):
        path = self.state(0, 2, "C1E", 2)
        with patch.object(Path, "write_text", return_value=2):
            with self.assertRaises(idle.IdleError) as ctx:
                idle.configure(self.root)
        self.assertIn(str(path), str(ctx.exception))
        self.assertTrue(ctx.exception.result["changed"])

    def test_negative_threshold_is_rejected(self):
        path = self.state(0, 2, "C1E", 2)
        with self.assertRaises(idle.IdleError):
            idle.configure(self.root, latency_threshold_us=-1)
        self.assertEqual(path.read_text().strip(), "0")

    @unittest.skipUnless(shutil.which("ansible-playbook"), "requires Ansible")
    def test_real_role_tag_check_apply_and_idempotence(self):
        # Copy the role and redirect only its sysfs constant to the fixture.
        # The real main task list tests that --tags cpu_idle skips all other tuning.
        path = self.state(0, 2, "C1E", 2)
        fixture_role = self.root / "roles/tune"
        shutil.copytree(REPO / "roles/tune", fixture_role)
        module = fixture_role / "library/dingofs_cpu_idle.py"
        source = module.read_text()
        self.assertIn('CPU_SYSFS_ROOT = "/sys/devices/system/cpu"', source)
        module.write_text(source.replace(
            'CPU_SYSFS_ROOT = "/sys/devices/system/cpu"',
            "CPU_SYSFS_ROOT = " + repr(str(self.root))))
        play = self.root / "play.yml"
        play.write_text("- hosts: localhost\n  gather_facts: false\n  become: false\n"
                        "  roles:\n    - tune\n")
        env = dict(os.environ, ANSIBLE_ROLES_PATH=str(self.root / "roles"),
                   ANSIBLE_STDOUT_CALLBACK="default")
        args = ["ansible-playbook", "-i", "localhost,", "-c", "local", str(play),
                "-e", "ansible_python_interpreter=" + sys.executable,
                "--tags", "cpu_idle"]
        for check, changed in [(True, 1), (False, 1), (False, 0), (True, 0)]:
            result = subprocess.run(args + (["--check"] if check else []), cwd=self.root,
                                    env=env, capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("changed=" + str(changed), result.stdout)
            if check and changed:
                self.assertEqual(path.read_text().strip(), "0")
        self.assertEqual(path.read_text().strip(), "1")


if __name__ == "__main__":
    unittest.main()
