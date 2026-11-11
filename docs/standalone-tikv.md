# Single-node DingoFS 5.2 with an independently managed TiKV backend

The existing `dingofs_cluster` role supports a TiKV-backed topology with one
MDS (`mds_replicas: 1` and a single `meta_servers` host). The new
`playbooks/standalone_site.yml` provides a small first-install entry point that
reuses the existing Podman, server CLI and cluster deployment roles. TiKV may
run on another host; standalone describes the DingoFS MDS deployment, not a
requirement to colocate its database.

A new backend can be provisioned separately using `playbooks/standalone_tikv.yml`.
That playbook invokes **TiUP**, which owns the PD/TiKV binaries, generated
configuration, systemd units and cluster state. An existing TiUP backend can
instead be verified and reused without running the provisioning playbook.

## Site configuration

Keep the inventory, version variables and TiUP topology in the private env
repository, following the existing site's structure. A two-host example:

```yaml
# hosts.yml
all:
  children:
    meta_servers:
      hosts:
        demo-mds:
          ansible_host: 192.0.2.10
          dingofs_service_address: 192.0.2.10
    admin:
      hosts:
        demo-mds: {}
    tikv_servers:
      hosts:
        demo-tikv:
          ansible_host: 192.0.2.11
```

```yaml
# group_vars/all.yml (key settings; select a verified image per site)
dingofs_site_version: v5.2
dingofs_cluster_name: v5.2-standalone
dingofs_container_image: your-registry/dingofs:v5.2-your-build
dingofs_user: root
dingofs_home: /root
container_engine: podman
podman_enable_socket: false
dingo_cli_engine: podman
dingo_cli_db_url: sqlite:///root/.dingo/data/dingocli.db
dingo_cli_ssh_private_key_file: /root/.ssh/id_ed25519_dingofs_standalone
dingofs_mds_storage_engine: tikv
dingofs_mds_storage_url: list://192.0.2.11:2379
mds_replicas: 1
mds_base_port: 690
tikv_pd_addr: 192.0.2.11:2379
tikv_min_stores: 1
tikv_cluster_name: dingofs-demo-tikv
tikv_version: v8.5.8
tikv_topology_file: /path/to/site/tikv/topology-tikv.yaml
```

The lightweight MDS entry point currently uses rootful Podman and root's local
SQLite CLI registry, as in the existing cci-int deployment. The controller
connects with its normal SSH user and sudo. It creates a dedicated key **on the
MDS host** for the existing CLI's SSH connection to that same host; no controller
private key is uploaded. `dingofs_service_address` lets CLI/MDS use the internal
IP when Ansible connects through a floating/NAT address.

The `podman` role can optionally use an explicitly selected signed APT repository
and pinned package versions (`podman_apt_repository_url`, signing key URL/checksum,
and `podman_apt_packages`). Sites using the default OS package source retain their
existing installation behavior. The demo-cci profile selects Podman 6.1.3 and its
matching container tools on Ubuntu 22.04, whose distribution package is 3.4.4.

## Deployment

Prepare the compatible **Linux** server `dingo` binary on the Ansible controller,
using the same `dingo_cli_pkg_path` contract as other server workflows. The
standalone entry point does not download an unversioned CLI. Install the repo's
collections with `ansible-galaxy collection install -r requirements.yml`.

```bash
# New backend only; an existing backend skips this step.
ansible-playbook -i /path/to/site/hosts.yml playbooks/standalone_tikv.yml --check
ansible-playbook -i /path/to/site/hosts.yml playbooks/standalone_tikv.yml

# TiUP verification runs first, then the existing CLI deploys only one MDS.
ansible-playbook -i /path/to/site/hosts.yml playbooks/standalone_site.yml --check
ansible-playbook -i /path/to/site/hosts.yml playbooks/standalone_site.yml \
  -e dingo_cli_pkg_path=/path/to/compatible-linux-dingo
```

Check mode does not install packages, pull images, or register/start services.
The full MDS check requires the backend to have been deployed. To inspect only
the MDS plan before the backend exists, add `--skip-tags tikv` to `--check`.

