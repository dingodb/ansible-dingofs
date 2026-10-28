#!/usr/bin/env python3
"""只读的 Linux CPU/GPU 性能检查（仅用 Python 标准库）。

把本文件拷到目标机直接运行，不需要安装依赖，也不会修改任何配置：

    scp check_performance.py root@GPU-HOST:/tmp/
    ssh root@GPU-HOST 'python3 /tmp/check_performance.py --output /tmp/performance-check.md'

常用选项：
    --format markdown|text|json   报告格式（默认 markdown，中文）
    --output PATH                 把报告写入指定文件（默认只打印到 stdout）
    --compare BEFORE.json         与同一台机器的上次 JSON 快照对比
    --config expected.json        自定义期望值（只用于比较，不会写入主机）

前后对比：
    python3 check_performance.py --format json --output /tmp/before.json
    # 基础设施改完后，在同一台机器上
    python3 check_performance.py --compare /tmp/before.json --output /tmp/after.md

退出码：0 = 必需项全部通过；1 = 有必需项不达标；2 = 必需证据不足。
只读：不调优、不安装服务、不改 sysfs/sysctl/BIOS、不重启。缺少权限或接口的
项目记为 UNKNOWN（需核实），不会当成达标。
"""
import argparse
from collections import Counter, defaultdict
import csv
import datetime
import glob
import html
import io
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET


DEFAULT_POLICY = {
    "idle_allow": ["POLL", "C1", "C1-HSW", "C1-SKX", "C1_ACPI"],
    "governor": "performance",
    "epp": "performance",
    "epb": "0",
    "boost": True,
    "check_cpu_max": True,
    "thp": "madvise",
    "thp_defrag": "madvise",
    "platform_profile": "performance",
    "gpu_required": False,
    "gpu_persistence": True,
    # Expected GPU watts only; values are never applied.
    "gpu_power_limits": {},
    "sysctl": {},
    "policy_services": ["tuned", "power-profiles-daemon", "auto-cpufreq", "cpufrequtils"],
}


def selected(value):
    match = re.search(r"\[([^]]+)\]", value)
    return match.group(1) if match else " ".join(value.split())


def run(argv):
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=25)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)


