# ansible-dingofs

Ansible playbooks and roles for deploying and managing DingoFS (Ding Open File System) meta server clusters, Dingo Cache nodes, and Dingo Client (FUSE mount) nodes on CentOS/RHEL/Rocky Linux (cache/client nodes also support Ubuntu/Debian).

## Prerequisites

- **Control node**: macOS or Linux with Ansible >= 2.12 installed
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
│   ├── meta_site.yml                 # Full meta server deployment (all phases)
│   ├── 01_prepare.yml           # System prep: user, hosts, SSH, tools
│   ├── 02_tune.yml              # Performance tuning
│   ├── 03_rqlite.yml            # RQLite database cluster
│   ├── 04_dingo_cli.yml         # Podman + Dingo CLI installation
│   ├── 05_deploy_cluster.yml    # DingoFS cluster deployment
│   ├── 99_status.yml            # Cluster health check
│   ├── cache_site.yml           # Full cache deployment (all phases)
│   ├── cache_01_prepare.yml     # Cache: basic tools, cpupower
│   ├── cache_02_lvm.yml         # Cache: NVMe LVM setup
│   ├── cache_03_tune.yml        # Cache: performance tuning
│   ├── cache_04_jemalloc.yml    # Cache: compile & install jemalloc
│   ├── cache_05_deploy.yml      # Cache: dingo-cache service
│   ├── cache_99_status.yml      # Cache: health check
│   ├── client_site.yml          # Full client deployment (all phases)
│   ├── client_01_prepare.yml    # Client: basic tools, cpupower
│   ├── client_02_tune.yml       # Client: performance tuning
│   ├── client_03_jemalloc.yml   # Client: compile & install jemalloc
│   ├── client_04_deploy.yml     # Client: dingo-client FUSE service
│   └── client_99_status.yml     # Client: health check
└── roles/
    ├── common/                  # User, /etc/hosts, SSH keys, packages
    ├── tune/                    # CPU, ulimits, sysctl, hugepages
    ├── rqlite/                  # RQLite install + systemd cluster
    ├── dingo_cli/               # Dingo CLI binary + config
    ├── podman/                  # Podman container engine
    ├── dingofs_cluster/         # Topology, image pull, cluster deploy
    ├── cache_prepare/           # Cache node system preparation
    ├── lvm_cache/               # NVMe LVM detection + setup
    ├── jemalloc/                # Compile jemalloc from source
    ├── dingo_cache/             # Dingo-cache service deployment
    └── dingo_client/            # Dingo-client FUSE mount service
```

## Dingo Meta Deployment

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

```bash
# Dry run (check mode)
ansible-playbook playbooks/meta_site.yml --check

# Full deployment
ansible-playbook playbooks/meta_site.yml
```

### 4. Run individual phases

```bash
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

Only `01_prepare.yml` connects with `remote_user: root`. The other phase playbooks use the default `remote_user` from `ansible.cfg` (`dingofs`) together with `become: true`.

## Dingo Cache Deployment

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

## Dingo Client Deployment

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
| `dingofs_cluster_name` | `v5.1` | Cluster name identifier |
| `dingofs_container_image` | `harbor.zetyun.cn/dingofs/dingofs:v5.1-b484ef2` | DingoFS container image |
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
| `jemalloc_version` | `5.3.0` | Jemalloc version to compile |

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

## Troubleshooting

```bash
# Test connectivity
ansible meta_servers -m ping

# Check specific node
ansible node-1 -m shell -a "dingo cluster status" --become-user dingofs

# View rqlite cluster health
ansible admin -m uri -a "url=http://localhost:4001/status"
```
