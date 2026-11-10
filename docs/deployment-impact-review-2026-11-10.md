# 本次修复对已有部署的影响

比较基线：`ea44087`（PR #79 之前的 main）。本记录描述当前 PR 的代码行为，
未执行生产部署、数据迁移、磁盘格式化或生产服务重启。

## 已按要求撤回

- rqlite 的 systemd 模板与基线一致：root 用户、原有 join 参数和认证行为。
- rqlite 默认数据目录恢复 `/root/rqlite/node`，inventory 也恢复原值。
- 删除新增的账户创建、auth.json、认证强制检查和迁移要求。
- Grafana 配置模板恢复基线；删除新增密码变量、强制密码检查及 API 改密。
- #71 不纳入本 PR 的修复关闭列表；#67 只保留镜像固定版本部分，凭证部分不处理，issue 继续开放。
- rqlite 的下载包校验仍保留，属于 #68；它不变更数据库账户、目录或认证。

## 已确认保留的部署行为

| 改动 | 何时触发 | 实际影响与限制 |
| --- | --- | --- |
| Cache/Client 的 systemd 单元和客户端启动脚本更新 | 再次执行相应 deploy/site playbook | 文件内容变化会走原有重启流程。Cache 会通知 handler 并重启，没有客户端那样的忙挂载延后检查；普通 cache_05_deploy 没有 serial 分批限制，可能多个 Cache 节点同时重启，影响正在使用缓存的客户端。Client 部署和升级均为 serial: "20%"，通常只在检查为空闲时重启；原有 force_restart 开关仍可绕过忙检查。 |
| 持久化待重启标记 `.restart-required` | 文件已更新但上次运行中断，或客户端因为忙而延后 | 即使本次 copy 显示 unchanged，也可能补做重启。这是修复漏重启的预期行为，不能再把 unchanged 理解为“绝不会重启”。缓存升级保留 serial: 1。 |
| SSH 默认开启 host key checking | 所有使用本仓库 ansible.cfg 的 SSH 连接 | known_hosts 没有目标主机，或者密钥变化，会阻断连接；需要已有可信主机密钥。没有自动扫描/信任密钥的新增步骤。 |
| 下载强制 SHA256 | 显式启用 CLI 下载；或运行 rqlite/jemalloc 角色的包准备阶段 | CLI 下载需要新增 checksum 配置。默认版本 rqlite v9.4.5 / jemalloc 5.3.0 已提供 hash；自定义版本、重新打包的本地包必须提供对应 hash，否则直接失败。jemalloc 即使已安装，也会先检查控制机的 staged 包。在线路径可能重新下载并替换不匹配的缓存包。 |
| CLI 下载先运行候选的 `--version` | 显式启用 CLI 下载，且 hash 验证成功 | 命令在控制机执行，CLI 自身可能初始化其运行环境。候选不能执行则保留原 staged CLI；替换成功时保留旧文件备份。默认仍不下载。 |
| 默认固定监控镜像版本，允许 inventory 自行覆盖 | 执行监控部署角色 | 默认改成 node-exporter v1.9.1、Prometheus v2.53.5、Grafana 11.6.0。inventory 可以使用 :latest 或其他镜像引用，不再做强制拒绝。已有正常运行的监控栈会跳过 deploy，但仍可能拉取固定镜像；今后创建/重建栈时采用这些版本。尚未验证与现网版本、数据目录和服务 CLI 的兼容性，尤其要防止重新创建时降级。 |
| 配置文件权限 0644 → 0600，并固定归属 | 写 dingo.yaml、dingocli.cfg、monitor.yaml | 只有文件所有者和 root 可读。使用其他账号读取这些文件的脚本/工具可能失败。dingo.yaml / dingocli.cfg 的渲染内容未改；diff 和任务输出被隐藏以避免泄密。 |
| THP 持久化 | 执行 tune 角色 | 当前设置仍按 tune_thp_mode 应用；新增 tmpfiles.d 配置使重启后继续使用该策略（默认 madvise）。以前重启会恢复系统默认，现行为不同，会影响重启后的内存性能。 |
| Core/process 限制及 root PAM limits | 更新服务单元后重启；或新建 root 登录会话 | Cache/Client 增加 LimitCORE=infinity、LimitNPROC=4194304；root PAM 会话增加 nofile/core/nproc 条目。进程崩溃时可能真正产生很大的 core 文件并占满磁盘，core 也包含进程内存。不是只有文字说明变化。 |
| Core 目录权限 0777 → 1777 | 执行 tune 角色 | sticky bit 阻止普通用户删除其他用户的文件；依赖跨用户清理 core 的脚本可能受影响。root 清理不受此限制。 |
| 空 Cache ID 自动再生成 | 已有 dingo-cache.conf 没有 --id 或值为空 | 生成稳定 UUID，并通过原有模板/handler 更新配置、重启服务。会改变该异常节点后续使用的 ID/缓存子目录；已有非空 ID 保持不变，也不删除旧缓存目录。 |
| 更严格地报告错误 | Kubernetes 节点查询、监控 config/status 查询、Podman socket 或 linger 操作失败 | 原来部分错误被跳过或误当成功；现在会中止相应主机的执行。旧 CLI 不支持新用到的查询子命令、输出格式不兼容时也会失败，需要验证 CLI 兼容性。 |