class Performance:
    def __init__(self, policy=None, root="/"):
        self.policy = dict(DEFAULT_POLICY)
        self.policy.update(policy or {})
        self.root = Path(root)
        self.checks = []

    def path(self, path):
        return self.root / path.lstrip("/")

    def read(self, path):
        try:
            return self.path(path).read_text().strip()
        except OSError:
            return None

    def paths(self, pattern):
        return ["/" + str(Path(p).relative_to(self.root))
                for p in sorted(glob.glob(str(self.path(pattern))))]

    def add(self, key, status, current, expected, hint="", required=True):
        self.checks.append(dict(id=key, status=status, current=current,
                                expected=expected, hint=hint, required=required))

    def settings(self, key, paths, want, hint="", required=True):
        """Read every path and aggregate differences without writing anything."""
        if not paths:
            self.add(key, "UNKNOWN", "interface not exposed",
                     "per-path hardware maximum" if callable(want) else want, hint, required)
            return
        values, targets, drift, unknown = Counter(), Counter(), [], []
        for path in paths:
            expected = selected(str(want(path) if callable(want) else want))
            targets[expected] += 1
            value = self.read(path)
            if value is None:
                unknown.append(path)
                continue
            values[selected(value or "unreadable")] += 1
            if value is None or selected(value) != expected:
                drift.append(path)
        status = "FAIL" if drift else "UNKNOWN" if unknown else "PASS"
        current = {"values": dict(values), "mismatches": len(drift),
                   "targets": dict(targets), "unreadable": len(unknown), "examples": (drift + unknown)[:4]}
        self.add(key, status, current, "per-path hardware maximum" if callable(want) else str(want),
                 hint, required)

    def cpu(self):
        online = []
        for cpu in self.paths("/sys/devices/system/cpu/cpu[0-9]*"):
            if self.read(cpu + "/online") != "0":
                online.append(cpu)
        states = defaultdict(list)
        missing_idle, missing_freq = [], []
        for cpu in online:
            idle = self.paths(cpu + "/cpuidle/state*")
            if not idle:
                missing_idle.append(cpu)
            for state in idle:
                states[self.read(state + "/name") or "UNREADABLE"].append(state + "/disable")
            if not self.path(cpu + "/cpufreq/scaling_governor").exists():
                missing_freq.append(cpu)
        self.add("cpu.inventory", "INFO", {"online": len(online), "idle_driver":
                 self.read("/sys/devices/system/cpu/cpuidle/current_driver"), "cmdline":
                 self.read("/proc/cmdline"), "frequency_drivers": sorted({
                     self.read(c + "/cpufreq/scaling_driver") or "unavailable" for c in online}),
                 "system_product": self.read("/sys/class/dmi/id/product_name"),
                 "bios_version": self.read("/sys/class/dmi/id/bios_version"),
                 "model_name": next((line.split(":", 1)[1].strip() for line in
                     (self.read("/proc/cpuinfo") or "").splitlines() if line.startswith("model name")), None),
                 "numa_cpus": {p: self.read(p) for p in self.paths("/sys/devices/system/node/node*/cpulist")}},
                 "inventory", required=False)
        if missing_idle or not online:
            self.add("cpu.idle.coverage", "UNKNOWN", len(missing_idle),
                     "idle state evidence on all online CPUs",
                     "BIOS/VM/driver may hide states; absence does not prove firmware C1E/C6 disabled.")
        for name, paths in sorted(states.items()):
            if name in self.policy["idle_allow"]:
                # Preserve any intentional disabling of a shallow state.
                self.add("cpu.idle." + name, "INFO", dict(Counter(self.read(p) for p in paths)),
                         "shallow state may remain enabled", required=False)
            elif name == "UNREADABLE":
                self.add("cpu.idle.unnamed", "UNKNOWN", len(paths), "readable state names")
            else:
                self.settings("cpu.idle." + name, paths, "1",
                              "请基础设施同事核查并关闭所有在线 CPU 的该状态；编号随 CPU/内核变化。")
        if missing_freq or not online:
            self.add("cpu.frequency.coverage", "UNKNOWN", len(missing_freq),
                     "frequency controls on all online CPUs", "Check BIOS P-state/CPPC policy or hypervisor.")
        policies = self.paths("/sys/devices/system/cpu/cpufreq/policy*")
        if not policies:
            policies = [c + "/cpufreq" for c in online if self.path(c + "/cpufreq").exists()]
        for leaf, key, value in [("scaling_governor", "governor", self.policy["governor"]),
                                  ("energy_performance_preference", "epp", self.policy["epp"])]:
            paths = [p + "/" + leaf for p in policies if self.path(p + "/" + leaf).exists()]
            self.settings("cpu." + key, paths, value, "Interface depends on CPU driver.",
                          required=key == "governor" or bool(paths))
        epb = [c + "/power/energy_perf_bias" for c in online
               if self.path(c + "/power/energy_perf_bias").exists()]
        self.settings("cpu.epb", epb, self.policy["epb"], required=bool(epb))
        if self.policy["check_cpu_max"]:
            paths = [p + "/scaling_max_freq" for p in policies
                     if self.read(p + "/cpuinfo_max_freq") is not None]
            self.settings("cpu.max_frequency", paths,
                          lambda p: self.read(str(Path(p).with_name("cpuinfo_max_freq"))),
                          "核查是否有软件频率上限；此项通过不代表满载时能维持最高睿频。")
        if self.policy["boost"]:
            turbo = "/sys/devices/system/cpu/intel_pstate/no_turbo"
            boost = self.paths("/sys/devices/system/cpu/cpufreq/boost")
            boost += self.paths("/sys/devices/system/cpu/cpufreq/policy*/boost")
            if self.path(turbo).exists():
                self.settings("cpu.turbo", [turbo], "0")
            elif boost:
                self.settings("cpu.turbo", boost, "1")
            else:
                self.add("cpu.turbo", "UNKNOWN", "no OS control", "BIOS boost enabled",
                         "Verify Turbo Boost/Core Performance Boost in firmware.", required=False)
        profile = "/sys/firmware/acpi/platform_profile"
        choices = self.read("/sys/firmware/acpi/platform_profile_choices") or ""
        if self.policy["platform_profile"] in choices.split():
            self.settings("platform.profile", [profile], self.policy["platform_profile"])
        else:
            self.add("platform.profile", "UNKNOWN", self.read(profile), self.policy["platform_profile"],
                     "Verify vendor BIOS/BMC maximum-performance profile.", required=False)

    def memory(self):
        for key, leaf in [("thp", "enabled"), ("thp_defrag", "defrag")]:
            paths = self.paths("/sys/kernel/mm/transparent_hugepage/" + leaf)
            self.settings("memory." + key, paths, self.policy[key], required=False)
        for key, value in sorted(self.policy["sysctl"].items()):
            if not re.fullmatch(r"[a-zA-Z0-9_.]+", key):
                raise ValueError("invalid sysctl key: " + key)
            self.settings("sysctl." + key, self.paths("/proc/sys/" + key.replace(".", "/")), value)
        for key, path in [("numa_balancing", "/proc/sys/kernel/numa_balancing"),
                          ("swappiness", "/proc/sys/vm/swappiness"),
                          ("hugepages", "/proc/sys/vm/nr_hugepages")]:
            self.add("memory." + key, "INFO", self.read(path), "workload-specific; see tuning plan",
                     required=False)

    def services(self):
        states = {}
        for service in self.policy["policy_services"]:
            rc, state, err = run(["systemctl", "is-active", service + ".service"])
            states[service] = state or err
        self.add("service.policy_owners", "INFO", states,
                 "由基础设施维护统一的性能策略；服务启停状态本身不判为不达标", required=False)
        self.add("persistence.verification", "UNKNOWN", "当前仅验证运行态",
                 "重启及内核升级后仍保持期望值",
                 "请基础设施提供持久化方案，并在维护窗口重启/升级后复检。", required=False)

    def gpu(self):
        required = self.policy["gpu_required"] or bool(self.policy["gpu_power_limits"])
        detected = any(self.read(p + "/vendor") == "0x10de" and
                       (self.read(p + "/class") or "").startswith("0x03")
                       for p in self.paths("/sys/bus/pci/devices/*"))
        if not shutil.which("nvidia-smi"):
            self.add("gpu.inventory", "UNKNOWN" if required or detected else "SKIP", "nvidia-smi absent",
                     "working NVIDIA driver/tooling" if required or detected else "no NVIDIA GPU detected",
                     required=required or detected)
            return
        fields = "uuid,name,persistence_mode,power.limit,power.default_limit,power.min_limit,power.max_limit"
        rc, out, err = run(["nvidia-smi", "--query-gpu=" + fields, "--format=csv,noheader,nounits"])
        if rc or not out:
            self.add("gpu.inventory", "UNKNOWN", err or out, "query succeeds", required=required or detected)
            return
        seen = set()
        for row in csv.reader(io.StringIO(out)):
            if len(row) != 7:
                self.add("gpu.inventory", "UNKNOWN", row, "7 query fields", required=required or detected)
                continue
            uuid, name, persistence, power, default, minimum, maximum = [v.strip() for v in row]
            seen.add(uuid)
            if not name.startswith("NVIDIA "):
                self.add("gpu." + uuid, "SKIP", name, "NVIDIA policy only; vendor tool required",
                         required=required)
                if required:
                    self.add("gpu.vendor." + uuid, "UNKNOWN", name, "NVIDIA GPU")
                continue
            if self.policy["gpu_persistence"]:
                self.add("gpu.persistence." + uuid,
                         "PASS" if persistence == "Enabled" else "FAIL" if persistence == "Disabled" else "UNKNOWN",
                         persistence, "Enabled", "请基础设施核实 GPU persistence 配置及重启后是否保留。")
            try:
                actual, nominal, low, high = map(float, [power, default, minimum, maximum])
                target = float(self.policy["gpu_power_limits"].get(uuid, nominal))
                explicit = uuid in self.policy["gpu_power_limits"]
                if not 0 < low <= target <= high:
                    raise ValueError("target outside device range")
                ok = abs(actual - target) <= 0.5 if explicit else actual >= target - 0.5
                self.add("gpu.power." + uuid, "PASS" if ok else "FAIL",
                         {"name": name, "watts": actual, "default": nominal, "max": high},
                         str(target) + (" W" if explicit else " W or higher (board default)"),
                         "请基础设施核对板卡标称功率和 BMC/机箱功率限制；不按型号写死瓦数。")
            except ValueError as exc:
                self.add("gpu.power." + uuid, "UNKNOWN", [power, default, minimum, maximum],
                         "supported power limits", str(exc), required=True)
        for uuid in set(self.policy["gpu_power_limits"]) - seen:
            self.add("gpu.power.missing." + uuid, "FAIL", "UUID not found", "configured GPU present")
        # Diagnostic only: idle P8/low PCIe speed is normal; never demand idle P0.
        rc, out, err = run(["nvidia-smi", "-q", "-x"])
        if rc == 0:
            try:
                for gpu in ET.fromstring(out).findall("gpu"):
                    uuid = gpu.findtext("uuid") or gpu.get("id", "unknown")
                    reasons = []
                    for tag in ("clocks_event_reasons", "clocks_throttle_reasons"):
                        for item in gpu.findall(tag + "/*"):
                            if item.text == "Active" and not item.tag.endswith("gpu_idle"):
                                reasons.append(item.tag)
                    self.add("gpu.telemetry." + uuid, "WARN" if reasons else "INFO",
                             {"pstate": gpu.findtext("performance_state"), "active_limits": reasons,
                              "temperature": gpu.findtext("temperature/gpu_temp"),
                              "clocks": {e.tag: e.text for e in gpu.findall("clocks/*")},
                              "pcie": {e.tag: e.text for e in gpu.findall("pci/pci_gpu_link_info/pcie_gen/*")}},
                             "inspect power/thermal/clock limits under representative load", required=False)
            except ET.ParseError:
                self.add("gpu.telemetry", "UNKNOWN", "invalid XML", "nvidia-smi -q -x", required=False)
        else:
            self.add("gpu.telemetry", "UNKNOWN", err, "nvidia-smi -q -x", required=False)

    def diagnostics(self):
        self.add("firmware.cstates", "UNKNOWN", "not readable through generic Linux sysfs",
                 "BIOS C1E/Enhanced Halt off; deep/package C-states off; maximum performance profile",
                 "OS state disable proves OS policy only. Export vendor Redfish/BIOS config and verify residency under load.",
                 required=False)
        self.add("pcie.aspm", "INFO", self.read("/sys/module/pcie_aspm/parameters/policy"),
                 "validate with PCIe topology and load; see plan", required=False)
        for interface in self.paths("/sys/class/infiniband/*/ports/*"):
            self.add("rdma." + interface.split("/infiniband/")[-1], "INFO",
                     {k: self.read(interface + "/" + k) for k in ("state", "phys_state", "rate", "link_layer")},
                     "Active and rated link speed for this fabric", required=False)
        for device in self.paths("/sys/class/nvme/nvme*"):
            self.add("nvme." + Path(device).name, "INFO", self.read(device + "/device/power/control"),
                     "assess runtime PM/APST and I/O latency per device", required=False)

    def collect(self):
        self.cpu()
        self.memory()
        self.services()
        self.gpu()
        self.diagnostics()
        return {"schema_version": 2, "host": platform.node(), "kernel": platform.release(),
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "mode": "check", "read_only": True, "policy": self.policy,
                "summary": dict(Counter(c["status"] for c in self.checks)),
                "checks": self.checks}


