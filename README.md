# ansible-dingofs

Ansible playbooks and roles for deploying and managing DingoFS meta server clusters, DingoFS Cache nodes, and DingoFS Client (FUSE mount) nodes on CentOS/RHEL/Rocky and Ubuntu/Debian Linux.

## Versioned branches

A branch deploys exactly one DingoFS version. `main` tracks the current one and
the `VERSION` file at the repo root names it; when a new version arrives, `main`
moves on and a branch named after the old version freezes it:

| branch   | deploys                     | VERSION |
|----------|-----------------------------|---------|
| `main`   | current version (v5.2)      | `v5.2`  |
| `v5.1`   | v5.1 sites (frozen)         | --      |
| `v5.2`   | v5.2 sites, once main is v5.3 | `v5.2` |

Site configs live in the private `ansible-dingofs-env` repo and declare their
version as `dingofs_site_version`. The `version_guard` role (imported by
`dingo_cache`, `dingo_client`, `dingofs_cluster`) asserts that the checkout and
the site agree, and fails before writing anything -- the v5.2 config carries
flags the v5.1 binaries do not define, so the wrong pairing breaks a running
service rather than erroring out cleanly. Override for a deliberate one-off:
`-e dingofs_skip_version_check=true`.

## Prerequisites

- **Control node**: Linux with Ansible >= 2.12 installed
- **Target nodes**: CentOS/RHEL/Rocky Linux (3 nodes minimum for HA)
- **Network**: All target nodes reachable via SSH from the control node
- **Privileges**: Root or sudo access on target nodes

## Repository Structure

```
ansible-dingofs/
├── ansible.cfg                  # Ansible configuration
├── inventory/
│   ├── hosts.yml                # Inventory (edit with your IPs)
│   └── group_vars/all.yml       # Global variables (edit to customize)
├── playbooks/
│   │                            # --- meta server ---
│   ├── meta_site.yml            # Full meta deployment (01 -> 05, NOT self-contained:
│   │                            #   see the Meta Deployment section for the two
│   │                            #   steps it leaves out)
│   ├── 01_prepare.yml           # System prep: user, /etc/hosts, SSH, tools
│   ├── 01b_lvm_data.yml         # NVMe LVM data disks at /mnt/diskN
│   ├── 02_tune.yml              # Performance tuning
│   ├── 03_rqlite.yml            # RQLite database cluster
│   ├── 04_dingo_cli.yml         # Podman + dingo CLI installation
│   ├── 05_deploy_cluster.yml    # DingoFS cluster deployment
│   ├── 99_status.yml            # Cluster health check
│   │                            # --- cache ---
│   ├── cache_site.yml           # Full cache deployment (cache_01 -> 05)
│   ├── cache_01_prepare.yml     # Cache: basic tools, cpupower
│   ├── cache_02_lvm.yml         # Cache: NVMe LVM setup
│   ├── cache_02b_remap.yml      # Cache: normalize mount layout by disk size
│   ├── cache_03_tune.yml        # Cache: performance tuning
│   ├── cache_04_jemalloc.yml    # Cache: compile & install jemalloc
│   ├── cache_05_deploy.yml      # Cache: dingo-cache service
│   ├── cache_06_upgrade.yml     # Cache: upgrade binaries
│   ├── cache_99_status.yml      # Cache: health check
│   │                            # --- client ---
│   ├── client_site.yml          # Full client deployment (client_01 -> 06)
│   ├── client_01_prepare.yml    # Client: basic tools, cpupower
│   ├── client_02_tune.yml       # Client: performance tuning
│   ├── client_03_jemalloc.yml   # Client: compile & install jemalloc
│   ├── client_04_deploy.yml     # Client: dingo-client FUSE service
│   ├── client_05_label.yml      # Client: node labels
│   ├── client_06_dingocli.yml   # Client: ~/.dingo/dingo.yaml + dingo binary
│   ├── client_07_upgrade.yml    # Client: upgrade binaries, restart mounts
│   ├── client_98_discover.yml   # Client: discover actual mounts without fs config
│   ├── client_99_status.yml     # Client: health check
│   │                            # --- cross-cutting / standalone ---
│   ├── config_podman_rootless.yml  # Rootless podman + systemd linger
│   └── config_cron_cleanup.yml     # Log cleanup script + daily cron job
└── roles/
    ├── common/                  # User, /etc/hosts, SSH keys, packages
    ├── tune/                    # CPU, ulimits, sysctl, hugepages
    ├── rqlite/                  # RQLite install + systemd cluster
    ├── dingo_cli/               # dingo CLI binary + config
    ├── podman/                  # Podman container engine (rootful)
    ├── podman_rootless/         # Rootless podman: linger, storage.conf, XDG
    ├── dingofs_cluster/         # Topology, image pull, cluster deploy
    ├── lvm_data/                # Meta: NVMe LVM data disks (/mnt/diskN)
    ├── dingo_env/               # ~/.dingo/dingo.yaml (mdsaddr and friends)
    ├── cache_prepare/           # Cache node system preparation
    ├── lvm_cache/               # Cache: NVMe LVM detection + setup
    ├── jemalloc/                # Compile jemalloc from source
    ├── dingo_cache/             # Dingo-cache service deployment
    ├── dingo_client/            # Dingo-client FUSE mount service
    ├── dingo_client_upgrade/    # Dingo-client upgrade
    └── cron_cleanup/            # Log cleanup script + cron job
```

Some playbooks are deliberately **not** part of any `*_site.yml` and must be run
explicitly. The ones that matter for a working meta deployment:
`01b_lvm_data.yml` and `client_06_dingocli.yml` -- see the Meta Deployment
section below.

`client_06_dingocli.yml` defaults to cache/client workers, excluding meta/admin
nodes. Pass `-e target_hosts=admin` for a meta command environment; it uses the
manually prepared server CLI and never downloads latest. (The same trap
applied to `config_podman_rootless.yml`; it no longer matters for meta, because
`04_dingo_cli.yml` now runs the role directly.)