The backend playbook runs TiUP locally on its selected host with `--ssh=none`.
It bootstraps TiUP from the official HTTPS archive only when absent (a mirror
URL and checksum can be configured), then uses normal `cluster deploy/start`.
It does not rewrite PD/TiKV units or generated TOML files itself. Existing
registrations are health-checked; changed topology/version inputs require a
normal TiUP maintenance operation. A failed/partial registration is preserved,
so recovery is explicit rather than repeating deployment automatically.

## Small-host profile

Use one PD and one TiKV, `replication.max-replicas: 1`, placement rules off, and
`ignore_exporter: true` on PD/TiKV/TiDB in the site-owned TiUP topology. Include
one TiDB with an explicit budget. Omit TiUP Prometheus, Grafana and Alertmanager;
deploy the existing DingoFS monitor separately. TiUP `resource_control` can cap PD
at 512 MiB / 50% CPU and TiKV at 2048 MiB / 100% CPU. A 256-MiB TiKV block cache,
1536-MiB internal memory target, and small RocksDB/thread pools suit a small demo.
These are examples for a small host. Larger sites should override the limits
and cache sizes in their env configuration. The demo-cci 64-GiB hosts use an
8-GiB/4-CPU MDS, a 2-GiB/1-CPU PD, and a 16-GiB/8-CPU TiKV with a 6-GiB block
cache and 12-GiB internal memory target; the MDS otherwise retains the image's
default worker pools and cache settings.

For MDS, `dingofs_mds_extra_config` adds site-selected flags to the existing
topology template. Set `bthread_concurrency: 4`, small GC/quota/stat worker pools,
`mds_clean_threshold_count: 65536`, and
`mds_filesystem_hash_mds_num_default: 1`. The entry point applies persistent
Podman limits after the existing CLI starts its registered MDS: by default
1024 MiB and one CPU. For older Podman without `update`, a persistent override
on only that container's systemd scope provides the limits. These hard limits take effect after initial startup;
the reduced thread/cache settings are present before the MDS starts.

These are resource ceilings, not measured idle usage. Watch actual memory/disk
use. TiKV's configured `raftstore.capacity` is its reported capacity, not a
filesystem quota. Use local ext4/XFS directories and preserve both PD and TiKV
data. One MDS and one TiKV replica provide no node-level redundancy.

## Management

Use the existing `dingo cluster status`, `dingo mds status`, and `dingo fs`
commands with the cluster-specific server CLI. Use `tiup cluster display`,
`edit-config`, `reload`, and `upgrade` for the independently managed backend.
No RQLite, DingoStore, executor, machine-wide tuning, disk formatting, cache,
client or filesystem is installed by the MDS entry point.

After the server is healthy, create a filesystem with `--mdsnum 1` and a
**dedicated** RADOS pool or S3 bucket. Historical Ceph credentials and existing
filesystem metadata are not copied into a new deployment.

References: [TiUP topology](https://docs.pingcap.com/tidb/v8.5/tiup-cluster-topology-reference/),
[TiKV configuration](https://docs.pingcap.com/tidb/v8.5/tikv-configuration-file/).

## Required TiDB and DingoFS monitor

The standalone topology must include one TiDB on the same backend host, with explicit memory/CPU limits and `ignore_exporter: true`. New deployments pass it to TiUP together with PD/TiKV. Existing deployments can add only that TiDB with `standalone_tikv.yml -e tikv_standalone_add_tidb=true`; the role requires the saved PD/TiKV topology to remain identical and rejects partially registered additions. Standalone health checks require all three backend components. MDS preflight rejects a missing or unhealthy TiDB.

Deploy the DingoFS monitor separately after MDS using the existing `playbooks/06_deploy_monitor.yml`. Configure `dingo_monitor_data_dir` on an existing filesystem, smaller Prometheus retention, and optional `dingo_monitor_resource_limits` by role (`cpus`, `memory_mb`). These Podman limits apply after the existing CLI starts the containers; other sites default to no additional ceilings.

The demo profile enables `dingo_monitor_initialize_grafana`. It waits for Prometheus and Grafana APIs, selects the provisioned default Prometheus datasource, finishes the CLI server-dashboard UID substitution, and checks that Grafana can query Prometheus and load the repaired dashboard. Only the CLI's exact `Error: failed to fetch prometheus info` timeout is eligible for this recovery; missing containers, bad credentials and unrelated errors still fail. Set the Grafana API credentials in site variables when changed.