def exit_code(report):
    required = [c["status"] for c in report["checks"] if c["required"]]
    if any(s in ("FAIL", "ERROR") for s in required):
        return 1
    return 2 if "UNKNOWN" in required else 0


def compare_reports(previous, report):
    if previous["host"] != report["host"]:
        raise ValueError("--compare snapshot belongs to another host")
    old = {c["id"]: c for c in previous["checks"]}
    return {"previous_timestamp": previous["timestamp"],
            "policy_changed": previous.get("policy") != report["policy"], "checks": [
                {"id": c["id"], "before": old.get(c["id"]), "after": c}
                for c in report["checks"] if old.get(c["id"]) != c],
            "removed": [key for key in old if key not in {c["id"] for c in report["checks"]}]}


def describe(check):
    """Translate evidence into a handoff item; never emit remediation commands."""
    key = check["id"]
    current = check["current"]
    expected = check["expected"]
    title = key
    action = "请基础设施核查该项，确认配置方案并在处理后复检。"
    if isinstance(current, dict) and "values" in current:
        value = "；".join("{} × {}".format(k, v) for k, v in current["values"].items())
        if current.get("unreadable"):
            value += "；{} 处不可读".format(current["unreadable"])
    else:
        value = json.dumps(current, ensure_ascii=False) if isinstance(current, (dict, list)) else str(current)
    if key.startswith("cpu.idle.") and key not in ("cpu.idle.coverage", "cpu.idle.unnamed"):
        state = key.split(".")[-1]
        title = "CPU {} 休眠状态".format(state)
        if isinstance(current, dict) and "values" in current:
            counts = current["values"]
            value = "开启 {} 个 / 禁用 {} 个；未达标 {} 个".format(
                counts.get("0", 0), counts.get("1", 0), current["mismatches"])
            expected = "全部在线 CPU 禁用该状态"
        else:
            value = "开启 {} 个 / 禁用 {} 个".format(current.get("0", 0), current.get("1", 0))
            expected = "普通 C1/POLL 可保留，需结合固件核实"
        action = "请基础设施核查 {}，覆盖全部在线 CPU，并确认重启/内核升级后仍生效。".format(state)
    elif key in ("cpu.governor", "cpu.epp", "cpu.epb", "cpu.max_frequency", "cpu.turbo"):
        title, action = {
            "cpu.governor": ("CPU 调频策略", "请基础设施统一核查全部 cpufreq policy 的 performance 策略。"),
            "cpu.epp": ("CPU 能效偏好 EPP", "请基础设施核查驱动支持与 performance 偏好。"),
            "cpu.epb": ("CPU 能效偏置 EPB", "请基础设施核查 EPB=0（性能优先），覆盖全部在线 CPU。"),
            "cpu.max_frequency": ("CPU 软件频率上限", "请基础设施核对各 policy 的硬件允许上限；实际睿频需负载验证。"),
            "cpu.turbo": ("CPU 睿频开关", "请基础设施核查 Intel Turbo Boost / AMD CPB，并用负载验证实际频率。"),
        }[key]
        if key == "cpu.turbo":
            expected = "no_turbo=0（Intel）" if expected == "0" else "boost=1（AMD）" if expected == "1" else "固件允许睿频"
        if key == "cpu.max_frequency":
            expected = "各 policy 的硬件允许上限"
            if isinstance(current, dict) and current.get("targets"):
                expected += "：" + "；".join("{} kHz × {}".format(k, v) for k, v in current["targets"].items())
    elif key == "platform.profile":
        title = "平台性能档位"
        expected = "Maximum Performance / performance（按厂商机型）"
        action = "请基础设施导出 BIOS/BMC 性能档位；Linux 接口缺失不能视为固件未配置。"
    elif key in ("firmware.cstates", "persistence.verification"):
        title = "BIOS C1E / 深度休眠策略" if key.startswith("firmware") else "重启/内核升级后持久化"
        value = "仅凭当前运行态无法确认"
        expected = "固件关闭 C1E/深状态，采用性能档位" if key.startswith("firmware") else "重启及升级后复检仍符合基线"
        action = "请基础设施提供 BIOS/BMC 配置导出与复检证据。"
    elif key in ("cpu.idle.coverage", "cpu.frequency.coverage", "cpu.idle.unnamed"):
        title = "CPU 休眠接口覆盖" if ".idle." in key else "CPU 调频接口覆盖"
        action = "请基础设施核查 BIOS/驱动/虚拟化配置，补充证据后再判断。"
    elif key.startswith("gpu.persistence."):
        title = "GPU persistence / " + key.split(".", 2)[2]
        value = {"Enabled": "已开启", "Disabled": "未开启"}.get(current, value)
        expected = "当前开启；重启持久化另行核实"
    elif key.startswith("gpu.power.") and isinstance(current, dict):
        title = "GPU 功率 / " + key.split(".", 2)[2]
        value = "当前 {}W / 标称 {}W / 设备上限 {}W".format(current["watts"], current["default"], current["max"])
        expected = "不低于板卡标称 {}W".format(current["default"]) if "or higher" in str(expected) else "期望功率 " + str(expected)
        action = "请基础设施核对板卡标称功率及 BMC/机箱预算；实际限频需负载验证。"
    elif key.startswith("gpu.telemetry."):
        title = "GPU 瞬时运行状态 / " + key.split(".", 2)[2]
        if isinstance(current, dict):
            reasons = [r.replace("clocks_event_reason_", "").replace("clocks_throttle_reason_", "")
                       for r in current.get("active_limits", [])]
            value = "{}；温度 {}；限频原因 {}".format(current.get("pstate"), current.get("temperature"), ", ".join(reasons) or "无活动告警")
        expected = "在代表性负载下验证温度、功率与持续时钟"
        action = "请基础设施结合当时负载复测；达到标称功率不等于配置错误，空闲低频也不判错。"
    elif key == "gpu.inventory":
        title = "GPU 驱动/工具可见性"
        action = "请基础设施核实实际 GPU 厂商与驱动工具，补充设备证据。"
    elif key.startswith("memory."):
        title = {"memory.thp": "透明大页 THP", "memory.thp_defrag": "THP 整理策略",
                 "memory.swappiness": "Swap 倾向", "memory.numa_balancing": "NUMA 自动平衡",
                 "memory.hugepages": "预留大页数量"}.get(key, key)
        action = "请基础设施与业务共同评估；此项与负载相关，不要求无条件全中心统一修改。"
    elif key.startswith("rdma."):
        title = "RDMA 端口 / " + key.split(".", 1)[1]
    elif key.startswith("nvme."):
        title = "NVMe runtime PM / " + key.split(".", 1)[1]
    elif key == "service.policy_owners":
        title = "现有性能策略服务"
    elif key == "pcie.aspm":
        title = "PCIe ASPM 策略"
    if value in ("None", "interface not exposed", "no OS control"):
        value = "接口未暴露，当前无法确认"
    return title, value, expected, action