## Inventory variable layout

`inventory/group_vars/all.yml` keeps the original flat Ansible variable names and
uses numbered sections aligned with site configuration files. Sections appear
only when the file defines variables or notes for that category.

| Section | Configuration |
| --- | --- |
| 01 | Site version and deployment mode |
| 02 | Accounts, SSH and shared paths |
| 03 | Container engine and rootless storage |
| 04 | Meta/Cache disk provisioning |
| 05 | Host tuning and jemalloc |
| 06 | RQLite |
| 07 | Dingo administration CLI |
| 08 | Meta cluster, MDS/TiKV, coordinator, store and executor |
| 09 | Cache service |
| 10 | Client mounts |
| 11 | Shared RDMA readiness |
| 12 | Dingo command environment and filesystem backend |
| 13 | Monitoring |
| 14 | Log cleanup |
| 15 | Site notes and operational records |
| 16 | Per-host overrides |

Cache and Client sections group their paths, network endpoints, CPU/NUMA,
RDMA, logging and lifecycle settings. Keep mount instance lists together and
place `dingofs_host_vars` at the end. Add future overrides beside the related
role variables, with site-specific explanations next to the relevant settings.

## DingoFS Meta Deployment

### 1. Configure inventory

Edit `inventory/hosts.yml` with your server IPs and hostnames:

```yaml
all:
  children:
    meta_servers:
      hosts:
        node-1:
          ansible_host: 10.0.0.1
          rqlite_node_id: 1
        node-2:
          ansible_host: 10.0.0.2
          rqlite_node_id: 2
        node-3:
          ansible_host: 10.0.0.3
          rqlite_node_id: 3
    rqlite_master:
      hosts:
        node-1:
    rqlite_slaves:
      hosts:
        node-2:
        node-3:
    admin:
      hosts:
        node-1:
```

### 2. Customize variables

Edit `inventory/group_vars/all.yml` to adjust:
- Container images and versions
- Service ports
- Performance tuning parameters
- Dingo CLI settings

### 3. Run full deployment

`meta_site.yml` runs phases 01-05. Two steps live outside it and have to be run
around it, in this order:

```bash
INV=inventory/hosts.yml            # or -i <region>/hosts.yml

# (a) Data disks FIRST -- meta_site.yml does not create them, and
#     dingofs_cluster does not fail when they are missing: it writes the data
#     onto the system disk instead.
ansible-playbook -i $INV playbooks/01b_lvm_data.yml

# (b) Main deployment. This now also configures rootless podman for the service
#     user (04_dingo_cli.yml runs the podman_rootless role), so no separate
#     step is needed for the containers to start.
ansible-playbook -i $INV playbooks/meta_site.yml

# (c) Write ~/.dingo/dingo.yaml, which carries the MDS address used by every
#     `dingo fs ...` command. Without it the CLI falls back to 127.0.0.1:7400 and
#     each command dies with
#         Error-Code: 660000 / rpc request to mds cluster failed / context deadline exceeded
ansible-playbook -i $INV playbooks/client_06_dingocli.yml -e target_hosts=admin

# (d) Verify
ansible-playbook -i $INV playbooks/99_status.yml
```

Notes:

- Run `01b_lvm_data.yml` **before** `meta_site.yml`. The `common` role creates
  `/mnt/disk1/corefiles`; doing that before the disks are mounted leaves the
  directory on the system disk, where it is then hidden by the mount.
- `01b_lvm_data.yml` cannot be previewed with `--check`: the work is done by a
  shell script, which check mode skips. Its closing mount assertion still runs
  (deliberately) and fails with "not mounted" -- that is the expected result, not
  a broken playbook.
- Step (c) is idempotent; re-run it freely.

### Existing hosts: add another cluster

`meta_site.yml` is the first-install workflow. When hosts already run another
DingoFS cluster, use `meta_add_cluster.yml` instead. In the new site's inventory,
set `dingofs_meta_existing_hosts: true`, list the existing data mounts in
`dingofs_existing_mounts`, and use independent service ports, paths and cluster name.
The additional-cluster workflow requires the full meta inventory and one admin.

```bash
INV=../ansible-dingofs-env/bj09/v5.2-tikv/hosts-meta.yml
ansible-playbook -i "$INV" playbooks/meta_add_cluster.yml --check
ansible-playbook -i "$INV" playbooks/meta_add_cluster.yml
```

This checks the existing users, mounts, container engine, reserved/listening ports
and TiKV dependency, then updates the shared CLI and deploys only the new cluster.
It does not recreate users, initialize disks/RQLite, rewrite Podman settings,
run system tuning, deploy monitors, replace host registrations or select a new
shared default cluster. It verifies the old topologies and default selection
after the operation. A healthy, matching existing target is verified without
reinitialization; configuration drift, partial deployment and query failures stop
the run for inspection rather than triggering another initial deploy.

Prepare the compatible **server** CLI manually on the Ansible controller at
`dingo_cli_pkg_path` (default `/root/dingofs/pkg/dingo`) before deploying servers.
Server workflows never download a CLI URL and fail if this file is missing.
They validate and stage this binary under `dingo_cli_bin_dir`, which defaults to
`~/.dingo/bin/server-<cluster-name>`, so different clusters can use incompatible
CLI versions. Existing `dingocli.cfg` and legacy executables remain in place.
A changed executable is backed up under `<dingo_cli_bin_dir>/backups/dingo-<sha256>`
and restored on failed validation; a failed first executable at a new path is
removed. Staging can also run separately:

```bash
ansible-playbook -i "$INV" playbooks/04b_update_dingo_cli.yml
```

