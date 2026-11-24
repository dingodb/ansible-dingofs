"""Unit and real Ansible integration tests for client `enabled` and `dingo_client_only`."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dingo_client_filter", REPO / "filter_plugins/dingo_client.py")
client_filter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client_filter)


class ClientSelectionFilterTests(unittest.TestCase):
    def test_enabled_defaults_true_and_filters_disabled_for_deploy(self):
        clients = [
            {"fsname": "alayanew", "mount_point": "/mnt/alayanew"},
            {"fsname": "dingospeed-public", "mount_point": "/mnt/public-fs", "enabled": True},
            {"fsname": "manila", "mount_point": "/mnt/manila", "enabled": False},
            {"fsname": "str-off", "mount_point": "/mnt/off", "enabled": "false"},
            {"fsname": "str-on", "mount_point": "/mnt/on", "enabled": "yes"},
        ]
        res = client_filter.select_clients(clients, version_suffix="-v5.2")
        self.assertEqual([c["fsname"] for c in res["enabled"]], ["alayanew", "dingospeed-public", "str-on"])
        self.assertEqual([c["fsname"] for c in res["disabled"]], ["manila", "str-off"])
        self.assertEqual([c["fsname"] for c in res["selected"]], ["alayanew", "dingospeed-public", "str-on"])

    def test_invalid_enabled_and_malformed_only_fail_fast(self):
        for bad_enabled in [None, "flase", "maybe", 2, []]:
            with self.subTest(bad_enabled=bad_enabled):
                with self.assertRaises(client_filter.AnsibleFilterError):
                    client_filter.select_clients([{"fsname": "a", "mount_point": "/mnt/a", "enabled": bad_enabled}])

        for bad_only in [",", "a,,b", "a,", ",a", True, 123, ["a", ""], ["a", 1]]:
            with self.subTest(bad_only=bad_only):
                with self.assertRaises(client_filter.AnsibleFilterError):
                    client_filter.parse_only(bad_only)

        for empty_only in [None, "", "   ", []]:
            with self.subTest(empty_only=empty_only):
                self.assertEqual(client_filter.parse_only(empty_only), [])

    def test_matching_by_fsname_mount_point_and_service_name_disambiguates_duplicates(self):
        clients = [
            {"fsname": "shared", "mount_point": "/mnt/a", "service_name": "dingofs-mount-shared-a"},
            {"fsname": "shared", "mount_point": "/mnt/b"},
            {"fsname": "other", "mount_point": "/mnt/other"},
        ]
        # fsname matches both mounts of shared without duplicates
        res = client_filter.select_clients(clients, only="shared, shared", version_suffix="-v5.2")
        self.assertEqual([c["mount_point"] for c in res["selected"]], ["/mnt/a", "/mnt/b"])

        # /mount_point with trailing slash matches exact mount
        res = client_filter.select_clients(clients, only="/mnt/b/", version_suffix="-v5.2")
        self.assertEqual([c["mount_point"] for c in res["selected"]], ["/mnt/b"])

        # explicit service_name and computed service_name (+ .service suffix) both match
        res = client_filter.select_clients(clients, only="dingofs-mount-shared-a.service", version_suffix="-v5.2")
        self.assertEqual([c["mount_point"] for c in res["selected"]], ["/mnt/a"])

        res = client_filter.select_clients(clients, only="dingofs-mount-other-v5.2", version_suffix="-v5.2")
        self.assertEqual([c["fsname"] for c in res["selected"]], ["other"])

        # Overlapping selectors matching the same entry never duplicate it
        res = client_filter.select_clients(clients, only="other,/mnt/other", version_suffix="-v5.2")
        self.assertEqual([c["fsname"] for c in res["selected"]], ["other"])

    def test_validate_play_catches_typos_and_disabled_only_across_heterogeneous_hosts(self):
        hostvars = {
            "gpu32": {
                "dingo_clients": [
                    {"fsname": "alayanew", "mount_point": "/mnt/alayanew"},
                    {"fsname": "manila", "mount_point": "/mnt/manila", "enabled": False},
                ],
                "dingo_client_version_suffix": "-v5.2",
            },
            "cpu16": {
                "dingo_clients": [
                    {"fsname": "dingospeed-fs", "mount_point": "/dingofs/v5.2-dingospeed-fs"},
                    {"fsname": "dingospeed-public", "mount_point": "/dingofs/v5.2-dingospeed-public"},
                ],
                "dingo_client_version_suffix": "-v5.2",
            },
            "gpu01": {
                "dingo_clients": [
                    {"fsname": "alayanew", "mount_point": "/mnt/alayanew"},
                    {"fsname": "dingospeed-public", "mount_point": "/mnt/public-fs"},
                    {"fsname": "manila", "mount_point": "/mnt/manila", "enabled": False},
                ],
                "dingo_client_version_suffix": "-v5.2",
            },
        }
        hosts = ["gpu32", "cpu16", "gpu01"]

        # Valid heterogeneous filter
        plan = client_filter.validate_play(hosts, hostvars, only="dingospeed-fs,dingospeed-public")
        self.assertTrue(plan["valid"], plan["error"])
        self.assertEqual(plan["selected_count"], 3)

        # Typo fails even when another selector matches
        plan = client_filter.validate_play(hosts, hostvars, only="dingospeed-public,dingospeed-f")
        self.assertFalse(plan["valid"])
        self.assertEqual(plan["unmatched"], ["dingospeed-f"])

        # Disabled-only fails for deploy/upgrade/status
        plan = client_filter.validate_play(hosts, hostvars, only="manila", include_disabled=False)
        self.assertFalse(plan["valid"])
        self.assertEqual(plan["disabled_only"], ["manila"])
        self.assertIn("enabled: false", plan["error"])

        # Disabled entry IS valid for uninstall
        plan = client_filter.validate_play(hosts, hostvars, only="manila", include_disabled=True)
        self.assertTrue(plan["valid"], plan["error"])
        self.assertEqual(plan["selected_count"], 2)

        # Limiting to gpu32 alone with dingospeed-public fails (0 matches in play)
        plan = client_filter.validate_play(["gpu32"], hostvars, only="dingospeed-public")
        self.assertFalse(plan["valid"])
        self.assertEqual(plan["unmatched"], ["dingospeed-public"])


@unittest.skipUnless(shutil.which("ansible-playbook"), "requires Ansible")
class ClientSelectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dingo-client-selection-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.fakebin = self.root / "fakebin"
        self.fakebin.mkdir()

    def _base_env(self):
        return dict(
            os.environ,
            ANSIBLE_CONFIG=str(REPO / "ansible.cfg"),
            ANSIBLE_ROLES_PATH=str(REPO / "roles"),
            ANSIBLE_LIBRARY=str(self.library),
            ANSIBLE_FILTER_PLUGINS=str(REPO / "filter_plugins"),
            ANSIBLE_STDOUT_CALLBACK="default",
            DINGO_CLIENT_REPORT_DIR=str(self.root / "reports"),
            ANSIBLE_NOCOLOR="1",
        )

    def test_upgrade_filters_instances_preserves_restart_check_and_skips_unmatched_host_under_serial(self):
        events = self.root / "upgrade-events"
        (self.library / "systemd.py").write_text(
            "from ansible.module_utils.basic import AnsibleModule\n"
            "import os\n"
            "m = AnsibleModule(argument_spec=dict(name=dict(), state=dict()))\n"
            "with open(os.environ['UPGRADE_EVENTS'], 'a') as f:\n"
            "    f.write(m.params['name'] + '\\n')\n"
            "m.exit_json(changed=True)\n"
        )
        roles = self.root / "upgrade-roles"
        shutil.copytree(REPO / "roles/dingo_client_upgrade", roles / "dingo_client_upgrade")
        common_tasks = roles / "common/tasks"
        common_tasks.mkdir(parents=True)
        shutil.copy(REPO / "roles/common/tasks/client_report.yml", common_tasks / "client_report.yml")
        common_files = roles / "common/files"
        common_files.mkdir(parents=True)
        (common_files / "dingofs_mount_usage_check.sh").write_text("#!/bin/sh\nexit 0\n")
        (common_files / "dingofs_mount_usage_check.sh").chmod(0o755)
        checker = self.root / "usage-check.sh"
        mountpoint = self.fakebin / "mountpoint"
        mountpoint.write_text("#!/bin/sh\nexit 0\n")
        mountpoint.chmod(0o755)
        candidate = self.root / "candidate-bin"
        candidate.write_text("v5.2-955d5b0\n")

        def make_entry(host, fsname, enabled=True):
            base = self.root / host / fsname
            (base / "bin").mkdir(parents=True)
            (base / "bin/dingo-client").write_text("old-bin\n")
            entry = {
                "fsname": fsname,
                "base_dir": str(base),
                "mount_point": str(base / "mount"),
            }
            if not enabled:
                entry["enabled"] = False
            return entry

        # Simulate xn01 layout with serial: 1 where batch 1 (gpu32) has NO dingospeed-* mounts.
        inventory = {
            "all": {
                "vars": {
                    "ansible_connection": "local",
                    "ansible_become": False,
                    "ansible_python_interpreter": sys.executable,
                    "ansible_os_family": "None",
                    "dingo_client_version_suffix": "-v5.2",
                    "dingo_client_bin_local_src": str(candidate),
                    "dingo_client_mount_check_script": str(checker),
                    "dingo_client_upgrade_script_path": str(self.root / "{{ inventory_hostname }}-restart.sh"),
                    "dingo_clients": "{{ dingofs_host_vars.get(inventory_hostname, {}).get('dingo_clients', dingo_clients_default) }}",
                    "dingo_clients_default": [
                        make_entry("gpu01", "alayanew", enabled=True),
                        make_entry("gpu01", "dingospeed-public", enabled=True),
                        make_entry("gpu01", "manila", enabled=False),
                    ],
                    "dingofs_host_vars": {
                        "gpu32": {
                            "dingo_clients": [
                                make_entry("gpu32", "alayanew", enabled=True),
                                make_entry("gpu32", "manila", enabled=False),
                            ]
                        },
                        "cpu16": {
                            "dingo_clients": [
                                make_entry("cpu16", "dingospeed-fs", enabled=True),
                                make_entry("cpu16", "dingospeed-public", enabled=True),
                            ]
                        },
                    },
                },
                "children": {
                    "client_servers": {
                        "hosts": {"gpu32": {}, "cpu16": {}, "gpu01": {}}
                    }
                },
            }
        }
        inv_path = self.root / "inventory.yml"
        inv_path.write_text(yaml.safe_dump(inventory, sort_keys=False))

        # Keep inventory host order so batch 1 (gpu32) has 0 dingospeed-* entries.
        play_path = self.root / "upgrade-play.yml"
        play_path.write_text(yaml.safe_dump([{
            "hosts": "client_servers",
            "order": "inventory",
            "gather_facts": False,
            "serial": 1,
            "environment": {
                "PATH": str(self.fakebin) + ":" + os.environ["PATH"],
                "UPGRADE_EVENTS": str(events),
            },
            "roles": ["dingo_client_upgrade"],
        }], sort_keys=False))
        upgrade_env = dict(self._base_env(), ANSIBLE_ROLES_PATH=str(roles))

        # 1) Typo fails on batch 1 before modifying anything
        bad = subprocess.run(
            ["ansible-playbook", "-i", str(inv_path), str(play_path), "-e", "dingo_client_only=dingospeed-pubilc"],
            cwd=REPO, env=upgrade_env, capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(bad.returncode, 0, bad.stdout + bad.stderr)
        self.assertIn("dingospeed-pubilc", bad.stdout + bad.stderr)
        self.assertFalse(events.exists())
        # Batch 1 100% failure aborts play immediately (cpu16/gpu01 never even run)
        self.assertNotIn("cpu16", bad.stdout.split("PLAY RECAP")[-1])

        # 2) Targeting a disabled-only fsname fails before modifying anything
        disabled = subprocess.run(
            ["ansible-playbook", "-i", str(inv_path), str(play_path), "-e", "dingo_client_only=manila"],
            cwd=REPO, env=upgrade_env, capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(disabled.returncode, 0, disabled.stdout + disabled.stderr)
        self.assertIn("enabled: false", disabled.stdout + disabled.stderr)
        self.assertFalse(events.exists())

        # 3) Valid heterogeneous filter across serial=1 batches
        ok = subprocess.run(
            ["ansible-playbook", "-i", str(inv_path), str(play_path), "-e", "dingo_client_only=dingospeed-fs,dingospeed-public"],
            cwd=REPO, env=upgrade_env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn("SKIP gpu32", ok.stdout)

        # Only dingospeed-fs (cpu16) and dingospeed-public (cpu16 + gpu01) were upgraded
        self.assertEqual(
            events.read_text().splitlines(),
            ["dingofs-mount-dingospeed-fs-v5.2", "dingofs-mount-dingospeed-public-v5.2", "dingofs-mount-dingospeed-public-v5.2"],
        )
        self.assertEqual((self.root / "gpu32/alayanew/bin/dingo-client").read_text(), "old-bin\n")
        self.assertEqual((self.root / "gpu01/alayanew/bin/dingo-client").read_text(), "old-bin\n")
        self.assertEqual((self.root / "gpu01/manila/bin/dingo-client").read_text(), "old-bin\n")
        self.assertEqual((self.root / "gpu01/dingospeed-public/bin/dingo-client").read_text(), "v5.2-955d5b0\n")
        self.assertEqual((self.root / "cpu16/dingospeed-fs/bin/dingo-client").read_text(), "v5.2-955d5b0\n")
        self.assertEqual((self.root / "cpu16/dingospeed-public/bin/dingo-client").read_text(), "v5.2-955d5b0\n")

        # Skipped host gpu32 was not touched; gpu01 restart script preserves all enabled mounts and omits disabled manila
        self.assertFalse((self.root / "gpu32-restart.sh").exists())
        gpu01_script = (self.root / "gpu01-restart.sh").read_text()
        self.assertIn(str(self.root / "gpu01/alayanew/mount"), gpu01_script)
        self.assertIn(str(self.root / "gpu01/dingospeed-public/mount"), gpu01_script)
        self.assertNotIn(str(self.root / "gpu01/manila/mount"), gpu01_script)

    def test_status_playbook_honors_enabled_and_dingo_client_only(self):
        # Fake systemd: only dingospeed-public-v5.2 is active; alayanew and manila are inactive!
        (self.library / "systemd.py").write_text(
            "from ansible.module_utils.basic import AnsibleModule\n"
            "m = AnsibleModule(argument_spec=dict(name=dict()))\n"
            "active = 'active' if 'dingospeed-public' in m.params['name'] else 'inactive'\n"
            "m.exit_json(changed=False, status={'ActiveState': active})\n"
        )
        for name, body in {
            "mountpoint": '#!/bin/sh\necho "$*" | grep -q public-fs\n',
            "df": '#!/bin/sh\necho "DingoFS:dingospeed-public 100G 1G 99G 1% $2"\n',
            "findmnt": '#!/bin/sh\necho \'{"filesystems":[{"fstype":"fuse.dingofs","source":"DingoFS:dingospeed-public"}]}\'\n',
        }.items():
            p = self.fakebin / name
            p.write_text(body)
            p.chmod(0o755)

        inv = {
            "all": {
                "vars": {
                    "ansible_connection": "local",
                    "ansible_become": False,
                    "ansible_python_interpreter": sys.executable,
                    "dingo_client_version_suffix": "-v5.2",
                    "dingo_clients": "{{ dingofs_host_vars.get(inventory_hostname, {}).get('dingo_clients', dingo_clients_default) }}",
                    "dingo_clients_default": [
                        {"fsname": "alayanew", "mount_point": "/mnt/alayanew"},
                        {"fsname": "dingospeed-public", "mount_point": "/mnt/public-fs"},
                        {"fsname": "manila", "mount_point": "/mnt/manila", "enabled": False},
                    ],
                    "dingofs_host_vars": {
                        "gpu32": {
                            "dingo_clients": [
                                {"fsname": "alayanew", "mount_point": "/mnt/alayanew"},
                                {"fsname": "manila", "mount_point": "/mnt/manila", "enabled": False},
                            ]
                        }
                    },
                },
                "children": {"client_servers": {"hosts": {"gpu01": {}, "gpu32": {}}}},
            }
        }
        inv_path = self.root / "status-inv.yml"
        inv_path.write_text(yaml.safe_dump(inv))
        env = dict(self._base_env(), PATH=str(self.fakebin) + ":" + os.environ["PATH"])

        # Filtering to dingospeed-public passes on gpu01 (ignoring inactive alayanew/manila) and skips gpu32
        ok = subprocess.run(
            ["ansible-playbook", "-i", str(inv_path), str(REPO / "playbooks/client_99_status.yml"),
             "-e", "dingo_client_only=dingospeed-public"],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn("SKIP gpu32", ok.stdout)

        # Without dingo_client_only, enabled alayanew is checked (and fails because inactive)
        fail_active = subprocess.run(
            ["ansible-playbook", "-i", str(inv_path), str(REPO / "playbooks/client_99_status.yml"),
             "--limit", "gpu01"],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(fail_active.returncode, 0, fail_active.stdout + fail_active.stderr)
        self.assertIn("alayanew", fail_active.stdout)

        # Disabled manila is skipped when not requested, and rejected if explicitly requested
        fail_disabled = subprocess.run(
            ["ansible-playbook", "-i", str(inv_path), str(REPO / "playbooks/client_99_status.yml"),
             "-e", "dingo_client_only=manila"],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(fail_disabled.returncode, 0, fail_disabled.stdout + fail_disabled.stderr)
        self.assertIn("enabled: false", fail_disabled.stdout + fail_disabled.stderr)

    def test_deploy_role_skips_unmatched_host_and_rejects_disabled_or_typo_before_preflight(self):
        # Verify roles/dingo_client/tasks/main.yml selection runs before binaries/numactl/RDMA checks
        inv = {
            "all": {
                "vars": {
                    "ansible_connection": "local",
                    "ansible_become": False,
                    "ansible_python_interpreter": sys.executable,
                    "dingofs_skip_version_check": True,
                    "dingo_client_bin_local_src": str(self.root / "nonexistent-client-bin"),
                    "dingo_client_dingo_bin_local_src": str(self.root / "nonexistent-dingo-bin"),
                    "dingo_client_numactl_enabled": True,
                    "dingo_clients": [
                        {"fsname": "disabled-fs", "mount_point": "/mnt/disabled", "port": 12000, "enabled": False},
                    ],
                },
                "children": {"client_servers": {"hosts": {"host1": {}}}},
            }
        }
        inv_path = self.root / "deploy-inv.yml"
        inv_path.write_text(yaml.safe_dump(inv))
        play_path = self.root / "deploy-play.yml"
        play_path.write_text(yaml.safe_dump([{
            "hosts": "client_servers",
            "gather_facts": False,
            "roles": ["dingo_client"],
        }]))
        # All clients disabled -> clean SKIP, does not fail on missing binaries or numactl
        ok = subprocess.run(
            ["ansible-playbook", "-i", str(inv_path), str(play_path)],
            cwd=REPO, env=self._base_env(), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn("SKIP host1", ok.stdout)
        self.assertIn("disabled: disabled-fs", ok.stdout)

        # Explicitly asking for disabled-fs fails cleanly with enabled: false error
        fail_disabled = subprocess.run(
            ["ansible-playbook", "-i", str(inv_path), str(play_path), "-e", "dingo_client_only=disabled-fs"],
            cwd=REPO, env=self._base_env(), capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(fail_disabled.returncode, 0, fail_disabled.stdout + fail_disabled.stderr)
        self.assertIn("enabled: false", fail_disabled.stdout + fail_disabled.stderr)

        # Per-host disabled override: enabled on host_on, disabled on host_off
        mixed_inv = {
            "all": {
                "vars": {
                    "ansible_connection": "local",
                    "ansible_become": False,
                    "ansible_python_interpreter": sys.executable,
                    "dingofs_skip_version_check": True,
                    "dingo_client_version_suffix": "-v5.2",
                    "dingo_clients": "{{ dingofs_host_vars.get(inventory_hostname, {}).get('dingo_clients', dingo_clients_default) }}",
                    "dingo_clients_default": [
                        {"fsname": "shared", "mount_point": "/mnt/shared", "port": 12000, "enabled": True},
                    ],
                    "dingofs_host_vars": {
                        "host_off": {
                            "dingo_clients": [
                                {"fsname": "shared", "mount_point": "/mnt/shared", "port": 12000, "enabled": False},
                            ]
                        }
                    },
                },
                "children": {"client_servers": {"hosts": {"host_off": {}, "host_on": {}}}},
            }
        }
        mixed_inv_path = self.root / "mixed-status-inv.yml"
        mixed_inv_path.write_text(yaml.safe_dump(mixed_inv, sort_keys=False))
        (self.library / "systemd.py").write_text(
            "from ansible.module_utils.basic import AnsibleModule\n"
            "m = AnsibleModule(argument_spec=dict(name=dict()))\n"
            "m.exit_json(changed=False, status={'ActiveState': 'active'})\n"
        )
        for name, body in {
            "mountpoint": "#!/bin/sh\nexit 0\n",
            "df": "#!/bin/sh\necho ok\n",
            "findmnt": '#!/bin/sh\necho \'{"filesystems":[{"fstype":"fuse.dingofs","source":"DingoFS:shared"}]}\'\n',
        }.items():
            p = self.fakebin / name
            p.write_text(body)
            p.chmod(0o755)
        env = dict(self._base_env(), PATH=str(self.fakebin) + ":" + os.environ["PATH"])
        # Across both hosts: host_on passes, host_off logs disabled skip and skips cleanly
        both = subprocess.run(
            ["ansible-playbook", "-i", str(mixed_inv_path), str(REPO / "playbooks/client_99_status.yml"),
             "-e", "dingo_client_only=shared"],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(both.returncode, 0, both.stdout + both.stderr)
        self.assertIn("SKIP disabled dingo-client instance(s) on host_off (enabled: false): shared", both.stdout)
        # Limiting only to host_off fails loudly because shared is disabled on every host in the play
        only_off = subprocess.run(
            ["ansible-playbook", "-i", str(mixed_inv_path), str(REPO / "playbooks/client_99_status.yml"),
             "--limit", "host_off", "-e", "dingo_client_only=shared"],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(only_off.returncode, 0, only_off.stdout + only_off.stderr)
        self.assertIn("enabled: false", only_off.stdout + only_off.stderr)

    def test_uninstall_allows_disabled_entries_and_filters_by_only_and_mount_point(self):
        units = self.root / "units"
        units.mkdir()
        state_dir = self.root / "state"
        proc_dir = self.root / "proc"
        proc_dir.mkdir()
        mounts = self.root / "mountinfo"
        mounts.write_text("31 1 8:1 / / rw - ext4 /dev/fixture rw\n")

        roles = self.root / "roles"
        role = roles / "dingo_uninstall"
        shutil.copytree(REPO / "roles/dingo_uninstall", role)
        plan_mod = role / "library/dingofs_uninstall_plan.py"
        plan_mod.write_text(
            plan_mod.read_text()
            .replace("UNIT_DIR = Path('/etc/systemd/system')", "UNIT_DIR = Path(" + repr(str(units)) + ")")
            .replace("MOUNTINFO = Path('/proc/self/mountinfo')", "MOUNTINFO = Path(" + repr(str(mounts)) + ")")
        )
        state_mod = role / "library/dingofs_mount_state.py"
        state_mod.write_text(
            state_mod.read_text()
            .replace("MOUNTINFO = Path('/proc/self/mountinfo')", "MOUNTINFO = Path(" + repr(str(mounts)) + ")")
        )
        remove_yml = role / "tasks/remove.yml"
        remove_yml.write_text(remove_yml.read_text().replace("pathlib.Path('/proc')", "pathlib.Path(" + repr(str(proc_dir)) + ")"))
        checker = roles / "common/files/dingofs_mount_usage_check.sh"
        checker.parent.mkdir(parents=True)
        checker.write_text("#!/bin/sh\nexit 0\n")
        checker.chmod(0o755)

        (self.library / "systemd.py").write_text(
            "from ansible.module_utils.basic import AnsibleModule\n"
            "m = AnsibleModule(argument_spec=dict(name=dict(), state=dict(), enabled=dict(type='bool'), daemon_reload=dict(type='bool')), supports_check_mode=True)\n"
            "m.exit_json(changed=not m.check_mode)\n"
        )
        systemctl = self.fakebin / "systemctl"
        systemctl.write_text(
            "#!" + sys.executable + "\nimport pathlib, sys\n"
            "p = pathlib.Path(" + repr(str(units)) + ") / (sys.argv[2] if sys.argv[2].endswith('.service') else sys.argv[2] + '.service')\n"
            "print('LoadState=' + ('loaded' if p.exists() else 'not-found'))\n"
            "print('ActiveState=inactive')\n"
            "print('FragmentPath=' + (str(p) if p.exists() else ''))\n"
        )
        systemctl.chmod(0o755)

        def make_installed(fsname, suffix, enabled=True):
            base = self.root / ("inst-" + suffix)
            mnt = self.root / ("mnt-" + suffix)
            log = self.root / ("log-" + suffix)
            base.mkdir()
            mnt.mkdir()
            log.mkdir()
            svc = "dingofs-mount-" + suffix
            unit = units / (svc + ".service")
            unit.write_text("ExecStart=" + str(base / "dingofs-mount.sh") + "\n")
            (base / "dingofs-mount.sh").write_text(
                'g_mnt="' + str(mnt) + '"\ng_fsname="' + fsname + '"\ng_log="' + str(log) + '"\ng_mdsaddr="127.0.0.1:7000"\n'
            )
            entry = {"fsname": fsname, "mount_point": str(mnt), "base_dir": str(base), "service_name": svc}
            if not enabled:
                entry["enabled"] = False
            return entry, unit, base, mnt

        e_keep, u_keep, b_keep, m_keep = make_installed("alayanew", "alayanew", enabled=True)
        e_dis, u_dis, b_dis, m_dis = make_installed("manila", "manila", enabled=False)
        e_dup_a, u_dup_a, b_dup_a, m_dup_a = make_installed("same", "same-a", enabled=True)
        e_dup_b, u_dup_b, b_dup_b, m_dup_b = make_installed("same", "same-b", enabled=True)

        play = self.root / "uninstall-play.yml"
        play.write_text(yaml.safe_dump([{
            "hosts": "all",
            "gather_facts": False,
            "become": False,
            "vars": {
                "ansible_connection": "local",
                "ansible_become": False,
                "ansible_python_interpreter": sys.executable,
                "dingo_uninstall_kind": "client",
                "dingo_uninstall_state_dir": str(state_dir),
                "dingo_client_mds_addrs": "127.0.0.1:7000",
                "dingo_clients": [e_keep, e_dis, e_dup_a, e_dup_b],
            },
            "environment": {"PATH": str(self.fakebin) + ":" + os.environ["PATH"]},
            "roles": ["dingo_uninstall"],
        }]))

        env = dict(self._base_env(), ANSIBLE_ROLES_PATH=str(roles))

        # 1) Typo fails and leaves all 4 installed
        typo = subprocess.run(
            ["ansible-playbook", "-i", "fixture,", str(play), "--limit", "fixture", "-e", "dingo_client_only=manlia"],
            cwd=self.root, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(typo.returncode, 0, typo.stdout + typo.stderr)
        self.assertTrue(u_dis.exists() and u_keep.exists())

        # 2) Uninstall disabled manila via dingo_client_only=manila -> only manila removed
        rm_dis = subprocess.run(
            ["ansible-playbook", "-i", "fixture,", str(play), "--limit", "fixture", "-e", "dingo_client_only=manila"],
            cwd=self.root, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(rm_dis.returncode, 0, rm_dis.stdout + rm_dis.stderr)
        self.assertFalse(u_dis.exists() or b_dis.exists() or m_dis.exists())
        self.assertTrue(u_keep.exists() and u_dup_a.exists() and u_dup_b.exists())

        # 3) Disambiguate duplicate fsname by /mount_point/ -> only same-b removed, same-a preserved
        rm_b = subprocess.run(
            ["ansible-playbook", "-i", "fixture,", str(play), "--limit", "fixture",
             "-e", "dingo_client_only=" + str(m_dup_b) + "/"],
            cwd=self.root, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(rm_b.returncode, 0, rm_b.stdout + rm_b.stderr)
        self.assertFalse(u_dup_b.exists() or b_dup_b.exists() or m_dup_b.exists())
        self.assertTrue(u_keep.exists() and u_dup_a.exists())


if __name__ == "__main__":
    unittest.main()
