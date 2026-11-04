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

`client_06_dingocli.yml` defaults to `target_hosts: client_servers`, so against
a meta-only inventory it matches **zero hosts and silently does nothing**. Pass
`-e target_hosts=admin` when running it for a meta deployment. (The same trap
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

The CLI update downloads the latest executable from the configured main/latest
URL, validates its version, backs up a changed executable under `.dingo/bin/backups/dingo-<sha256>`,
and replaces it atomically. It preserves `dingocli.cfg` and restores the previous
binary if a post-install validation fails. The update can also run separately:

```bash
ansible-playbook -i "$INV" playbooks/04b_update_dingo_cli.yml
```

This is an Ansible-managed executable update; keep `dingo_cli_auto_upgrade: false`
so CLI invocations do not independently upgrade a shared installation. An optional
`dingo_cli_download_checksum` pins the candidate checksum. Check mode runs the
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

See `inventory/group_vars/all.yml` for all configurable variables.

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
| `dingo_client_numactl_enabled` | `true` | Start the client under `numactl --cpunodebind=<node> --membind=<node>` |
| `dingo_client_numactl_node` | `""` | Force the numactl NUMA node; empty = the instance's cpuset node |

## Standalone Playbooks

Not referenced by any `*_site.yml`; they only run when invoked explicitly.

| Playbook | Default target | Purpose |
|---|---|---|
| `config_podman_rootless.yml` | `client_servers` | Standalone entry point for the `podman_rootless` role: enables systemd linger for the service user, writes `storage.conf`, sets `XDG_RUNTIME_DIR`. The meta path does not need it -- `04_dingo_cli.yml` runs the same role for `meta_servers`. |
| `config_cron_cleanup.yml` | `all` | Deploys `cleanup_and_compression.sh` plus a daily cron job. Defaults to cleaning `dingo_cache_log_dir`; override `cron_cleanup_directories` for other layouts. The script requires at least one directory argument, so an empty list will not work. |
| `cache_02b_remap.yml` | `cache_servers` | Renumbers cache mounts by disk size (smallest -> `/mnt/disk1`). Check-only unless `-e remap_apply=true`. |
| `cache_06_upgrade.yml` | `cache_servers` | Replaces cache binaries and restarts the service. |
| `client_05_label.yml` | `client_servers` | Applies node labels. |
| `client_06_dingocli.yml` | `client_servers` | Installs the dingo binary into `~/.dingo/bin` and writes `~/.dingo/dingo.yaml`. Also needed on the meta admin node. |
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
  registered. Re-running is safe -- the role treats this as success.
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

Mount safety checks cover all Kubernetes namespaces and return 2 when required
checks cannot complete. Client deployment, upgrade and automatic remount paths
use this result to prevent changes when usage is busy or unknown. Upgrades retain
a per-instance restart marker until the new mount has been verified.

CPU detection runs in one Python process. `--self-exe` identifies the instance
whose existing allocation may be reused; `-S` remains accepted for command-line
compatibility. Preferred and fallback NUMA selection use one snapshot. Forced
allocation is computed only with `--force` and preserves sibling exclusions.