Cache/client workers use the universal **latest client** CLI, independently of
the `dingo-client` and `dingo-cache` release packages. Worker deployment uses
the existing controller file at `dingo_cli_latest_pkg_path` (default
`/root/dingofs/pkg/dingo-latest`) and copies it by content checksum.
`dingo_client_dingo_bin_local_src` points to this worker CLI
path. The downloader refuses to overwrite the manual server CLI, including via
a symlink. Cache deployment configures the command and PATH without rewriting
an existing `dingo.yaml`; `client_06_dingocli.yml` also configures `dingo.yaml`.
Updating only the CLI does not restart a running FUSE client.
`dingo_cli_download_enabled` defaults to **false**: no URL is contacted, and a
missing/empty controller source fails early with its path. To refresh the file
from `dingo_cli_download_url` once per play, explicitly pass
`-e dingo_cli_download_enabled=true`.
Check mode uses the cached controller binary and does not fetch the latest URL.

This is an Ansible-managed executable update; keep `dingo_cli_auto_upgrade: false`
so CLI invocations do not independently upgrade a server installation. An optional
`dingo_cli_download_checksum` pins the worker download checksum. Check mode runs the
read-only prerequisites and skips CLI/image downloads, registration and deployment;
it does not prove the release image is available in the registry.

### 4. Run individual phases

```bash
# Data disks (must precede 01_prepare)
ansible-playbook playbooks/01b_lvm_data.yml

# System preparation only
ansible-playbook playbooks/01_prepare.yml

# Performance tuning only
ansible-playbook playbooks/02_tune.yml

# RQLite database only
ansible-playbook playbooks/03_rqlite.yml

# Podman + Dingo CLI only
ansible-playbook playbooks/04_dingo_cli.yml

# DingoFS cluster deployment only
ansible-playbook playbooks/05_deploy_cluster.yml

# Check cluster status
ansible-playbook playbooks/99_status.yml
```

Twenty of the playbooks set `remote_user: root` explicitly. The rest inherit
`remote_user` from `ansible.cfg` (which defaults to `dingofs`, with `become: true`),
and most region inventories override that with `ansible_user: root` in
`group_vars/all.yml` anyway -- so in practice everything runs as root.

### TiKV-backed meta clusters: run the pre-flight first

A v5.2 MDS with `mds_storage_engine: tikv` keeps its metadata in TiKV, so a
healthy TiKV cluster is a hard prerequisite. TiKV itself is deployed and
upgraded with **tiup**, deliberately not by ansible (tiup owns the cluster
state machine and its rolling operations); what this repo provides is the
read-only check:

```bash
ansible-playbook -i <region>/hosts-meta.yml playbooks/tikv_verify.yml
```

It asserts that tiup lists the cluster, every component is `Up`, and PD
reports every store `Up`, then prints the PD endpoints and the exact
`mds_storage_engine` / `storage_url` lines for the site topology. It never
writes anything, so it is safe before any deploy and on a live cluster.
Sites whose metadata lives in dingo-store or rqlite do not need it.

The topology template supports both dingo-store and TiKV-backed, MDS-only
clusters. On existing hosts, `meta_add_cluster.yml` runs this read-only TiKV
verification on the admin before updating the CLI or deploying MDS. TiUP remains
responsible for PD/TiKV/TiDB lifecycle operations.

## DingoFS Cache Deployment

Dingo Cache is deployed independently from the meta server cluster, on a separate set of `cache_servers` nodes.

### Prerequisites

- Cache nodes with unused NVMe devices (for LVM setup)
- jemalloc source tarball on the control node (default: `/root/dingofs/pkg/jemalloc-5.3.0.tar.bz2`)
- dingo-cache binary on the control node (default: `/root/dingofs/pkg/dingo-cache`)

### 1. Configure cache inventory

Add your cache server IPs to `inventory/hosts.yml`:

```yaml
    cache_servers:
      hosts:
        cache-node-1:
          ansible_host: 10.0.0.10
        cache-node-2:
          ansible_host: 10.0.0.11
```

### 2. Customize cache variables

Edit `inventory/group_vars/all.yml` to set:
- `dingo_cache_mds_addrs` — MDS addresses from the meta server cluster
- `dingo_cache_cache_dirs` / `dingo_cache_log_dir` — must match your LVM mount layout
- `dingo_cache_ip_pattern` — network prefix for auto-detecting the listen IP
- `dingo_cache_cache_size_mb` — total cache size in MB
- `jemalloc_local_src` — path to jemalloc tarball on the control node

### 3. Run full cache deployment

```bash
# Dry run
ansible-playbook playbooks/cache_site.yml --check

# Full deployment
ansible-playbook playbooks/cache_site.yml
```

### 4. Run individual cache phases

```bash
# System preparation (basic tools, cpupower)
ansible-playbook playbooks/cache_01_prepare.yml

# LVM setup (detect NVMe, create VGs, format, mount)
ansible-playbook playbooks/cache_02_lvm.yml

# Performance tuning
ansible-playbook playbooks/cache_03_tune.yml

# Compile and install jemalloc
ansible-playbook playbooks/cache_04_jemalloc.yml

# Deploy dingo-cache service
ansible-playbook playbooks/cache_05_deploy.yml

# Check cache status
ansible-playbook playbooks/cache_99_status.yml
```

All cache playbooks connect with `remote_user: root`.

## DingoFS Client Deployment

Dingo Client (FUSE mount) is deployed independently from both meta server and cache deployments, on a separate set of `client_servers` nodes.

### Prerequisites

- jemalloc source tarball on the control node (default: `/root/dingofs/pkg/jemalloc-5.3.0.tar.bz2`)
- `dingo-client` binary on the control node (default: `/root/dingofs/pkg/dingo-client`)
- `dingo` CLI binary on the control node (default: `/root/dingofs/pkg/dingo`)

### 1. Configure client inventory

Add your client server IPs to `inventory/hosts.yml`:

```yaml
    client_servers:
      hosts:
        client-node-1:
          ansible_host: 10.0.0.20
        client-node-2:
          ansible_host: 10.0.0.21
```

### 2. Customize client variables

