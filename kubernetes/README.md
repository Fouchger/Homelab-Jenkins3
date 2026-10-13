# Kubernetes platform lifecycle

This repository is the rebuild recipe for the homelab Kubernetes platform.
Jenkins runs reviewed infrastructure and recovery workflows. Flux owns
ongoing in-cluster application configuration after the cluster is bootstrapped.

## Ownership boundaries

| Layer | Owner | Rebuild and day-2 approach |
| --- | --- | --- |
| Proxmox VMs, disks, and tagged VLAN 80 NICs | OpenTofu | Review a plan, then apply from Jenkins. Keep state durable and backed up. |
| Talos OS, machine configuration, and control-plane bootstrap | Talos API with `talosctl`, invoked by Jenkins | Generate secrets once and store them outside Git/state. Render per-node config from a versioned inventory. Bootstrap exactly one control-plane node once; subsequent runs must be safe to repeat. |
| Cluster networking | Cilium, installed before Flux | Pin the chart and values in Git. Confirm the chosen pod/service CIDRs do not overlap homelab VLANs. |
| Applications and cluster add-ons | Flux | Bootstrap Flux to `kubernetes/clusters/homelab`; Git is the desired state for normal operation. |
| NFS export on OpenMediaVault | OMV management interface (currently configured) | Dedicated `Kubernetes` shared folder on TB4a is exported read/write to `192.168.80.0/24` with root squashing. Preserve existing SMB shares. |
| Persistent volumes | Kubernetes storage configuration reconciled by Flux | Start with NFS for shared files and suitable simple application data. Select an application-aware backup/restore approach for databases; an NFS snapshot alone is not a database backup. |
| Secrets | Infisical plus SOPS encryption for Git-held Kubernetes secrets | Keep Talos machine secrets and private keys out of Git, OpenTofu state, and build logs. |

## Delivery sequence

1. Confirm Proxmox host placement, VM/ISO storage, CPU/RAM/disk sizing, VLAN 80
   addresses, and an API virtual IP outside the confirmed DHCP pool
   `192.168.80.10-192.168.80.199`.
2. Add static DHCP reservations for the five VM MAC addresses in VLAN 80
   through the router workflow, using `.101`-`.105` from the private inputs.
   Keep `.200` out of DHCP. Then run the OpenTofu plan and approved apply to
   create the Talos VMs.
3. Generate and protect Talos machine secrets. Boot the pinned Talos Image
   Factory image, apply control-plane/worker machine configuration, and verify
   all three control planes before bootstrapping the cluster once.
4. Install and verify Cilium, then configure ingress and service IP allocation
   to match the MikroTik routing/firewall design.
5. Verify the dedicated OMV NFS location and export from VLAN 70; verify
   node connectivity and an end-to-end persistent-volume mount before relying
   on it for workloads. For a Sonarr/Radarr/Lidarr/qBittorrent migration, expose
   the existing media/download folders separately where needed; use the new
   Kubernetes folder for cluster-specific files. Keep app databases on a
   storage path tested for their database locking behavior, and back them up
   with application-aware procedures.
6. Bootstrap Flux and reconcile the cluster add-ons, storage classes, policies,
   and first workloads from Git.
7. Add scheduled/read-only health checks and manually approved Jenkins
   workflows for Talos upgrades, Kubernetes upgrades, and tested recovery.

## Recovery expectations

Rebuilding should recreate VMs from OpenTofu, reapply Talos configuration from
protected inputs, bootstrap only when the cluster is genuinely new, and let
Flux restore declarative in-cluster configuration. Stateful data recovery is a
separate tested procedure using OMV and application-consistent backups. Never
destroy/recreate a live cluster as a routine upgrade path.

The current code implements the Proxmox Talos VM definitions and the Jenkins
plan/approval/apply workflow. Talos installation and bootstrap, DHCP
reservation automation, Cilium, Flux reconciliation, and workload manifests
are subsequent implementation stages.