上述策略已经由操作者确认：Cache 可按选定主机或并发部署，Client 部署和升级按选定主机的 20% 分批
执行；保留无限 core、THP 持久化、SSH 校验、0600 权限、SHA256 校验及空 ID 再生成。
监控镜像允许 latest 覆盖。代码仍在 PR 中，没有自动部署。

## 磁盘、卸载和删除操作

| 操作 | 是否本次新增 | 触发条件及影响 |
| --- | --- | --- |
| `pvcreate` / `vgcreate` / `lvcreate` / `mkfs` 新建数据盘或缓存盘 | 原来就有；本次未增加格式化命令或默认 force | 运行 LVM provisioning playbook，且检测到待创建挂载和可用设备时，仍会格式化选中的盘。它本来就属于高风险操作；`lvm_force` / `lvm_data_force` 仍默认 false。 |
| 缓存盘编号选择 | 本次修改 | 每次分配时避开已存在的 VG、挂载点和 fstab 条目，拒绝隐藏非空目录。可能使用更大的 diskN 编号；硬编码磁盘编号的外部配置需核对。 |
| 数据盘恢复 `lvm_data_repair=true` | 本次新增，默认 false | 显式开启后可激活已有 VG、挂载验证过的 LV，并补写缺少的 fstab 条目。恢复已有 LV 的分支不执行 pvcreate/mkfs；冲突、未知文件系统或非空目标会失败。默认只报告异常并停止。整个 LVM 建盘 playbook 仍可能继续为其他缺失的数据盘创建并格式化新卷，不能把 repair=true 当成整份 playbook 的“只修复模式”。 |
| 重映射缓存挂载、改 fstab、停/启服务 | 原来就有 | 仍须显式 `remap_apply=true`，且探测到需要重映射。会中断相关服务和改变挂载位置。本次改的是恢复/启动失败处理，没有默认开启 remap。`--check` 不进入 apply，但会复制辅助脚本到 /tmp 并运行探测。 |
| 日志到期删除、压缩后删除原日志 | 原来就有，保留期限和清理目录规则未变 | cron_cleanup 安装的任务仍按原配置运行；默认 delete_days=3、compress_days=2，具体以 inventory 为准。本次修复参数引用及失败归档清理，成功压缩后仍会删除源日志。 |
| 删除失败归档、下载候选和完成的重启标记 | 本次新增的清理 | 只针对失败 tar 的临时归档、此次 CLI 下载候选、验证完成后的 .restart-required。未新增删除 DingoFS 业务数据、rqlite 数据库或已有缓存数据的步骤。 |
| 客户端启动时正常 umount / remount | 原来就有 | 本次把注册挂载的查询改成精确 host:port:path 匹配，并让查询失败中止启动。没有新增强制卸载或 lazy unmount。 |

其余修复包括：缓存二进制缺失时提前报错、busy 客户端升级延后后继续处理其他实例、
有限 --limit 场景只交换当前活跃 meta 主机的 SSH key、Podman 使用通用 package 模块、
linger 幂等、cron 五字段展示、性能报告真正输出纯文本、CI 忽略本地依赖目录，以及
修正文档/注释。OOMScoreAdjust 增加配置项但默认仍是实际已有的 0；挂载等待从约
30 秒延长到约 120 秒，服务 RestartSec 仍为 30 秒。

## 平时如何使用，不需要审查脚本

| 用途 | 使用入口 | 会不会建盘/格式化 |
| --- | --- | --- |
| 已有机器加挂载、部署客户端 | client_04_deploy.yml，按需加 --limit | 不执行 LVM 建盘；按选定主机的 20% 分批处理客户端。 |
| 已有机器升级客户端二进制 | client_07_upgrade.yml，按需加 --limit | 不执行 LVM 建盘；按选定主机的 20% 分批升级，忙实例延后。 |
| 已有机器部署/升级 Cache | cache_05_deploy.yml / cache_06_upgrade.yml | 这两个入口不执行 LVM 建盘；前者可并发，后者逐台。 |
| 新机器从头部署或专门准备数据盘 | client_site.yml、cache_site.yml、cache_02_lvm.yml、01b_lvm_data.yml | 含建盘步骤，可能格式化检测到的可用设备。client_site 本来就包含 cache_02_lvm，不能把整套 site 当作仅更新客户端的入口。 |

正常情况下不设置 lvm_data_repair 和 remap_apply。已有数据卷还在但挂载丢失时才
处理恢复；想调整缓存磁盘与挂载点对应关系时才做 remap。保留现有日志清理周期。
这些触发条件由代码和测试核实，不要求操作者审查 shell 脚本。

Cache ID 也参与本地目录：DingoFS CacheNode 把 --id 赋给 cache_dir_uuid，
RealCacheDir 再拼接为 <cache_dir>/<ID>。因此改变 ID 会使用另一个本地子目录。
本 PR 只在 ID 缺失/为空时生成，保留已有非空 ID，不移动或删除旧目录。