Edit `inventory/group_vars/all.yml` to set:
- `dingo_client_mds_addrs` — MDS addresses from the meta server cluster
- `dingo_client_fsname` — filesystem name to mount
- `dingo_client_mount_point` — where to mount the filesystem
- `dingo_client_cache_group` — cache group name (must match `dingo_cache_group_name`)
- FUSE/VFS tuning parameters as needed

### 3. Run full client deployment

```bash
# Dry run
ansible-playbook playbooks/client_site.yml --check

# Full deployment
ansible-playbook playbooks/client_site.yml
```

### 4. Run individual client phases

```bash
# System preparation (basic tools, cpupower)
ansible-playbook playbooks/client_01_prepare.yml

# Performance tuning
ansible-playbook playbooks/client_02_tune.yml

# Compile and install jemalloc
ansible-playbook playbooks/client_03_jemalloc.yml

# Deploy dingo-client FUSE mount service
ansible-playbook playbooks/client_04_deploy.yml

# Check client status
ansible-playbook playbooks/client_99_status.yml
```

All client playbooks connect with `remote_user: root`.

### Discover mounts without knowing filesystem names

`client_98_discover.yml` scans the selected nodes without reading `dingo_clients`,
the configured filesystem name, mountpoint or version suffix. It discovers
DingoFS mounts across versions, including manual mounts and bind mounts, and
reports stopped/failed client systemd services as well as running clients.

```bash
SITE=../ansible-dingofs-env/xn01/v5.2
ansible-playbook -i "$SITE/hosts.py" playbooks/client_98_discover.yml --limit NODE

# Scan every host in a supplied inventory, including hosts outside client_servers
ansible-playbook -i /path/to/hosts.yml playbooks/client_98_discover.yml \
  -e dingo_client_discover_hosts=all
```

The kernel's `/proc/self/mountinfo` is the source of actual mount records.
System systemd units (including installed inactive units) and `/proc` client
processes supply additional identity and service associations. Literal
`g_mnt`/`g_fsname` assignments in startup scripts and direct client startup
arguments help locate services whose clients are stopped. Arbitrary shell
expressions and symlinked scripts are left unresolved. Custom units are detected
by client names/descriptions, startup commands or a direct running client as their
main process; opaque wrappers may remain unassociated.

The terminal prints node/mount totals, findings first, and identical mount
layouts grouped with node counts and example hostnames. It shows at most 20
findings and 20 layouts; `-e dingo_client_discover_display_limit=50` changes that
limit. Per-host task output and the full play recap are suppressed for discovery.
Use `-v` to show normal task details. The `dingofs` stdout callback configured in
`ansible.cfg` preserves default output for other plays; use the same callback and
plugin directory when supplying a custom Ansible configuration.

Full records retain `mounts`, `services`, `processes`, `other_fuse_mounts` and
`warnings`. Service `mount_state` is `mounted`, `not_mounted`,
`different_filesystem` or `unknown`. Other FUSE mounts are listed separately;
they are not assumed to be DingoFS. Mounted records preserve filesystem type,
source, bind root, mount ID, read-only state and associated services/PIDs.
An empty successful scan is valid. `scan_status: partial`, `failed` and
`unreachable` distinguish missing evidence from a node with no client mounts.

The controller writes private reports for **this invocation's selected hosts**
to `reports/client-discovery/run-*/` and prints the exact directory. Terminal
limits do not truncate these artifacts:

| File | Contents |
| --- | --- |
| `summary.txt` | Compact terminal summary |
| `hosts.tsv` | Every node, scan status, layout group and counts |
| `mounts.tsv` | Every DingoFS mount with filesystem, path, service and PID |
| `issues.tsv` | Every finding, including scan failures and stopped/unmounted services |
| `layouts.tsv` | Every layout and its complete node membership |
| `report.json` | Full discovery evidence, totals, layouts and findings |

Scan failures and unreachable nodes are excluded from layout grouping, so they
cannot appear in the "no mounts" group. Partial scans retain their observed
layout and also appear in the findings table. Inactive services and unresolved
mount identity are findings to inspect, rather than proof of an application fault.

Override the report root with `-e dingo_client_discover_report_dir=/path/to/reports`.
Reports are also written in `--check` mode. Optional
`-e dingo_client_discover_strict=true` fails incomplete scans **after** saving the
report; it does not assert that discovered services are active. Each systemctl
query has a 15-second timeout, configurable with
`dingo_client_discover_command_timeout`.

Scanning changes no remote services or filesystems and does not run startup
scripts, `df`, directory traversal or filesystem I/O probes. `mounted` means
present in the kernel table; filesystem responsiveness and application I/O health
require a separate check. Mounts cover the scanner's current mount namespace;
clients found in other namespaces are marked `mount_namespace: other`, without
attributing their mountpoints to the host. Container-private mounts and per-user
systemd managers are outside this scan's scope. Inventory filtering and `--limit`
still determine which nodes are inspected.

### Client restart and skip reports

Client deployment and upgrade runs now print the controller-side report paths
at the end of execution. Reports cover every host, filesystem instance and
serial batch in that invocation:

- `reports/<run-id>/client-restart-skips-<run-id>.tsv`: a tab-separated list of
  deferred instances, with host, filesystem, mount point, service and reason.
- `reports/<run-id>/client-restart-report-<run-id>.json`: full instance outcomes,
  check-mode information, failed/unreachable hosts and run completion status.

The paths are absolute in the final output. Each invocation has its own private
directory and files, so concurrent runs cannot overwrite each other's reports.
Set `DINGO_CLIENT_REPORT_DIR=/path/on/controller` to change the report directory.
The callback is enabled in this repository's `ansible.cfg`; when using a custom
configuration, also enable `dingofs_client_report` and set `callback_plugins` to
this repository's `callback_plugins` directory.

```bash
DINGO_CLIENT_REPORT_DIR=/tmp/dingo-client-reports \
  ansible-playbook -i /path/to/site/hosts.py playbooks/client_04_deploy.yml
# After execution, use the exact TSV/JSON paths printed in the final summary.
```

