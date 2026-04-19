# CLAUDE.md

Guide Claude Code (claude.ai/code) in repo.

## Project Overview

Ansible playbooks deploy DingoFS distributed file system. Three pipelines: meta server cluster, cache nodes, client (FUSE mount) nodes.

## Common Commands

```bash
# Meta server full deploy
ansible-playbook playbooks/meta_site.yml

# Cache full deploy
ansible-playbook playbooks/cache_site.yml

# Client full deploy
ansible-playbook playbooks/client_site.yml

# Run single phase (e.g. tune cache nodes)
ansible-playbook playbooks/cache_03_tune.yml

# Dry run any playbook
ansible-playbook playbooks/<playbook>.yml --check

# Syntax check
ansible-playbook playbooks/<playbook>.yml --syntax-check

# Test connectivity
ansible meta_servers -m ping
ansible cache_servers -m ping
ansible client_servers -m ping

# Check status
ansible-playbook playbooks/99_status.yml
ansible-playbook playbooks/cache_99_status.yml
ansible-playbook playbooks/client_99_status.yml
```

## Architecture

Three pipelines, different host groups:

**Meta Server** (`meta_servers`, 3-node HA): prepare → tune → rqlite → podman+cli → deploy cluster
- Phase 1 run as `root`; phases 2-5 use `dingofs` user with `become: true`
- RQLite master/slave pattern (master start first no `--join`)
- Cluster deploy from single `admin` node via `dingo` CLI

**Cache** (`cache_servers`): prepare → lvm → tune → jemalloc → deploy dingo-cache
- All phases run as `root`
- LVM role auto-detect unused NVMe devices, create `dingo_cache_N` VGs
- dingo-cache listen on port detected via `dingo_cache_ip_pattern` network match

**Client** (`client_servers`): prepare → tune → jemalloc → deploy dingo-client
- All phases run as `root`
- FUSE mount via systemd service, use `numactl` + `jemalloc` LD_PRELOAD
- Mount script cleanup existing mountpoint before remount

## Conventions

**Playbook naming**: `{pipeline}_{NN}_{phase}.yml` — meta use bare `NN_phase.yml`, cache/client prefix `cache_`/`client_`. Master playbook: `{pipeline}_site.yml`.

**Variable prefixes**: `dingofs_` (meta cluster), `dingo_cache_` (cache), `dingo_client_` (client), `rqlite_` (database), `tune_` (performance), `lvm_` (LVM setup), `jemalloc_` (build config).

**Role structure**: Each role has `tasks/main.yml`, `defaults/main.yml`, `handlers/main.yml`. Templates in `templates/`. No `vars/` or `files/` dirs.

**Global vars**: All configurable values in `inventory/group_vars/all.yml`. Role defaults mirror for standalone use.

**Shared roles**: `tune`, `cache_prepare`, `jemalloc` reused across cache and client pipelines.

**Idempotency patterns**:
- Check-before-set (tune role read current value, apply only if different)
- `stat` + `when` guards (jemalloc skip if lib exists, LVM skip if VGs exist)
- Config ID preserved across re-runs (dingo_cache read existing UUID from config)

**Binaries**: Copied from control node paths (`/root/dingofs/pkg/`), not downloaded. Key vars: `*_local_src`, `*_bin_local_src`.

**OS support**: Meta server target RHEL/CentOS only (`yum`). Cache/client `cache_prepare` role handle both RHEL (`yum`/`kernel-tools`) and Debian (`apt`/`linux-tools`).

## Inventory Groups

`meta_servers` — 3 meta server nodes (also contain `rqlite_master`, `rqlite_slaves`, `admin` subgroups)
`cache_servers` — dingo-cache nodes (independent)
`client_servers` — dingo-client FUSE mount nodes (independent)