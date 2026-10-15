# OpenMediaVault rebuild and configuration record

This file records the verified OpenMediaVault configuration used by the
Kubernetes cluster and the parts Jenkins can recreate. It is a service
configuration recipe, not a disk image or a substitute for backing up the NAS
data.

## Current verified baseline

| Setting | Value |
| --- | --- |
| Hostname | `omv01.fouchger.uk` |
| OpenMediaVault | `8.5.6-1` (observed in the Workbench dashboard on 2026-10-15) |
| Management VLAN 30 address | `192.168.30.20` |
| Storage VLAN 70 address | `192.168.70.20` |
| Kubernetes data filesystem label | `TB4a` |
| OMV shared folder | `Kubernetes`, relative path `Kubernetes` on `TB4a` |
| NFS client | `192.168.80.0/24` |
| NFS access | Read/write |
| NFS options | `sync,no_subtree_check,no_root_squash` |
| Kubernetes mount | NFSv4.1 `192.168.70.20:/Kubernetes` over TCP/2049 |

The export is dedicated to the Kubernetes VLAN. `no_root_squash` allows the
NFS CSI controller to create per-claim directories, but it also gives root on
an allowed client root-level file access inside that share. Do not reuse this
export for untrusted clients. NFS does not authenticate with an OMV username
and password; access is based on client network/export rules and filesystem
UID/GID. The secrets `/omv/OMV_USER` and `/omv/OMV_USER_PASSWORD` are the OMV
Workbench administrator login used by Jenkins to configure the service.

The MikroTik rule for Kubernetes to OMV is maintained separately by Job 004:
VLAN 80 to `192.168.70.20`, TCP/2049 only. NFSv4.1 avoids NFSv3's extra
rpcbind/mountd ports. Flux owns the Kubernetes CSI driver and StorageClass;
Job 011 owns the OMV NFS service/export.

The current OMV configuration also has existing SMB shares. Job 011 does not
read, replace, or modify SMB settings. Inventory those shares and their data
paths before treating this document as a complete record of every service on
the NAS.

## Rebuild procedure

1. Restore the OMV guest using the Proxmox backup/recovery procedure. Keep its
   existing VM/CT identity, NIC MAC addresses, VLAN 30 and VLAN 70 connections,
   and DHCP reservations so the existing addresses remain reachable. This
   repository does not yet provision the OMV guest or its operating system.
2. Restore or reconnect the data disk/filesystem without formatting it. Make
   sure OMV mounts the intended filesystem and reports its label as `TB4a`.
   Job 011 refuses to run if that filesystem is missing or ambiguous.
3. Install and update OMV, set the hostname, and restore network connectivity
   on VLANs 30 and 70. Keep the Workbench reachable at the URL supplied to
   Job 011. No Jenkins action changes OMV networking because a bad interface
   change could cut off the management connection.
4. Create or restore an OMV Workbench administrator account and set
   `/omv/OMV_USER` and `/omv/OMV_USER_PASSWORD` in Infisical. Grant the Jenkins
   read identity permission to read those two secrets. These are Workbench
   credentials; they are not NFS client credentials.
5. In Jenkins run **011 - OpenMediaVault NFS Setup** with `APPLY` unchecked.
   Review that the unique mounted `TB4a` filesystem and planned
   `Kubernetes` export are correct. For HTTPS, supply its certificate-valid URL
   as `OMV_URL`; Python verifies the certificate using the agent's trust
   store. The current Workbench URL is HTTP. Until HTTPS is configured, check
   `ALLOW_INSECURE_HTTP` only on the trusted management LAN; this sends the
   Workbench login over that LAN without transport encryption.
6. Rerun Job 011 with `APPLY` checked and approve the displayed plan. It
   enables NFS, ensures NFSv4.1 is enabled while retaining other protocol
   versions, creates the shared-folder metadata/directory only if missing,
   reconciles the read/write export for `192.168.80.0/24`, and applies OMV's
   `fstab`, shared-folder and NFS modules through the supported OMV RPC
   interface. It never formats or wipes storage.
7. Run Job 004's read-only review and approved apply if the MikroTik NFS rule
   is missing. Run Job 010 only after the Talos cluster is healthy; Flux then
   restores the NFS CSI driver and `omv-nfs-retain` StorageClass.
8. Verify storage with the temporary `nfs-smoke-test` namespace, a PVC using
   `omv-nfs-retain`, and a writer Pod mounting the claim at `/data`. Wait for
   the PVC to become `Bound` and the Pod to become `Running`, then read
   `/data/test.txt` from the Pod. The successful 2026-10-15 test wrote and
   read `NFS-mount-works`. Keep this as an operational smoke check; it is not
   yet a tracked Jenkins stage or repository manifest.

## Jenkins Job 011

The pipeline uses the OpenMediaVault JSON-RPC interface used by the Workbench.
It discovers the filesystem by label and resolves the mounted filesystem to
OMV's configured mountpoint reference; it does not hardcode the filesystem's
UUID. If the `Kubernetes` folder is absent, the job creates it at the
`Kubernetes` relative path on `TB4a`. If a folder with that name points to a
different filesystem or path, or if `TB4a` cannot be identified exactly once,
the job stops before changing anything.

NFS settings are reconciled via `NFS.getSettings`/`NFS.setSettings`, the export
via `NFS.getShareList`/`NFS.setShare`, and OMV changes are applied with
`Config.applyChanges`. Generated `/etc/exports` and `/etc/fstab` content is
never edited directly. The pipeline has a read-only plan, an explicit
`APPLY` parameter, and a Jenkins approval gate. It applies only `fstab`,
`sharedfolders`, and `nfs` modules.

## Not yet captured or automated

The repository does not yet contain a verified OMV guest provisioning recipe,
boot-disk layout, data-disk identifiers/RAID layout, filesystem formatting or
mount recovery, full OMV configuration backup, complete SMB/user/share
inventory, or network interface recovery. Those must be documented from the
current Proxmox/OMV configuration and backed up before claiming a full NAS
rebuild is automated. The pipeline deliberately avoids guessing storage
devices or touching existing media data.