| Status | Meaning |
| --- | --- |
| `skipped_busy_before_change` | Mount busy before configuration/upgrade; this instance's files and service were left unchanged |
| `restart_deferred_busy` | Restart deferred because the mount became busy; inspect `changed_on_disk` and `pending_restart` |
| `restart_deferred_unknown` | Restart deferred because usage could not be verified, for example missing `fuser` |
| `check_failed` / `restart_failed` / `verification_failed` | Operation failed; inspect the JSON error records and Ansible failure output |
| `completed` / `dry_run` | Finished or intentionally not applied; not counted as a busy skip |

An empty TSV does not prove every host succeeded: also check the JSON failure
and unreachable summaries, including hosts that failed before a filesystem
could be identified. Reports contain structured summaries, not full checker
output, container inspection data or inventory credentials. Writing reports
does not change the existing mount checks, force flags or failure behavior.

For `changed_on_disk: true` and `pending_restart: true`, wait for the mount to
be idle before completing the restart. An unchanged repeat of the ordinary
deployment does not necessarily retry a previously deferred restart; use the
upgrade entry point's checks or arrange a controlled manual restart. The report
is an execution snapshot and does not itself schedule or perform a restart.

## Meta Server Deployment Phases

| Phase | Playbook | Target | Description |
|-------|----------|--------|-------------|
| 1 | `01_prepare.yml` | All nodes | Create user, /etc/hosts, SSH keys, install packages |
| 2 | `02_tune.yml` | All nodes | CPU governor, ulimits, sysctl, hugepages |
| 3 | `03_rqlite.yml` | All nodes | RQLite database cluster (master first, then slaves) |
| 4 | `04_dingo_cli.yml` | All + admin | Podman on all nodes, Dingo CLI on admin node |
| 5 | `05_deploy_cluster.yml` | Admin node | Pull images, configure and deploy DingoFS cluster |

### Key Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `dingofs_user` | `dingofs` | System user for DingoFS |
| `dingofs_cluster_name` | `v5.2` | Cluster name identifier |
| `dingofs_container_image` | *(must set)* | DingoFS container image |
| `rqlite_version` | `v9.4.5` | RQLite version |
| `container_engine` | `podman` | Container engine |
| `tune_hugepages` | `1024` | Number of huge pages |
| `tune_swappiness` | `10` | VM swappiness |
| `tune_cpupower_idle_disable` | `3` | Disable CPU idle states with exit latency >= this many microseconds |
| `tune_disable_c1e` | `true` | Also disable C1E by state name on every online CPU, including 2 µs variants |

See `inventory/group_vars/all.yml` for all configurable variables.

### CPU idle tuning

The shared `tune` role inspects each online CPU's `cpuidle/state*/name`,
`latency` and `disable` attributes. It disables C1E by name and states at or
above the configured exit-latency threshold, writes only enabled target states,
and verifies the result. It does not depend on a fixed state index or the
presence of `cpupower`. Already-disabled states are never re-enabled, including
when `tune_disable_c1e` is set to false.

To run only this part of tuning on a selected cache node:

```bash
ansible-playbook -i /path/to/site/hosts.py playbooks/cache_03_tune.yml \
  --limit GPU-HOST --tags cpu_idle --check
ansible-playbook -i /path/to/site/hosts.py playbooks/cache_03_tune.yml \
  --limit GPU-HOST --tags cpu_idle
```

The same tag is available in `02_tune.yml` and `client_02_tune.yml`.
Check mode reports planned changes without writing CPU idle settings. Missing
CPUIdle interfaces or a missing named C1E state produce an explicit warning;
they do not prove that firmware C1E is disabled.

