"""Checks cannot mutate hosts; regression evidence remains actionable."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("check_performance", SCRIPTS / "check_performance.py")
perf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(perf)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def write(self, name, value):
        path = self.root / name.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value))

    def state(self, cpu, index, name, disabled):
        base = "/sys/devices/system/cpu/cpu{}/cpuidle/state{}".format(cpu, index)
        self.write(base + "/name", name)
        self.write(base + "/disable", disabled)
        return base + "/disable"

    def engine(self, policy=None):
        return perf.Performance(policy, root=str(self.root))

    def check(self, engine, key):
        return next(c for c in engine.checks if c["id"] == key)

    def test_partial_idle_configuration_catches_all_online_cpus(self):
        for cpu in range(2):
            self.state(cpu, 1, "C1", 0)
            self.state(cpu, 2, "C1E", 0 if cpu else 1)
            self.state(cpu, 3, "C6", 1)
            self.state(cpu, 7, "C6P", 0)
        e = self.engine()
        e.cpu()
        self.assertEqual(self.check(e, "cpu.idle.C1E")["current"]["mismatches"], 1)
        self.assertEqual(self.check(e, "cpu.idle.C6")["status"], "PASS")
        self.assertEqual(self.check(e, "cpu.idle.C6P")["status"], "FAIL")

    def test_amd_c2_reported_without_intel_assumptions(self):
        self.state(0, 2, "C2", 0)
        e = self.engine()
        e.cpu()
        self.assertEqual(self.check(e, "cpu.idle.C2")["status"], "FAIL")

    def test_cpu_zero_does_not_hide_other_governor(self):
        for cpu in range(2):
            self.state(cpu, 1, "C1", 0)
            self.write("/sys/devices/system/cpu/cpu{}/cpufreq/scaling_governor".format(cpu),
                       "performance" if cpu == 0 else "powersave")
        e = self.engine()
        e.cpu()
        self.assertEqual(self.check(e, "cpu.governor")["current"]["mismatches"], 1)

    def test_full_collection_is_read_only_including_gpu_and_services(self):
        path = self.state(0, 2, "C1E", 0)
        calls = []

        def query(argv):
            calls.append(argv)
            if argv[0] == "systemctl":
                self.assertEqual(argv[1], "is-active")
                return 0, "active", ""
            self.assertEqual(argv[0], "nvidia-smi")
            self.assertTrue("-q" in argv or any(a.startswith("--query-gpu=") for a in argv))
            if "-q" in argv:
                return 0, "<nvidia_smi_log/>", ""
            return 0, "GPU-1, NVIDIA B200, Disabled, 700, 1000, 400, 1000", ""

        e = self.engine({"gpu_power_limits": {"GPU-1": 900}})
        with patch.object(perf, "run", side_effect=query), \
                patch.object(perf.shutil, "which", return_value="nvidia-smi"), \
                patch.object(Path, "write_text", side_effect=AssertionError("host write")):
            report = e.collect()
        self.assertTrue(report["read_only"])
        self.assertEqual(e.read(path), "0")
        self.assertEqual(self.check(e, "gpu.power.GPU-1")["current"]["watts"], 700)
        self.assertEqual(self.check(e, "gpu.persistence.GPU-1")["status"], "FAIL")
        self.assertTrue(calls)

    def test_apply_option_does_not_exist(self):
        with patch.object(sys, "argv", ["check_performance", "--apply"]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            perf.main()
        self.assertEqual(ctx.exception.code, 2)

    def test_missing_interfaces_are_serializable_unknown(self):
        self.write("/sys/devices/system/cpu/cpu0/online", "1")
        e = self.engine()
        e.cpu()
        json.dumps(e.checks)
        self.assertEqual(self.check(e, "cpu.idle.coverage")["status"], "UNKNOWN")

    def test_active_tuned_is_diagnostic_and_no_custom_service_required(self):
        e = self.engine()
        with patch.object(perf, "run", return_value=(0, "active", "")):
            e.services()
        self.assertEqual(self.check(e, "service.policy_owners")["status"], "INFO")
        self.assertFalse(any(c["status"] == "FAIL" for c in e.checks))
        self.assertFalse(any("dingofs-performance" in c["id"] for c in e.checks))

    def test_thp_is_a_workload_choice(self):
        self.write("/sys/kernel/mm/transparent_hugepage/enabled", "[always] madvise never")
        e = self.engine()
        e.memory()
        self.assertFalse(self.check(e, "memory.thp")["required"])

    def test_offline_cpu_ignored(self):
        self.state(1, 2, "C1E", 0)
        self.write("/sys/devices/system/cpu/cpu1/online", "0")
        e = self.engine()
        e.cpu()
        self.assertFalse(any(c["id"] == "cpu.idle.C1E" for c in e.checks))

    def test_sysctl_vectors_normalize_whitespace(self):
        path = "/proc/sys/net/ipv4/tcp_wmem"
        self.write(path, "4096\t16384\t4194304")
        e = self.engine()
        e.settings("tcp", [path], "4096 16384 4194304")
        self.assertEqual(self.check(e, "tcp")["status"], "PASS")

    def test_required_unknown_has_distinct_exit_status(self):
        for status, required, code in [("PASS", True, 0), ("FAIL", True, 1),
                                       ("UNKNOWN", True, 2), ("UNKNOWN", False, 0)]:
            self.assertEqual(perf.exit_code({"checks": [{"status": status, "required": required}]}), code)

    def test_report_translates_c1e_counts_and_does_not_demand_our_service(self):
        self.state(0, 2, "C1E", 0)
        e = self.engine()
        e.cpu()
        report = {"host": "canary", "kernel": "test", "timestamp": "now", "checks": e.checks}
        md = perf.render_markdown([report])
        self.assertIn("基础设施处理清单", md)
        self.assertIn("开启 1 个 / 禁用 0 个；未达标 1 个", md)
        self.assertNotIn("dingofs-performance.service", md)
        self.assertIn("全部在线 CPU 禁用", md)

    def test_report_escapes_untrusted_markdown(self):
        md = perf.render_markdown([{"host": "x|<script>", "collection_error": "bad|<img>"}])
        self.assertIn("x\\|&lt;script&gt;", md)
        self.assertIn("采集失败，不能判断", md)

    def test_compare_rejects_other_host(self):
        with self.assertRaises(ValueError):
            perf.compare_reports({"host": "a"}, {"host": "b"})


if __name__ == "__main__":
    unittest.main()
