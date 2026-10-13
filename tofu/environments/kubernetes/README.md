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
3. Provide `PROXMOX_VE_ENDPOINT` and `PROXMOX_VE_API_TOKEN` through the process
   environment. The API token needs the minimum permissions required to audit
   the target node and datastore, allocate an ISO template, modify VM settings,
   and create VMs. Do not put credentials in `.tfvars`.
4. Initialize and inspect a plan. VM destruction is guarded by both Proxmox
   protection and OpenTofu `prevent_destroy`; intentional rebuilds need an
   explicit reviewed change to those guards.

For Jenkins, install the private variables file at
`/var/lib/jenkins-agent/homelab/kubernetes/cluster.tfvars` on the
`homelab-automation` agent. The pipeline reads Proxmox token values from
Infisical and expects the existing Jenkins read identity and Proxmox host
credentials. The pipeline stores local backend state beside the private inputs
at `terraform.tfstate`, outside the ephemeral Jenkins workspace; protect and
back up this directory. Jenkins job `008 - Kubernetes Infrastructure` is
seeded by the controller installer; the plan is read-only unless `APPLY` is
selected, and apply then requires explicit approval.

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