These are runtime OS settings. Reapply and verify them after reboot, CPU
hotplug or another policy manager resetting the values. BIOS Enhanced Halt/C1E
and automatic C1 promotion remain firmware-specific checks; this role does not
write MSRs, change boot arguments or install a boot-time service. The separate
[`check_performance.py`](scripts/check_performance.py) remains read-only.
See the [kernel CPUIdle interface](https://docs.kernel.org/admin-guide/pm/cpuidle.html)
and the [upstream idle-set semantics](https://github.com/torvalds/linux/blob/master/tools/power/cpupower/man/cpupower-idle-set.1).

### Cluster Architecture

```
┌─────────────────────────────────────────────────────┐
│                  DingoFS Cluster                    │
├──────────────┬──────────────┬───────────────────────┤
│   Node 1     │   Node 2     │   Node 3              │
│  (admin)     │              │                       │
├──────────────┼──────────────┼───────────────────────┤
│ RQLite       │ RQLite       │ RQLite                │
│ (master)     │ (slave)      │ (slave)               │
├──────────────┼──────────────┼───────────────────────┤
│ Coordinator  │ Coordinator  │ Coordinator           │
│ Store        │ Store        │ Store                 │
│ MDS x3       │ MDS x3       │ MDS x3                │
│ Executor     │              │                       │
├──────────────┼──────────────┼───────────────────────┤
│ Dingo CLI    │              │                       │
│ Podman       │ Podman       │ Podman                │
└──────────────┴──────────────┴───────────────────────┘
```

## Cache Deployment Phases

| Phase | Playbook | Description |
|-------|----------|-------------|
| 1 | `cache_01_prepare.yml` | Install basic packages, cpupower, development tools |
| 2 | `cache_02_lvm.yml` | Detect unused NVMe devices, create LVM VGs, format XFS, mount |
| 3 | `cache_03_tune.yml` | CPU governor, ulimits, sysctl, hugepages (reuses tune role) |
| 4 | `cache_04_jemalloc.yml` | Copy, compile, and install jemalloc with profiling support |
| 5 | `cache_05_deploy.yml` | Configure and start dingo-cache systemd service |

### Cache Key Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `dingo_cache_listen_port` | `11000` | Service listen port |
| `dingo_cache_group_name` | `dingofs-group` | Cache group name |
| `dingo_cache_cache_size_mb` | `3145728` | Cache size in MB (3TB) |
| `dingo_cache_mds_addrs` | *(must set)* | Comma-separated MDS addresses |
| `dingo_cache_ip_pattern` | `192.168.` | Network prefix for listen IP detection |
| `dingo_cache_rdma_max_connections` | `256` | Maximum RDMA connections per shard |
| `dingo_cache_rdma_heartbeat_interval_s` | `2` | RDMA heartbeat interval in seconds |
| `dingo_cache_rdma_idle_timeout_s` | `10` | RDMA idle timeout in seconds; at least 3 times the heartbeat interval |
| `jemalloc_version` | `5.3.0` | Jemalloc version to compile |

Configure Cache RDMA settings in the site's `group_vars/all.yml` or in
`host_vars`, rather than editing `dingo-cache.conf.j2`:

```yaml
dingo_cache_rdma_max_connections: 256
dingo_cache_rdma_heartbeat_interval_s: 2
dingo_cache_rdma_idle_timeout_s: 10
```

The role checks positive uint32 values and the timeout/heartbeat constraint
before changing host files. When `dingo_cache_use_rdma: false`, these checks
are skipped and RDMA-specific flags are omitted. These are Cache settings;
Client connection limits are configured separately. The connection limit is
per shard and affects the RDMA message-pool memory budget.

For an existing Cache deployment, apply configuration with
`cache_05_deploy.yml --limit <canary-host>` during the chosen rollout window;
changes notify a service restart. Verify the updated service and its membership
in the intended MDS cache group after the run, then continue in small batches.
`cache_06_upgrade.yml` only updates the binary and does not apply new settings.
When replacing a local template hotfix with this version, preserve any other
local changes and carry the intended values into site variables.

## IB NUMA locality

Cache and Client share one switch, enabled by default:

```yaml
dingofs_numa_require_ib: true
```

When RDMA is enabled, the IB NUMA node must be known. Automatic CPU selection,
forced allocation and retained cpusets must all stay on that node. Explicit
CPU/node overrides that disagree with the IB topology fail, even with `force`.
An old automatic cpuset on another node is recalculated; a valid local cpuset
is retained. Sibling mount exclusions and existing CPU ownership checks remain.

Cache and Client default `cpuset_on_insufficient` to `force`: prefer free
physical cores, then share the least-busy eligible cores when none are free.
Required IB locality still limits both passes to the IB node, and automatic
allocation still excludes sibling mounts' assigned cores. If there are too few
eligible physical cores even with sharing, deployment fails. Existing explicit
`fail` or `warn` settings in an inventory retain their priority; use
`-e dingo_cache_cpuset_on_insufficient=force` or
`-e dingo_client_cpuset_on_insufficient=force` to override them for a run.

Both startup scripts use `numactl --cpunodebind=<node> --preferred=<node>` by
default, before process initialization. `warn` or `pin_cpu=false` can omit
per-core pinning, but the process CPU range remains on the IB node. A required
locality deployment cannot disable numactl. Default memory policy targets the
same node but may spill; this switch does not guarantee every page is local,
and application-specific per-buffer memory policies can override the default.
Set `dingo_cache_numactl_membind` or `dingo_client_numactl_membind` to `true` only
for an intentionally sized strict local-memory deployment.

`dingo_cache_ib_node_mem_max_pct` and `dingo_client_ib_node_mem_max_pct` default
to **80**. While IB locality is required, this is an estimated memory-pressure
alert threshold, not a reason to move CPUs to another NUMA node. Disabling
`dingofs_numa_require_ib` restores memory-aware node selection and cross-node CPU
fallback; RDMA-disabled hosts also retain that fallback behavior.

Use a limited `cache_05_deploy.yml` / `client_04_deploy.yml` rollout to apply this
configuration. Changed startup scripts/cpusets can require service restarts;
existing client busy-mount checks remain in effect. Binary-only upgrade
playbooks do not apply this policy. Validate per-thread CPU affinity and actual
page placement with `/proc/<pid>/numa_maps` or `numastat -p`, not only the allowed
memory-node mask.

## Client Deployment Phases

| Phase | Playbook | Description |
|-------|----------|-------------|
| 1 | `client_01_prepare.yml` | Install basic packages, cpupower, development tools |
| 2 | `client_02_tune.yml` | CPU governor, ulimits, sysctl, hugepages (reuses tune role) |
| 3 | `client_03_jemalloc.yml` | Copy, compile, and install jemalloc with profiling support |
| 4 | `client_04_deploy.yml` | Deploy dingo-client FUSE mount systemd service |

### Client Key Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `dingo_client_fsname` | `alayanew` | Filesystem name to mount |
| `dingo_client_mount_point` | `/dingofs/data` | FUSE mount point |
| `dingo_client_port` | `12000` | VFS dummy server port |
| `dingo_client_mds_addrs` | *(must set)* | Comma-separated MDS addresses |
| `dingo_client_cache_group` | `dingofs-group` | Cache group name |
| `dingo_client_fuse_max_threads` | `512` | FUSE max threads |
| `dingofs_numa_require_ib` | `true` | Require both Cache and Client CPU / memory-policy targets to use the IB NUMA node |
| `dingo_client_numactl_enabled` | `true` | Apply process CPU and memory policy before initialization; required in IB-local mode |
| `dingo_client_numactl_node` | `""` | Follow the IB/cpuset node; an override must agree with IB while locality is required |
| `dingo_client_numactl_membind` | `false` | Prefer local memory by default; true selects strict membind |

## Standalone Playbooks

Not referenced by any `*_site.yml`; they only run when invoked explicitly.

| Playbook | Default target | Purpose |
|---|---|---|
| `config_podman_rootless.yml` | `client_servers` | Standalone entry point for the `podman_rootless` role: enables systemd linger for the service user, writes `storage.conf`, sets `XDG_RUNTIME_DIR`. The meta path does not need it -- `04_dingo_cli.yml` runs the same role for `meta_servers`. |
| `config_cron_cleanup.yml` | `all` | Deploys `cleanup_and_compression.sh` plus a daily cron job. Defaults to cleaning `dingo_cache_log_dir`; override `cron_cleanup_directories` for other layouts. The script requires at least one directory argument, so an empty list will not work. |
| `cache_02b_remap.yml` | `cache_servers` | Renumbers cache mounts by disk size (smallest -> `/mnt/disk1`). Check-only unless `-e remap_apply=true`. |
| `cache_06_upgrade.yml` | `cache_servers` | Replaces cache binaries and restarts the service. |
| `client_05_label.yml` | `client_servers` | Applies node labels. |
| `client_06_dingocli.yml` | `client_servers:cache_servers:!meta_servers:!admin` | Distributes the staged worker CLI, updates `~/.dingo/bin/dingo` by SHA1 comparison, and writes `~/.dingo/dingo.yaml`. Download is opt-in. Explicit admin targets use the manual server CLI in a cluster-specific binary directory. |
| `client_07_upgrade.yml` | `client_servers` | Replaces client binaries and restarts mounts. Does not create missing mounts -- that is `client_04_deploy.yml`. |

Override the target with `-e target_hosts=<group>`.

## Troubleshooting

```bash
# Test connectivity
ansible meta_servers -m ping

# Check a specific node. Use the absolute path: ~/.dingo/bin is only on PATH for
# login shells, and `become` runs a non-login shell.
ansible node-1 -m shell -a "/home/dingofs/.dingo/bin/dingo cluster status" --become-user dingofs

# Rqlite health (rqlite binds to ansible_host, not localhost)
ansible admin -m uri -a "url=http://{{ rqlite_master_host }}:4001/status"
```

Failures that are easy to misread:

- **`crun: sd-bus call: Permission denied` while a container starts.** Rootless
  podman has no systemd user session. `04_dingo_cli.yml` runs the
  `podman_rootless` role, which calls `loginctl enable-linger`; if this appears,
  check it actually ran (it is skipped when `podman_rootless_user` resolves to
  the wrong user). To fix an existing host without a full re-run, use
  `config_podman_rootless.yml -e target_hosts=<group>`.
- **`dingo fs ...` hangs for ~30s, then `Error-Code: 660000 / context deadline exceeded`.**
  `~/.dingo/dingo.yaml` is missing, so the CLI fell back to the default
  `mdsaddr=127.0.0.1:7400`. Run `client_06_dingocli.yml` against the admin node.
- **`Error-Code: 620002 / mkdir ... Permission denied` for `/mnt/diskN/dingofs-vX.Y`.**
  The version directory does not exist, or is not owned by the service user -- a
  freshly created XFS mount is `root:root 0755`. Set `lvm_data_subdir` so
  `01b_lvm_data.yml` creates and chowns it.
- **Data ends up on the system disk.** `/mnt/diskN` was never mounted;
  `dingofs_cluster` does not check.
- **`Error-Code: 410009 / cluster already exist`.** A previous run left the cluster
  registered. Existing registrations must have matching topology and healthy
  services. Incomplete registrations require explicit inspection and recovery;
  the role never retries initialization over an existing cluster.
- **`Error-Code: 100000 / init SQLite database failed`.** `~/.dingo/dingocli.cfg`
  still points at an old rqlite address. The role writes the config before
  invoking the CLI, so re-running `04_dingo_cli.yml` fixes it.
- **Output full of `…` and `[1A[J`.** The dingo CLI draws a progress spinner even
  when stdout is not a TTY. The deploy and status tasks strip it.

### Deployment safety checks

Set `dingofs_container_image` to the matching versioned server image. Digest or
custom tags require `dingofs_container_version` from the verified build metadata.
Registered clusters are checked for matching topology and health before initial
installation can proceed. Recover incomplete deployments explicitly.

New service accounts use a locked password by default. Set
`dingofs_password_hash` from a secret store when password authentication is
required. The controller key can be supplied through
`dingofs_controller_public_key` or `dingofs_controller_public_key_file`; set
`dingofs_install_controller_key: false` to disable its installation.

Existing rqlite units containing a literal `~/` data argument require verification
and migration of the actual database directory before redeployment. Configure an
absolute `rqlite_node_dir` and retain the database state during that migration.

Mount safety checks inspect Kubernetes namespaces beginning with `cci` by default.
The checker uses CRI when available and also inspects containerd via `ctr`.
If CRI is unavailable, `ctr` alone can provide OCI mount metadata and running
task PIDs. Kubernetes namespace filtering is preserved; standalone containerd
namespaces are checked without the Kubernetes filter. Hosts with no detected
runtime are checked through process mount namespaces. An active runtime with
no usable inspection interface, invalid metadata or unreadable namespaces stays
UNKNOWN and blocks reconfiguration.
Set `dingo_client_mount_check_strict: true` (or pass `--strict` to the checker) to
include all containers and other process mount namespaces. Deployment, upgrade,
manual restart helpers and automatic remount scripts honor the same setting.
Host process usage is still checked in both modes; an unavailable required check
returns 2 and prevents changes. Upgrades retain a per-instance restart marker
until the new mount has been verified.

A container mapping `/` to `/host` is not evidence that an uncreated child mount
is busy. The checker inspects its actual namespace at the corresponding child
path. Direct references and retained child mounts remain protected even after
the host path is detached. Unmounted directories sharing the root filesystem
are not treated as the same mounted filesystem. Filesystem names alone are not
used to associate independent mounts or services.

CPU detection runs in one Python process. `--self-exe` identifies the instance
whose existing allocation may be reused; `-S` remains accepted for command-line
compatibility. Preferred and fallback NUMA selection use one snapshot. Forced
allocation is computed only with `--force` and preserves sibling exclusions.

Cache and Client use the same CPU detector. It inspects every live user-process
thread's `Cpus_allowed_list` under `/proc/<pid>/task/<tid>/status`, including when
the leader is unbound and only worker threads are pinned. This covers Cache
processes launched with `--flagfile`: current thread affinity is checked rather
than rereading a configuration file that may have changed since startup.
Explicit `--cpuset` argv reservations and observed thread affinities are combined;
whole-NUMA allowed masks keep their existing non-exclusive interpretation.

Automatic selection and manual `-c` checks both reject occupied SMT siblings.
`--no-busy` disables load sampling only, not ownership checks. A disappearing
thread does not discard other threads' claims; unreadable or incomplete live
ownership fails with exit code 3, including under force policy. Conflict output
includes PID/TID samples. Collection remains a current snapshot, not a lock
against another deployment concurrently claiming cores. See the Linux
[per-thread proc interface](https://man7.org/linux/man-pages/man5/proc_pid_task.5.html).

### Uninstall selected client/cache instances

Use the same site inventory that deployed the instances and an explicit
`--limit`. Client selection comes from `dingo_clients`; cache selection uses
`dingo_cache_service_name` and `dingo_cache_base_dir`. There is no inferred
default client filesystem. Start with a read-only plan:

```bash
SITE=/path/to/site
ansible-playbook -i "$SITE/hosts.py" playbooks/client_08_uninstall.yml --limit GPU-HOST --check
ansible-playbook -i "$SITE/hosts.py" playbooks/cache_07_uninstall.yml --limit GPU-HOST --check
```

Remove `--check` to apply. By default this stops/disables the selected service,
removes its unit and installation files, and **retains cache data and logs**.
The deletion switches are independent:

| Variable | Default | Effect |
| --- | --- | --- |
| `dingo_uninstall_remove_data` | `false` | Delete this cache instance's deployed data directories; never delete a client's remote filesystem contents |
| `dingo_uninstall_remove_logs` | `false` | Delete this instance's deployed log directories |
| `dingo_uninstall_remove_installation` | `true` | Delete the verified version/instance installation directory |
| `dingo_uninstall_remove_mountpoint` | `true` | Remove an empty, detached client mountpoint using `rmdir` |
| `dingo_uninstall_unregister_cache` | `true` | After stopping cache, leave its exact member/IP/port from its configured MDS cache group |
| `dingo_uninstall_cli` | `dingo_client_dingo_bin_local_src` | Controller-side CLI used for cache registration queries and leave |

For an explicitly approved cache data/log purge:

```bash
ansible-playbook -i "$SITE/hosts.py" playbooks/cache_07_uninstall.yml --limit GPU-HOST \
  -e dingo_uninstall_remove_data=true -e dingo_uninstall_remove_logs=true
```

Uninstall checks the unit's startup path and deployed configuration before
changing anything. Shared directories, symlinked paths and mounted directory
trees are rejected. If retained data/logs are inside the installation directory,
set `dingo_uninstall_remove_installation=false`. Client usage must be idle and
unmounting must succeed normally; no force/lazy unmount is used. Other versions,
LVM/filesystem layouts, shared tools and MDS filesystem contents are preserved.

Private ownership records in `/var/lib/dingofs-uninstall` allow a later run to
purge retained data/logs even after installation files have been removed.
Repeating a completed uninstall is a no-op. `--check` performs ownership/usage
and registration queries and prints exact removal/retention paths, without
stopping services, unmounting or deleting files.

## Operational security and recovery

SSH host key verification is enabled. Add verified host keys to the controller's
`known_hosts` before deployment. For an isolated disposable lab, explicitly set
`ANSIBLE_HOST_KEY_CHECKING=False`; production inventories should keep verification.

CLI downloads remain disabled by default. Enabling `dingo_cli_download_enabled`
also requires `dingo_cli_download_checksum: "sha256:<trusted release hash>"`.
A moving `latest` URL alone is not reproducible. The role downloads to a private
candidate, checks its hash and `--version`, and installs it with a backup of the
previous file. A changed release needs a new trusted hash. Rqlite and jemalloc
archives are checked on every staging run, including local/offline archives;
version overrides must supply their own `*_download_checksum`.

Rqlite keeps the existing root service account, configured data directory
(default `/root/rqlite/node`) and authentication behavior. Grafana credentials
also keep their existing behavior; this change does not require credentials,
rotate passwords or migrate database data. Monitor image versions remain pinned
by default. Inventory overrides, including `:latest`, are accepted.

Client deployment (`client_04_deploy.yml`) and upgrade (`client_07_upgrade.yml`)
run in batches of 20% of the selected hosts (`serial: "20%"`);
`client_site.yml` imports that deployment
entry point. Cache deployment remains suitable for selecting hosts with `--limit`;
its upgrade playbook also keeps `serial: 1`.

Cache upgrades and client deployments persist `.restart-required` before changing
service files. An interrupted or busy restart retains this marker for the next
run, including when all files already match. Busy client instances are deferred
individually so other instances can proceed. The `.bak` upgrade backup retains
the oldest saved binary; it is not necessarily the immediately preceding build.

`lvm_data_repair: true` activates and remounts an existing formatted data LV and
repairs a missing fstab entry. It never formats a managed LV. Missing LVs,
unknown filesystems, conflicting mounts or nonempty target directories require
manual recovery. Cache allocation skips occupied VG names, mountpoints and
fstab targets and refuses to hide files in an unmounted target directory.

A limited `01_prepare.yml --limit ...` run exchanges SSH keys only among active
meta hosts in that play. Run preparation against all intended meta peers to
establish their full mutual trust; limiting a play does not modify excluded
nodes.

THP policy is persisted through `/etc/tmpfiles.d/dingofs-thp.conf`. CPU idle/C1E
settings remain runtime settings as documented above. Cache/client units set
`LimitCORE=infinity` and `LimitNPROC=4194304`; the shared core directory uses
mode `1777`. Client OOM eligibility stays at the normal score 0, configurable
with `dingo_client_oom_score_adjust`; setting -1000 makes it ineligible for OOM
killing and requires a site-specific memory policy.