def md_cell(value):
    return html.escape(str(value), quote=False).replace("|", "\\|").replace("\n", "<br>")


def display_time(value):
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S UTC+08:00")
    except (ValueError, AttributeError):
        return str(value)


def render_markdown(reports):
    """Self-contained Chinese report for infrastructure colleagues."""
    local_tz = datetime.timezone(datetime.timedelta(hours=8))
    stamp = datetime.datetime.now(local_tz).strftime("%Y-%m-%d %H:%M:%S UTC+08:00")
    failures = [r for r in reports if "collection_error" in r]
    good = [r for r in reports if "collection_error" not in r]
    problems = [(r, c) for r in good for c in r["checks"] if c["status"] in ("FAIL", "ERROR") and c["required"]]
    uncertain = [(r, c) for r in good for c in r["checks"] if c["status"] == "UNKNOWN"]
    lines = ["# GPU 智算中心性能模式检查报告", "", "- 报告时间：" + stamp,
             "- 检查范围：{} 台，成功采集 {} 台，采集失败 {} 台。".format(len(reports), len(good), len(failures)),
             "- 职责：本工具仅检查和整理证据；具体配置与持久化由基础设施同事负责。",
             "- 执行范围：读取 sysfs/procfs、systemctl 状态和 nvidia-smi 查询；未修改远端配置、服务或固件。", "",
             "## 交接结论", "",
             "发现 **{} 项需整改**、**{} 项需核实**。UNKNOWN 表示证据不足，不等于配置错误；"
             "GPU 瞬时限频、THP 等业务相关项单列复核。".format(len(problems), len(uncertain)), "",
             "| 主机 | GPU | 在线逻辑 CPU | 需整改 | 需核实 | 告警/业务评估 |", "| --- | --- | ---: | ---: | ---: | ---: |"]
    for r in reports:
        if "collection_error" in r:
            lines.append("| {} | 采集失败 | — | — | 需恢复采集 | — |".format(md_cell(r["host"])))
            continue
        cs = r["checks"]
        inventory = next((c["current"] for c in cs if c["id"] == "cpu.inventory"), {})
        names = sorted({c["current"]["name"] for c in cs if c["id"].startswith("gpu.power.") and isinstance(c["current"], dict)})
        if not names:
            names = sorted({c["current"] for c in cs if c["id"].startswith("gpu.") and c["id"] != "gpu.inventory" and c["status"] == "SKIP" and isinstance(c["current"], str)})
        lines.append("| {} | {} | {} | {} | {} | {} |".format(md_cell(r["host"]), md_cell(", ".join(names) or "未检测到 NVIDIA GPU"),
            inventory.get("online", "未知"), sum(c["status"] in ("FAIL", "ERROR") and c["required"] for c in cs),
            sum(c["status"] == "UNKNOWN" for c in cs),
            sum(c["status"] == "WARN" or c["status"] == "FAIL" and not c["required"] for c in cs)))
    lines += ["", "## 基础设施处理清单", "", "| 主机 | 检查项 | 当前状态 | 期望状态 | 建议反馈 |", "| --- | --- | --- | --- | --- |"]
    if not problems:
        lines.append("| — | 本次未发现必需项不达标 | — | — | 仍需完成固件与持久化核实 |")
    for r, c in problems:
        title, value, expected, action = describe(c)
        lines.append("| " + " | ".join(md_cell(v) for v in (r["host"], title, value, expected, action)) + " |")
    lines += ["", "## 每台主机的证据与待核实事项", ""]
    for r in reports:
        lines += ["### " + md_cell(r["host"]), ""]
        if "collection_error" in r:
            lines += ["**采集失败，不能判断性能模式。** " + md_cell(r["collection_error"]), ""]
            continue
        inventory = next((c["current"] for c in r["checks"] if c["id"] == "cpu.inventory"), {})
        passed = [c for c in r["checks"] if c["status"] == "PASS"]
        lines += ["- 采集时间：" + md_cell(display_time(r.get("timestamp", "未知"))), "- 内核：" + md_cell(r["kernel"]),
                  "- CPU：" + md_cell(inventory.get("model_name") or "未采集型号"),
                  "- 机器型号 / BIOS：{} / {}".format(md_cell(inventory.get("system_product") or "未知"), md_cell(inventory.get("bios_version") or "未知")),
                  "- idle 驱动 / 调频驱动：{} / {}".format(md_cell(inventory.get("idle_driver") or "未知"), md_cell(", ".join(inventory.get("frequency_drivers", [])) or "未知")),
                  "- 已达标 {} 项（明细见下方展开项）。".format(len(passed)), "",
                  "| 状态 | 检查项 | 当前值 / 证据 | 期望值 |", "| --- | --- | --- | --- |"]
        for c in r["checks"]:
            if c["status"] in ("INFO", "SKIP", "PASS"):
                continue
            title, value, expected, action = describe(c)
            status = {"PASS": "已达标", "UNKNOWN": "需核实", "WARN": "负载复核", "ERROR": "检查异常"}.get(c["status"], "需整改" if c["required"] else "业务评估")
            lines.append("| " + " | ".join(md_cell(v) for v in (status, title, value, expected)) + " |")
        lines += ["", "<details>", "<summary>已达标项目（{} 项）</summary>".format(len(passed)), "",
                  "| 检查项 | 当前值 | 期望值 |", "| --- | --- | --- |"]
        for c in passed:
            title, value, expected, action = describe(c)
            lines.append("| " + " | ".join(md_cell(v) for v in (title, value, expected)) + " |")
        lines += ["", "</details>"]
        lines += ["", "<details>", "<summary>补充只读证据（启动参数、NUMA、策略服务、RDMA、NVMe）</summary>", ""]
        for c in r["checks"]:
            if c["status"] == "INFO":
                title, value, expected, action = describe(c)
                lines.append("- **{}**：{}".format(md_cell(title), md_cell(value)))
        lines += ["", "</details>", ""]
        if r.get("comparison"):
            delta = r["comparison"]
            lines += ["与上次 {} 对比：检查内容变化 {} 项；基线变化：{}。".format(
                md_cell(delta["previous_timestamp"]), len(delta["checks"]), "是" if delta["policy_changed"] else "否"), ""]
            if delta["policy_changed"]:
                lines += ["检查期望发生变化，不能把全部差异直接归因于机器配置变更。", ""]
            lines += ["| 变化项 | 上次状态 / 当前值 | 本次状态 / 当前值 |", "| --- | --- | --- |"]
            for change in delta["checks"]:
                before, after = change["before"], change["after"]
                if after["status"] in ("INFO", "SKIP") and (not before or before["status"] in ("INFO", "SKIP")):
                    continue
                title, value, expected, action = describe(after)
                old = "无对应检查项" if not before else before["status"] + " / " + str(describe(before)[1])
                lines.append("| " + " | ".join(md_cell(v) for v in (title, old, after["status"] + " / " + str(value))) + " |")
            if delta["removed"]:
                lines += ["", "本次未再出现的检查项：" + md_cell(", ".join(delta["removed"]))]
            lines.append("")
        elif r.get("previous_snapshot_missing"):
            lines += ["未找到该目标的上次快照，本次仅展示当前状态。", ""]
    lines += ["## 请基础设施同事反馈", "",
              "1. 对处理清单逐项反馈：已处理 / 计划处理 / 按业务要求保留，并注明机型、配置位置和原因。",
              "2. 提供 BIOS/BMC 的性能档位、C1E/Enhanced Halt、深度/package C-state 和睿频设置证据。OS 的 disable=1 不能代替固件验证。",
              "3. 说明如何在重启、内核升级及 CPU 热插拔后保持配置，并安排处理后、重启后各一次复检。",
              "4. 功率或温度告警结合当时负载复测；CPU/GPU 性能最终以同条件压测的吞吐、尾延迟、持续频率和温度验收。", "",
              "本报告不包含配置修改命令，也不安装任何性能服务；原有系统调优 playbook 保持原样。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="JSON file containing expected values (never applied)")
    parser.add_argument("--format", choices=["markdown", "text", "json"], default="markdown")
    parser.add_argument("--output", help="explicitly save report in selected format to this file")
    parser.add_argument("--compare", help="compare to a previous JSON snapshot of the same host")
    args = parser.parse_args()
    if platform.system() != "Linux":
        parser.error("this checker requires Linux")
    policy = json.loads(Path(args.config).read_text()) if args.config else {}
    previous = json.loads(Path(args.compare).read_text()) if args.compare else None
    if previous and previous["host"] != platform.node():
        parser.error("--compare snapshot belongs to another host")
    report = Performance(policy).collect()
    if previous:
        report["comparison"] = compare_reports(previous, report)
    output = json.dumps(report, indent=2, ensure_ascii=False) if args.format == "json" else render_markdown([report])
    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    print(output)
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
