"""Render real Cache configuration and exercise its RDMA preflight locally."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("ansible-playbook"), "requires Ansible")
class CacheRdmaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ansible-cache-rdma-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.play_dir = self.root / "playbooks"
        self.play_dir.mkdir()
        (self.root / "VERSION").write_text("v5.2\n")
        self.env = dict(os.environ, ANSIBLE_CONFIG=str(REPO / "ansible.cfg"),
                        ANSIBLE_ROLES_PATH=str(REPO / "roles"))

    def render(self, settings=None, inventory_defaults=False, full_role=False):
        output = self.root / "dingo-cache.conf"
        output.unlink(missing_ok=True)
        variables = {
            "ansible_connection": "local", "ansible_become": False,
            "ansible_python_interpreter": sys.executable,
            "dingofs_site_version": "v5.2",
            "dingo_cache_listen_ip": "127.0.0.1",
            "dingo_cache_config_id": "fixture-id",
        }
        variables.update(settings or {})
        role = {"name": "dingo_cache"}
        if not full_role:
            role["tasks_from"] = "validate_rdma"
        tasks = [{"ansible.builtin.import_role": role}]
        if not full_role:
            tasks.append({"name": "Render the real Cache configuration",
                          "ansible.builtin.template": {
                              "src": str(REPO / "roles/dingo_cache/templates/dingo-cache.conf.j2"),
                              "dest": str(output),
                          }})
        play = {"hosts": "localhost", "gather_facts": False, "become": False,
                "vars": variables, "tasks": tasks}
        if inventory_defaults:
            play["vars_files"] = [str(REPO / "inventory/group_vars/all.yml")]
        path = self.play_dir / "test.yml"
        path.write_text(yaml.safe_dump([play], sort_keys=False))
        # Override the example inventory's remote Python path as well.
        args = ["ansible-playbook", "-i", "localhost,", str(path),
                "-e", "ansible_python_interpreter=" + sys.executable]
        if full_role:
            args.append("--check")
        result = subprocess.run(args, cwd=REPO, env=self.env, text=True,
                                capture_output=True, timeout=45)
        return result, output.read_text() if output.exists() else None

    def assert_flags(self, content, connections, heartbeat, idle):
        for name, value in [("rdma_max_connections", connections),
                            ("rdma_heartbeat_interval_s", heartbeat),
                            ("rdma_idle_timeout_s", idle)]:
            self.assertEqual([line for line in content.splitlines()
                              if line.startswith("--" + name + "=")],
                             ["--" + name + "=" + str(value)])

    def test_defaults_render_the_requested_cache_settings(self):
        result, content = self.render()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--use_rdma=true\n", content)
        self.assert_flags(content, 256, 2, 10)

    def test_inventory_values_can_override_defaults_including_numeric_strings(self):
        result, content = self.render({
            "dingo_cache_rdma_max_connections": "128",
            "dingo_cache_rdma_heartbeat_interval_s": "4",
            "dingo_cache_rdma_idle_timeout_s": "12",
        })
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_flags(content, 128, 4, 12)

    def test_example_inventory_also_renders_256_and_10(self):
        result, content = self.render(inventory_defaults=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_flags(content, 256, 2, 10)

    def test_rdma_off_omits_its_flags_and_does_not_validate_unused_values(self):
        result, content = self.render({
            "dingo_cache_use_rdma": False,
            "dingo_cache_rdma_max_connections": 0,
            "dingo_cache_rdma_heartbeat_interval_s": "unused",
            "dingo_cache_rdma_idle_timeout_s": 0,
        })
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--use_rdma=false\n", content)
        for prefix in ["--rdma_", "--cache_rdma_", "--buffer_pool_mb="]:
            self.assertFalse(any(line.startswith(prefix) for line in content.splitlines()), content)

    def test_invalid_parameters_fail_before_rendering_configuration(self):
        invalid = [
            {"dingo_cache_rdma_max_connections": 0},
            {"dingo_cache_rdma_max_connections": -1},
            {"dingo_cache_rdma_max_connections": 1.5},
            {"dingo_cache_rdma_max_connections": 4294967296},
            {"dingo_cache_rdma_heartbeat_interval_s": 0},
            {"dingo_cache_rdma_idle_timeout_s": "invalid"},
            {"dingo_cache_rdma_heartbeat_interval_s": 4,
             "dingo_cache_rdma_idle_timeout_s": 10},
        ]
        for settings in invalid:
            with self.subTest(settings=settings):
                result, content = self.render(settings)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(content)
                self.assertIn("Cache RDMA", result.stdout)

    def test_full_role_checks_timeout_before_host_probes_even_in_check_mode(self):
        result, content = self.render({"dingo_cache_rdma_idle_timeout_s": 5}, full_role=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("at least 3 times", result.stdout)
        self.assertNotIn("TASK [dingo_cache : Detect listen IP", result.stdout)
        self.assertIsNone(content)


if __name__ == "__main__":
    unittest.main()
