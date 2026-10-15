# Kubernetes platform lifecycle

This repository is the rebuild recipe for the homelab Kubernetes platform.
Jenkins runs reviewed infrastructure and recovery workflows. Flux owns
ongoing in-cluster application configuration after the cluster is bootstrapped.

## Ownership boundaries

| Layer | Owner | Rebuild and day-2 approach |
| --- | --- | --- |
| Proxmox VMs, disks, and tagged VLAN 80 NICs | OpenTofu | Review a plan, then apply from Jenkins. Keep state durable and backed up. |
| Talos OS, machine configuration, and control-plane bootstrap | Talos API with `talosctl`, invoked by Jenkins | Inventory is `kubernetes/talos/cluster.json`. Generate secrets once and store them outside Git/state. Bootstrap exactly one control-plane node once; subsequent runs must be safe to repeat. |
| Cluster networking and ingress | Cilium, reconciled by Job 009 and Flux | Cilium Gateway API and L2 announcements use the VLAN 80 NIC. Direct service IPs are reserved from `192.168.80.210-192.168.80.219`. |
| Applications and cluster add-ons | Flux | Bootstrap Flux to `kubernetes/clusters/homelab`; Git is the desired state for normal operation. |
| NFS export on OpenMediaVault | OMV management interface (currently configured) | Dedicated `Kubernetes` shared folder on TB4a is exported read/write to `192.168.80.0/24` with root squashing. Preserve existing SMB shares. |
| Persistent volumes | Kubernetes storage configuration reconciled by Flux | OMV `192.168.70.20:/export/Kubernetes` is the initial NFS target. The `omv-nfs-retain` StorageClass creates one directory per claim and retains data when a claim is removed. Select an application-aware backup/restore approach for databases; an NFS snapshot alone is not a database backup. |
| Secrets | Infisical plus SOPS encryption for Git-held Kubernetes secrets | Keep Talos machine secrets and private keys out of Git, OpenTofu state, and build logs. |

The MikroTik configuration job now includes the narrow NFSv4 path required by
this storage target: TCP/2049 from `192.168.80.0/24` to `192.168.70.20`. The
rule appears in the read-only review and is applied only after the usual Job
004 approval and encrypted router backup. Job 004 also checks that
`192.168.80.210-192.168.80.219` does not overlap DHCP pools or existing leases,
ARP entries, or router DNS records before approving the direct service range.
The StorageClass pins NFSv4.1 over TCP so it does not depend on NFSv3's
additional rpcbind/mountd ports.

## Delivery sequence

1. Confirm Proxmox host placement, VM/ISO storage, CPU/RAM/disk sizing, VLAN 80
   addresses, and an API virtual IP outside the confirmed DHCP pool
   `192.168.80.10-192.168.80.199`.
2. Add static DHCP reservations for the five VM MAC addresses in VLAN 80
   through the router workflow, using `.101`-`.105` from the private inputs.
   Keep `.200` out of DHCP. Then run the OpenTofu plan and approved apply to
   create the Talos VMs.
3. Run Job 004's read-only review and approved apply before installing Talos.
   Confirm it reports the service IP range as clear, verifies the VLAN 80
   reservations, and opens VLAN 80-to-OMV NFSv4 TCP/2049 after the encrypted
   router backup.
4. Run Jenkins Job 009 using the inventory in `kubernetes/talos/cluster.json`.
   It verifies the DHCP-reserved node IPs are all fresh Talos maintenance-mode
   VMs and that the API VIP is unused, stores Talos secrets in Infisical,
   applies the pinned machine configuration, and bootstraps etcd once on
   `talos-cp-01`.
5. Job 009 installs the pinned
   Gateway API v1.6.1 CRDs and enables Cilium Gateway API and L2 announcements
   as part of configuration, before checking all five nodes Ready. On an
   existing cluster, an approved Job 009 apply rolls the Cilium agents and may
   briefly interrupt cluster networking.
6. Run Job 010 to install Flux and configure Git synchronization. It uses the
   existing KUBECONFIG in Infisical, requires all five inventory nodes Ready,
   and defaults to a read-only preflight. APPLY requires a separate Jenkins
   approval. The initial Git sync is public read-only and does not need a Git
   write token.
7. Flux installs the NFS CSI driver and creates the opt-in `omv-nfs-retain`
   StorageClass for `192.168.70.20:/export/Kubernetes`. Before using it for
   workloads, verify an end-to-end claim and mount from a Talos node. The OMV
   export is root-squashed, so the shared-folder filesystem permissions must
   allow the CSI controller to create per-claim directories and the workload
   UID/GID to write its files. Do not use this class for database data until
   database locking and application-aware backup/restore are validated. For a
   Sonarr/Radarr/Lidarr/qBittorrent migration, expose
   the existing media/download folders separately where needed; use the new
   Kubernetes folder for cluster-specific files. Keep app databases on a
   storage path tested for their database locking behavior, and back them up
   with application-aware procedures.
8. Flux creates the Cilium LoadBalancer pool and a Cilium Gateway at
   `192.168.80.210`. Direct LAN routes use the same gateway IP; add the
   corresponding Technitium DNS records after deciding the app hostnames.
   Public routes will use Cloudflare Tunnel and edge HTTPS, targeting the
   gateway from inside the cluster. The tunnel token must stay out of Git and
   will be supplied from Infisical when the connector deployment is added.
9. Add app HTTPRoutes and the Cloudflare Tunnel connector after choosing public
   hostnames and storing its tunnel token in Infisical. Local DNS can point
   those names directly to `192.168.80.210`; public DNS remains managed by
   Cloudflare Tunnel. Existing router and Technitium changes remain with Jobs
   004 and 006; Flux owns only in-cluster components and configuration.
10. Add scheduled/read-only health checks and manually approved Jenkins
   workflows for Talos upgrades, Kubernetes upgrades, and tested recovery.

## Recovery expectations

Rebuilding should recreate VMs from OpenTofu, reapply Talos configuration from
protected inputs, bootstrap only when the cluster is genuinely new, and let
Flux restore declarative in-cluster configuration. Stateful data recovery is a
separate tested procedure using OMV and application-consistent backups. Never
destroy/recreate a live cluster as a routine upgrade path.

The current code implements the Proxmox Talos VM definitions and the Jenkins
plan/approval/apply workflow, Job 009 for guarded Talos installation, initial
cluster bootstrap, and Cilium, and Job 010 for approved Flux installation and
initial Git synchronization. It also includes the Flux-managed NFS CSI driver
and retained StorageClass, Cilium service IP pool, L2 announcement policy, and
HTTP Gateway manifests. DHCP reservations remain a manual prerequisite. The
NFS claim and write check, app routes, Cloudflare Tunnel connector, and local
DNS records still need implementation or live verification. Public TLS is
terminated at Cloudflare. The Talos pipeline reuses the VMs from Job 008 and
never recreates them.
