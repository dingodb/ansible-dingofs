# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Ansible playbooks for deploying DingoFS distributed file system. Three independent deployment pipelines: meta server cluster, cache nodes, client (FUSE mount) nodes.

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

Three independent pipelines targeting different host groups:

**Meta Server** (`meta_servers`, 3-node HA): prepare → tune → rqlite → podman+cli → deploy cluster
- Phase 1 runs as `root` directly; phases 2-5 use `dingofs` user with `become: true`
- RQLite uses master/slave pattern (master starts first without `--join`)
- Cluster deployed from single `admin` node using `dingo` CLI

**Cache** (`cache_servers`): prepare → lvm → tune → jemalloc → deploy dingo-cache
- All phases run as `root`
- LVM role auto-detects unused NVMe devices, creates `dingo_cache_N` VGs
- dingo-cache listens on port detected via `dingo_cache_ip_pattern` network match

**Client** (`client_servers`): prepare → tune → jemalloc → deploy dingo-client
- All phases run as `root`
- FUSE mount via systemd service, uses `numactl` + `jemalloc` LD_PRELOAD
- Mount script handles existing mountpoint cleanup before remount

## Conventions

**Playbook naming**: `{pipeline}_{NN}_{phase}.yml` — meta uses bare `NN_phase.yml`, cache/client prefix with `cache_`/`client_`. Master playbook: `{pipeline}_site.yml`.

**Variable prefixes**: `dingofs_` (meta cluster), `dingo_cache_` (cache), `dingo_client_` (client), `rqlite_` (database), `tune_` (performance), `lvm_` (LVM setup), `jemalloc_` (build config).

**Role structure**: Each role has `tasks/main.yml`, `defaults/main.yml`, `handlers/main.yml`. Templates in `templates/`. No `vars/` or `files/` dirs used.

**Global vars**: All configurable values in `inventory/group_vars/all.yml`. Role defaults mirror these for standalone use.

**Shared roles**: `tune`, `cache_prepare`, `jemalloc` are reused across cache and client pipelines.

**Idempotency patterns**:
- Check-before-set (tune role reads current value, applies only if different)
- `stat` + `when` guards (jemalloc skips if lib exists, LVM skips if VGs exist)
- Config ID preserved across re-runs (dingo_cache reads existing UUID from config)

**Binaries**: Copied from control node paths (`/root/dingofs/pkg/`), not downloaded. Key vars: `*_local_src`, `*_bin_local_src`.

**OS support**: Meta server targets RHEL/CentOS only (`yum`). Cache/client `cache_prepare` role handles both RHEL (`yum`/`kernel-tools`) and Debian (`apt`/`linux-tools`).

## Inventory Groups

`meta_servers` — 3 meta server nodes (also contains `rqlite_master`, `rqlite_slaves`, `admin` subgroups)
`cache_servers` — dingo-cache nodes (independent)
`client_servers` — dingo-client FUSE mount nodes (independent)
