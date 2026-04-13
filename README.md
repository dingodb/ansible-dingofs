# ansible-dingofs

Ansible playbooks and roles for deploying and managing DingoFS (Ding Open File System) meta server clusters on CentOS/RHEL/Rocky Linux.

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
│   ├── site.yml                 # Full deployment (all phases)
│   ├── 01_prepare.yml           # System prep: user, hosts, SSH, tools
│   ├── 02_tune.yml              # Performance tuning
│   ├── 03_rqlite.yml            # RQLite database cluster
│   ├── 04_dingo_cli.yml         # Podman + Dingo CLI installation
│   ├── 05_deploy_cluster.yml    # DingoFS cluster deployment
│   └── 99_status.yml            # Cluster health check
└── roles/
    ├── common/                  # User, /etc/hosts, SSH keys, packages
    ├── tune/                    # CPU, ulimits, sysctl, hugepages
    ├── rqlite/                  # RQLite install + systemd cluster
    ├── dingo_cli/               # Dingo CLI binary + config
    ├── podman/                  # Podman container engine
    └── dingofs_cluster/         # Topology, image pull, cluster deploy
```

## Quick Start

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
ansible-playbook playbooks/site.yml --check

# Full deployment
ansible-playbook playbooks/site.yml
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

## Deployment Phases

| Phase | Playbook | Target | Description |
|-------|----------|--------|-------------|
| 1 | `01_prepare.yml` | All nodes | Create user, /etc/hosts, SSH keys, install packages |
| 2 | `02_tune.yml` | All nodes | CPU governor, ulimits, sysctl, hugepages |
| 3 | `03_rqlite.yml` | All nodes | RQLite database cluster (master first, then slaves) |
| 4 | `04_dingo_cli.yml` | All + admin | Podman on all nodes, Dingo CLI on admin node |
| 5 | `05_deploy_cluster.yml` | Admin node | Pull images, configure and deploy DingoFS cluster |

## Key Variables

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

## Cluster Architecture

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

## Troubleshooting

```bash
# Test connectivity
ansible meta_servers -m ping

# Check specific node
ansible node-1 -m shell -a "dingo cluster status" --become-user dingofs

# View rqlite cluster health
ansible admin -m uri -a "url=http://localhost:4001/status"
```
