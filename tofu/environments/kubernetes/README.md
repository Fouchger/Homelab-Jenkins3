# Talos Kubernetes VMs

This OpenTofu environment manages only Proxmox VM resources. Talos machine
configuration, cluster bootstrap, Cilium, Flux, and Kubernetes workloads are
separate lifecycle steps. Talos secrets and kubeconfig must never enter this
environment's state.

The network defaults build on the existing MikroTik `VLAN80-K8S` design:

- VLAN ID: `80`
- IPv4 subnet and gateway from the router baseline: `192.168.80.0/24` and
  `192.168.80.1`
- DHCP pool (confirmed by the operator): `192.168.80.10-192.168.80.199`
- Proxmox trunk in the router baseline: `ether2`

VM MAC addresses and proposed node IPs are exported for static DHCP
reservations. The example reserves `.101` through `.105` from the existing
pool. The proposed API VIP `.200` is outside the pool and is not provisioned by
this OpenTofu stack. Verify all six addresses are unused, then create the
static leases and protect the VIP from DHCP allocation through the router
configuration workflow before bootstrapping Talos. The router baseline is not
applied by this stack.

## Before use

1. Copy `cluster.tfvars.example` to the ignored `cluster.tfvars` on the
   automation agent. It pins Talos v1.13.10's default Image Factory `metal`
   ISO; the checksum is calculated from that HTTPS download because the public
   Image Factory checksum route returned HTTP 402. Review VM IDs, MAC addresses,
   Proxmox node, bridge, datastore, and sizing against the live host before use.
2. Ensure the selected ISO-capable datastore is available to every target VM
   node, or use a separate accessible ISO datastore per PVE node.
3. Use `pve01.fouchger.uk` as the Proxmox API hostname. Job `006 - DNS
   Deployment and Router Sync` maintains its DNS record in Technitium and
   MikroTik at `192.168.20.10`, so the Jenkins agent can resolve the Proxmox endpoint. Job
   `002 - Proxmox Access Setup` ensures that hostname is covered by a trusted
   Let's Encrypt certificate using the Cloudflare DNS token in `/cloudflare`;
   Proxmox renews the ACME certificate automatically. The Jenkins agent
   verifies the certificate chain and hostname before each Kubernetes
   plan/apply. Run Job 006 first if this DNS record is missing.
4. Provide `PROXMOX_VE_ENDPOINT` (using `pve01.fouchger.uk`) and
   `PROXMOX_VE_API_TOKEN` through the process environment. The API token needs
   the minimum permissions required to audit the target node and datastore,
   allocate an ISO template, modify VM settings, and create VMs. Do not disable
   certificate verification or put credentials in `.tfvars`.
5. Use Jenkins Job 008 for plans and applies. Ordinary builds reject VM
   deletion/replacement and reject plans that update multiple existing VMs at
   once. To recreate the existing VMs with the corrected
   VirtIO SCSI controller, select exactly one `RECREATE_NODE` per build, select
   `APPLY`, review the plan, and approve it. Jenkins targets only that VM,
   temporarily clears Proxmox protection for the selected VM, and restores
   protection after replacement. Wait for the selected node to return healthy
   before running another recreation build. This keeps the other VMs running;
   it does not itself drain Kubernetes workloads or prove the cluster can
   tolerate the selected node being offline. Do not run the stack directly
   with OpenTofu outside this guarded pipeline.

For Jenkins, review or edit the complete non-secret cluster configuration in
the `CLUSTER_TFVARS` build parameter before starting the job. The pipeline
writes that parameter to a temporary workspace file and removes it when the
build completes. On the first build after the Jenkins parameter form is
updated, the tracked example is used automatically if the new parameter is not
available yet; that refresh build is plan-only, and apply is blocked until the
configuration parameter is available. Proxmox token values still come from Infisical.
The pipeline stores local backend state at
`/var/lib/jenkins-agent/homelab/kubernetes/terraform.tfstate`, outside the
ephemeral Jenkins workspace; protect and back up this directory. Jenkins job
`008 - Kubernetes Infrastructure` is seeded by the controller installer; the
plan is read-only unless `APPLY` is selected, and apply then requires explicit
approval of the exact displayed plan. Proxmox API TLS is preflighted before
planning so a hostname or trust failure blocks the build before the approval
step. The `PROXMOX_API_HOSTNAME` build parameter defaults to
`pve01.fouchger.uk` and must match the certificate SAN.

The local backend is an initial single-operator backend. Jenkins overrides its
default path with the durable agent path above. Local backend locking is
provided, but this design is still tied to the single Jenkins agent; back up
the state directory and migrate to a shared remote backend before using
multiple operators or unattended applies.

## High availability note

The supplied Proxmox screenshots show one host, `pve01`, with 20 logical CPUs,
31.11 GiB RAM, and current host memory use of 9.87 GiB. The host view also shows
1.93 GiB of swap in use. The example starts with three 3 GiB control planes
and two 3 GiB workers (15 GiB allocated total); keep this conservative until
the workloads and memory pressure are understood. The local-lvm view shows an
852.33 GiB thin pool with 217.60 GiB used (about 634.73 GiB available). The
example disk allocations total 224 GiB, which fits the displayed free space;
still account for the other guests' growth before applying.

Three control-plane VMs and two workers on this single host provide node-level
redundancy, not host-level availability: a `pve01` outage takes down the
entire cluster. The workers are sized at 3 GiB each to keep total new VM
memory at 15 GiB; monitor host memory and swap before adding workloads or
increasing VM memory.

## Later stages

- Create DHCP reservations for the five VM MAC addresses in VLAN 80; the API
  VIP `.200` is confirmed unused and is outside the DHCP pool.
- Generate machine secrets outside OpenTofu state; apply Talos config with
  `talosctl`; bootstrap exactly once.
- Install Cilium before relying on Kubernetes scheduling.
- Bootstrap Flux to `kubernetes/clusters/homelab`.
- The dedicated OMV `Kubernetes` folder on TB4a and its read/write NFS export
  for VLAN 80 are configured. Verify NFS write permissions from a Kubernetes
  node before relying on it for workloads.
