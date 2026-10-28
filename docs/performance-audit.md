# 性能模式只读检查（单文件）

[`scripts/check_performance.py`](../scripts/check_performance.py) 是唯一需要带到现场的文件：
Python 3 标准库、单文件、无依赖。它只负责检查、对比和生成报告，具体配置修改与持久化由
基础设施同事处理。原有 `roles/tune`、inventory、调优 playbook 和 CI 保持原样。
不提供 apply 模式，不安装服务、不修改 sysfs/sysctl/BIOS、不重启机器。

## 准备与执行

在 Linux 上以 root 运行（非 root 也能跑，但读不到的项目会记为 UNKNOWN，不会误判为达标）。
把文件拷到 GPU 机器后执行；报告默认打印到 stdout，建议用 `--output` 落到文件：

```bash
# 1. 拷到目标机
scp scripts/check_performance.py root@GPU-HOST:/tmp/

# 2. 执行并生成 Markdown 报告
ssh root@GPU-HOST 'python3 /tmp/check_performance.py \
  --output /tmp/performance-check.md'

# 3. 取回报告
scp root@GPU-HOST:/tmp/performance-check.md ./performance-GPU-HOST.md
```

多台机器就对每台重复一次，远程展开一次主机名即可区分报告：

```bash
ssh root@GPU-HOST 'python3 /tmp/check_performance.py \
  --output /tmp/performance-$(hostname).md'
```

`--output` 只写指定报告文件。所有检查命令均为查询。
单机退出码：0=必需检查通过；1=有必需项不达标；2=必需证据不足。仍会生成完整报告。
固件等人工核实项会明确展示，不因退出码 0 而隐藏。

## 选项

| 选项 | 说明 |
| --- | --- |
| `--format markdown\|text\|json` | 报告格式，默认 `markdown`（中文） |
| `--output PATH` | 把报告写入指定文件；默认只打印到 stdout |
| `--compare BEFORE.json` | 与同一台机器的上次 JSON 快照逐项对比 |
| `--config expected.json` | 自定义期望值，只用于比较，不会写入主机 |

`python3 check_performance.py --help` 会打印同样的用法和选项。

## 修改前后对比

先留一份 JSON 基线，基础设施处理完后**在同一台机器上**对比：

```bash
python3 check_performance.py --format json --output /tmp/performance-before.json
# ... 基础设施处理 ...
python3 check_performance.py --compare /tmp/performance-before.json \
  --output /tmp/performance-after.md
```

`--compare` 会拒绝其他主机的快照；检查期望发生变化时会单独标注，
不能把口径变化直接归因于机器配置变更。

## 检查口径

| 项目 | 默认期望 / 解释 |
| --- | --- |
| CPU C-state | 按全部在线 CPU 的实际状态名检查；POLL、普通 C1 及已列出的普通 C1 别名可保留，C1E、C6、C6P、AMD C2 等期望禁用。不会修改任何状态 |
| CPU 频率 | 所有 cpufreq policy 的 governor=performance；存在接口时 EPP=performance、EPB=0；睿频允许；软件频率上限不低于硬件允许值。实际持续睿频仍需压测 |
| GPU | 按实际产品识别 NVIDIA；检查 persistence、设备标称功率与配置功率；温度、活动限频原因、时钟和 PCIe 速率作为负载证据。空闲 P8/低速不判错 |
| BIOS/BMC | 通过通用 OS 接口无法证明 Enhanced Halt/C1E、package C-state 和固件性能档位，明确标记为需基础设施核实 |
| 持久化 | 当前运行态不能证明重启/升级后状态，要求基础设施说明持久化方式并安排复检；不要求安装我们自己的 unit/timer |
| THP / NUMA / 内存 | THP 默认 madvise；属于业务评估项。其他内存设置展示当前值，不把所有 workload 参数强制统一 |
| RDMA / NVMe / ASPM | 读取端口 state/rate、runtime PM、ASPM policy，提供诊断信息；需结合实际拓扑、额定速率和负载验证 |
| 现有策略服务 | 只展示 tuned 等服务状态，服务 active 本身不视为问题；策略如何实现由基础设施决定 |

可用 `--config /path/to/expected.json` 显式提供检查期望，所有值仅用于比较，例如：

```json
{
  "gpu_required": true,
  "gpu_power_limits": {"GPU-REAL-UUID": 700},
  "sysctl": {"kernel.numa_balancing": 0}
}
```

不要用任意机型通用瓦数替代设备标称值。非 NVIDIA GPU 不套用 NVIDIA 管理规则；
缺少驱动、接口或权限时报告 UNKNOWN，而不是声称已达标。
BIOS、NUMA/IRQ、功率预算和压测由基础设施结合机型安排，本报告不生成修改命令。

## 报告怎么交接

Markdown 报告是自包含的：主机概览、基础设施处理清单、当前值/期望值、各机核实事项
与已达标项目都在里面，直接把这份 md 转交即可。建议附上原始 JSON 快照，便于处理后
用 `--compare` 复检。

## 原实现问题的交接依据

旧 `cpupower idle-set -D 3` 中的 3 是退出延迟阈值（µs），可能漏掉退出延迟为 2µs 的 C1E；
原有“任一状态已 DISABLED 就跳过”的判断也可能留下部分未禁用状态。按职责要求，原代码保持原样，
新的检查器独立发现这些差距，并提供给基础设施处理。

- [Linux CPU idle 管理](https://docs.kernel.org/admin-guide/pm/cpuidle.html)
- [intel_idle 与固件状态关系](https://docs.kernel.org/admin-guide/pm/intel_idle.html)
- [上游 cpupower idle-set 手册](https://github.com/torvalds/linux/blob/master/tools/power/cpupower/man/cpupower-idle-set.1)
- [NVIDIA nvidia-smi 查询字段](https://docs.nvidia.com/deploy/nvidia-smi/index.html)
