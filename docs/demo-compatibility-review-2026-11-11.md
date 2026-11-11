# standalone/demo 改动兼容性审查

分支：`codex/demo-standalone`，独立验证，尚未合入 main。

比较基线：最新 main `025357a`（2026-11-11 拉取）。范围为本轮未提交的 standalone、demo 资源配置、monitor 恢复与失联 FUSE 卸载修复；已在基线中的历史改动不计为本轮新增。

结论：没有发现默认将既有高资源部署降配、换镜像、关闭 RDMA/NUMA 绑核或跳过做盘的改动。
多数改动由新入口或 demo 环境变量显式启用；共享挂载/卸载检查仍是需要重点审查的执行路径。
下面列出发现并处理的问题，以及尚需遵守的使用边界。

## 发现并处理

### P1：手工重装指南删除了过宽的 TiKV 父目录

旧指南包含删除 `/var/lib/dingofs-tikv`、`/var/log/dingofs-tikv` 父目录的命令。
同机若另有集群把部署/数据目录放在这些父目录下，操作者执行该清理可能一起删除其他集群内容。
这不是普通 deploy 自动执行的任务，但它确实是具有破坏性的运维指令。

已从 `SERVER-REINSTALL.md` / `STORAGE-REINSTALL.md` 移除父目录删除，保留 TiUP 对指定集群的 destroy，
以及本集群命名的 `/root/tikv/dingofs-demo-tikv` 输入副本清理。destroy 本身仍是显式的删除数据操作。
本轮审查没有执行这些清理命令。

### P2：新挂载表探测需要保留别名处理及明确的错误状态

为支持失联 FUSE，检查改成读取内核 mountinfo，替代对挂载根目录的 stat。
审查发现字面路径匹配不足以处理符号链接别名，读取/解析异常也必须与“确实未挂载”区分。

已补充：找不到原路径时解析别名再匹配；真实未挂载返回 1，读取/解析异常返回 2。
没有取消 fuser、容器及挂载命名空间占用检查，也没有添加强制/lazy 卸载。
新增回归覆盖正常挂载、别名、未挂载、错误挂载表和读取失败。

### 撤回未完成的客户端构建配置

已停止 demo 上的临时 client 构建，没有发布或替换任何自编译制品。
已撤回尚未完成的 `x86-64-v3` 制品路径及 CPU 检查扩展，恢复原有制品路径契约。
兼容 CPU 的 release 交由开发提供；不在本轮运维改动中修改源码编译策略。

## 各改动的触发范围

| 改动 | 默认值 / 触发条件 | 既有高资源环境影响 |
| --- | --- | --- |
| 新 standalone playbook/role | 只有显式运行新入口才使用 | 原 site/deploy 入口不自动引入这些角色 |
| Cache 系统盘目录模式 | `dingo_cache_storage_mode=mounted`；demo 显式设 directory | 既有按盘发现、容量比例、分组权重计算仍执行 |
| 跳过 LVM | `dingofs_skip_disk_setup=false`；directory 也显式跳过 | 没有设置的环境保持原做盘条件；没有新增 force/mkfs 分支 |
| Client/Cache CPU、MemoryHigh/Max | 默认空，不输出 systemd 限制 | 原高资源实例不会得到 demo 的 1/2 CPU 和小内存上限 |
| Client bthread / poll_mode 参数 | 默认空，不新增启动参数 | 原大内存缓冲区档位、shards、RDMA、绑核及 NUMA 默认保留 |
| MDS 额外参数、业务 IP、CLI key | 默认空/原管理地址/原 key 路径 | 只有现场显式覆盖才改变配置；本次地址设置只在 demo |
| Podman 固定版本和额外 APT 源 | 默认包列表/URL 为空；socket 默认仍开启 | 原系统源安装逻辑保留；6.1.3 与额外软件源只在 demo |
| TiDB 必需校验 | 通用 verify 默认下限 0；standalone 设置 1 | 原非 standalone TiKV 流程不会新增强制 TiDB 要求 |
| Grafana API 等待、UID 修复 | `dingo_monitor_initialize_grafana=false` | 原站点不会强制使用新增 API 凭据或初始化逻辑 |
| monitor Podman 资源限制 | 默认空映射 | 旧 Docker/Podman 站点不执行新增 update |
| monitor 状态解析 | 忽略 CLI 已知的动画省略号 | 共享生效；未知响应和失败查询仍不能被当作“没有部署” |
| 失联 FUSE 卸载修复 | 仅显式执行卸载或部署前占用检查 | 共享生效；仍校验 unit、脚本、FS 名、内核挂载来源和其他服务的路径归属 |

显式启用固定 Podman/APT 包时，它是宿主机级的运行时变更，会影响同机其他容器使用的工具链，不能视作只修改某一个 DingoFS 集群。
该功能没有在其他站点默认开启。同样，卸载会停止并删除所选实例的服务/安装文件；日志和缓存数据删除仍须显式开启。

demo 的 CPU/内存预算、TCP、关闭绑核、挂载点 `/dingofs/manila`、禁用远端 cache 等均位于
`ansible-dingofs-env/demo-cci/v5.2-standalone/group_vars/all.yml`。
其他站点应继续只加载自己的 inventory/group_vars，不把 demo 变量混入同一次跨站点执行。
本轮没有修改已有 xn01、hd05 等站点的配置。

## 比对与测试证据

用 xn01/v5.2 公共变量、大内存档位和合成的 CPU/NUMA 运行时事实，在本地分别渲染 HEAD 和工作区版本。
以下 7 个关键模板的输出逐字节一致：

- Cache conf、启动脚本、systemd unit。
- Client 启动脚本、systemd unit。
- MDS CLI hosts 与 topology。

这验证了该高资源配置样例的默认输出兼容性，不等同于对全部生产主机及其动态探测结果的实际部署验收。
结构化结果见 `demo-compatibility-render-2026-11-11.json`。

39 个 playbook 的语法检查全部通过。

本轮针对性验证：运行时/挂载检查 11 项、部署安全 17 项、目录模式 4 项、资源参数 2 项均通过。
失联挂载卸载修复在修改时另有 15 项卸载测试，14 项通过、1 项仅 Linux 可运行而在本地跳过；
demo worker-6 的旧挂载已用正常 umount 和原卸载 playbook 成功移除。

## 仍存在但并非本轮引入的风险

- 同机多套 monitor 仍需要独立端口、数据目录和配置文件；原角色的 cluster checkout 会修改共享 CLI 默认选择。
- 全量 site playbook 原本就包含做盘/系统调优；运行哪个入口仍决定它是否触及磁盘。单独 client/cache deploy 不调用 LVM。
- 已有待重启标记、二进制或配置实际改变时，deploy 仍会按既有策略重启；“没有降配”不代表任何情况下都不会重启。
- 当前 release 的 AVX-512 不兼容属于制品编译目标问题；本轮 Ansible 配额和挂载点设置不能解决它。

建议将 demo 专属入口/配置与共享挂载检查修复分开评审和提交，避免用一份大的变更直接覆盖全部生产站点。

## 同步最新 main 后的分支验证

`codex/demo-standalone` 已 rebase 到 main `025357a`，保留上游 RDMA 设备检查和工作线程 CPU 归属修复。
补跑部署安全、CPU 工作线程、挂载运行时和 standalone 共 65 项测试：64 通过，1 项 Linux 专用测试在本地跳过；7 个关键模板与最新 main 的高资源样例输出一致。
此分支仍独立维护，未合并 main；上述检查不替代生产部署验证。
