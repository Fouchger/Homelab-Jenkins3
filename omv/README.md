# OpenMediaVault rebuild and configuration record

This file records the verified OpenMediaVault configuration used by the
Kubernetes cluster and Job 011's rebuild/restore workflow. The pipeline
captures a password-redacted inventory and, after a clean OMV OS install,
re-registers the existing filesystems and restores OMV data services. This is
configuration recovery, not a backup of NAS data.

## Current verified baseline

| Setting | Value |
| --- | --- |
| Proxmox guest | VMID `22000`, name `omv01`, node `pve01` |
| Hostname | `omv01.fouchger.uk` |
| OpenMediaVault | `8.5.6-1` (observed in the Workbench dashboard on 2026-10-15) |
| Management VLAN 30 address | `192.168.30.20` |
| Storage VLAN 70 address | `192.168.70.20` |
| Current default routes | `192.168.30.1` static on VLAN 30; `192.168.70.1` via DHCP on VLAN 70 with metric `100` |
| Network rebuild policy | Preserve the current setup for now; changing the preferred route to `192.168.70.1` is deferred |
| Kubernetes data filesystem label | `TB4a` (`/dev/sde`, BTRFS; UUID `2e2b5958-abf6-4272-93c2-9b198aa9e67f`) |
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
Job 011 owns the OMV NFS service/exports. The existing SMB shares remain
enabled for current clients.

The live OMV storage screen on 2026-10-15 showed five separate BTRFS
filesystems and six shared-folder definitions:

| Label | Current device | Filesystem UUID | Shared-folder definition |
| --- | --- | --- | --- |
| `TB10a` | `/dev/sdb`, Proxmox `scsi1`, serial `WP00WBAP` | `cd3bca8a-831d-4432-adc0-92e2aedf8569` | `TB10a` |
| `TB10b` | `/dev/sdc`, Proxmox `scsi2`, serial `WP014X13` | `c13be974-e9e3-4ffa-b249-568670e928c2` | `TB10b` |
| `TB5a` | `/dev/sdd`, Proxmox `scsi3`, serial `WCJ6V5MV` | `86fcd23a-1b7f-487f-a0eb-bb16849debdf` | `TB5a` |
| `TB4a` | `/dev/sde`, Proxmox `scsi4`, serial `WCK0DD4S` | `2e2b5958-abf6-4272-93c2-9b198aa9e67f` | `TB4a`, `Kubernetes` |
| `TB16a` | `/dev/sdf`, Proxmox `scsi5`, serial `ZYD83XZA` | `b408b5f4-1f6d-496d-b7ad-7a19aaf5fb5d` | `TB16a` |

The six shared folders include five SMB share definitions (`TB4a`, `TB5a`,
`TB10a`, `TB10b`, `TB16a`) and the NFS-only `Kubernetes` folder. The current
UI showed SMB `Public=No`; the full SMB service/share settings still need to
be captured from the RPC inventory before they can be reproduced accurately.
The UI showed two OMV user accounts and no custom groups. Passwords and
account recovery data are not part of the rebuild inventory.

These `/dev/sdX` names are current guest device names, not safe Proxmox disk
identifiers. Never use them to decide which virtual disk to attach or format.
The Proxmox Hardware page for VM 22000 was inspected read-only on 2026-10-15.
It confirms this disk and NIC layout:

| Proxmox device | Current backing device | Size / configuration | Role |
| --- | --- | --- | --- |
| `scsi0` | `local-lvm:vm-22000-disk-0` | 32 GiB, `iothread=1`, `ssd=1` | OMV system disk |
| `scsi1` | `/dev/disk/by-id/ata-ST10000VN000-3AK101_WP00WBAP` | 9314 GiB, `backup=0` | `TB10a`, UUID `cd3bca8a-831d-4432-adc0-92e2aedf8569` |
| `scsi2` | `/dev/disk/by-id/ata-ST10000VN000-3AK101_WP014X13` | 9314 GiB, `backup=0` | `TB10b`, UUID `c13be974-e9e3-4ffa-b249-568670e928c2` |
| `scsi3` | `/dev/disk/by-id/ata-ST5000LM000-2AN170_WCJ6V5MV` | 4883770584 KiB, `backup=0` | `TB5a`, UUID `86fcd23a-1b7f-487f-a0eb-bb16849debdf` |
| `scsi4` | `/dev/disk/by-id/ata-ST4000LM024-2AN17V_WCK0DD4S` | 3907018584 KiB, `backup=0` | `TB4a`, UUID `2e2b5958-abf6-4272-93c2-9b198aa9e67f`; Kubernetes NFS data |
| `scsi5` | `/dev/disk/by-id/ata-ST16000NM002H-3KW133_ZYD83XZA` | 14902 GiB, `backup=0` | `TB16a`, UUID `b408b5f4-1f6d-496d-b7ad-7a19aaf5fb5d` |
| `net0` | MAC `BC:24:11:FD:D3:2F` | `vmbr0`, VLAN tag `30`, firewall enabled | Management network `192.168.30.20/24` |
| `net1` | MAC `BC:24:11:97:08:39` | `vmbr0`, VLAN tag `70`, firewall enabled | Storage network `192.168.70.20/24` |
| `ide2` | `local:iso/openmediavault_8.0.7-amd64.iso` | CD-ROM | Current installer ISO |

The VM is configured with 4 GiB RAM, 2 CPU cores, SeaBIOS, machine type `q35`,
and a VirtIO SCSI single controller. It starts at boot, with boot order
`scsi0, ide2, net0`, QEMU Guest Agent enabled, and Proxmox protection enabled.
The `scsi0` disk is the only disk on Proxmox local-lvm and is the system disk.
The OMV guest currently sees the five data disks as `/dev/sdb` through
`/dev/sdf`; the pasted Proxmox and guest inventory now confirms the slot,
physical serial, and filesystem UUID mapping above. Job 011 also maps these
using OMV's QEMU SCSI slot identity (`drive-scsiN`), because the guest sees
virtual disk serials rather than the physical host serials. For an OMV ISO installation, detach `scsi1`
through `scsi5` from the VM configuration before booting the installer, then
reattach those exact host devices in their original slots after installation.
Detaching a passthrough device from the guest must not wipe, initialize, or
format its physical disk.

## Rebuild procedure

1. Before replacing OMV, run **011 - OpenMediaVault Rebuild and Restore** with
   `OPERATION=INVENTORY`. Download the successful build's `omv-inventory.json`
   artifact. In Infisical, create/update `/omv/OMV_RESTORE_INVENTORY` with the
   complete JSON text and grant the Jenkins read identity permission to read
   it. This snapshot excludes passwords, private keys, and email addresses;
   it preserves public SSH keys for account recovery.
2. Set `/omv/OMV_USER` and `/omv/OMV_USER_PASSWORD` to the Workbench account
   used after reinstall. For every other captured local user, create
   `/omv/user-passwords/OMV_PASSWORD_<username>` with that user's replacement
   password. Give the Jenkins read identity read access to both secret paths.
   NFS itself does not authenticate using OMV usernames/passwords.
3. Save the current Proxmox VM configuration and arrange a backup of the
   `scsi0` system disk. Shut down VM 22000. Detach `scsi1` through `scsi5`
   while retaining their by-id host paths and original slots. Leave only the
   32 GiB `scsi0` system disk and installer ISO visible. The OMV ISO installer
   is interactive and consumes the selected system disk. OMV is not supported
   in an LXC/container.
4. Install OMV onto `scsi0`. Once powered off, reattach each physical disk to
   its original SCSI slot. Do not initialize, create filesystems, or format
   those disks.
5. Set hostname, restore VLAN 30/70 addresses and routes at the local console,
   and create the Workbench administrator account. Preserve current route
   behavior; the planned VLAN 70 gateway preference is deferred. Ensure the
   Workbench is reachable at `OMV_URL`. Jenkins leaves networking unchanged.
6. Run Job 011 with `OPERATION=RESTORE`, `APPLY` unchecked, and a reachable
   `OMV_URL`. Review all five disk serial/slot/UUID matches and the restore
   plan. The job checks that every required replacement password exists before
   it offers approval. Use a trusted HTTPS URL if configured; otherwise enable
   `ALLOW_INSECURE_HTTP` only on the trusted LAN.
7. Rerun with `OPERATION=RESTORE`, `APPLY` checked, and approve. Job 011
   registers only existing filesystems by UUID, restores shared folders,
   SMB/NFS definitions, local users/groups and applies OMV configuration. It
   never creates/formats filesystems or changes Proxmox hardware/networking.
   Then run `OPERATION=MEDIA` with `APPLY` checked and approve to ensure the
   five existing data shares are exported to VLAN 80, even if the saved
   pre-rebuild inventory predates those exports.
8. Run Job 004's read-only review and approved apply if the MikroTik NFS rule
   is missing. Run Job 010 only after the Talos cluster is healthy; Flux then
   restores the NFS CSI driver and `omv-nfs-retain` StorageClass.
9. Verify storage with the temporary `nfs-smoke-test` namespace, a PVC using
   `omv-nfs-retain`, and a writer Pod mounting the claim at `/data`. Wait for
   the PVC to become `Bound` and the Pod to become `Running`, then read
   `/data/test.txt` from the Pod. The successful 2026-10-15 test wrote and
   read `NFS-mount-works`. Keep this as an operational smoke check; it is not
   yet a tracked Jenkins stage or repository manifest.

## Jenkins Job 011 — OpenMediaVault Rebuild and Restore

With `OPERATION=INVENTORY`, Job 011 exports physical disk identities,
mounted filesystem details, OMV mountpoint definitions, shared folders,
network interface definitions, users and groups, NFS settings and exports,
SMB settings and shares, and an allowlisted Proxmox VM 22000 hardware config
to `omv-inventory.json`. It removes email, password, token, and private-key fields
before archiving the artifact. This is a read-only capture. The artifact
contains private network, account, storage and share metadata; its visibility
follows the Jenkins permissions on Job 011 and its build artifacts.
The inventory matches each Proxmox passthrough disk to OMV's disk and
filesystem records using the guest-visible SCSI slot identity, with physical
serial matching as an additional path where available. The output reports the
match count. Resolve any missing match before reattaching or mounting those
drives during a rebuild; `/dev/sdX` order alone is not a safe match.
The artifact must be promoted to Infisical secret `/omv/OMV_RESTORE_INVENTORY`
before replacing the OS disk. Jenkins artifacts are not automatically loaded
by a later build.

The capture needs the Jenkins Infisical read identity to read
`/proxmox/automation/PVE_API_TOKEN_ID` and `PVE_API_TOKEN_SECRET`, and the
Proxmox API token to have `VM.Audit` access to `/vms/22000`. The token is only
used for an HTTPS GET of the VM configuration. The pipeline does not change
Proxmox.

With `OPERATION=RESTORE`, the read-only plan verifies all five fixed filesystem
UUIDs, labels, physical disk serials and SCSI slots; checks expected share
definitions and UID/GID conflicts; and confirms replacement passwords exist
for captured non-admin accounts. The approved apply registers only existing
filesystems through `FileSystemMgmt.setMountPoint`, then restores shared-folder,
SMB, NFS, user and group records through OMV RPC. It never calls
filesystem creation, formatting, growing, deletion, or Proxmox mutation
methods. It leaves network configuration alone. Restore should first be
exercised against cloned disks, not the only production copy.

With `OPERATION=NFS`, the separate approved path only reconciles the
Kubernetes NFS service and export. With `OPERATION=MEDIA`, Job 011 adds NFSv4
exports for the existing `TB4a`, `TB16a`, `TB10a`, `TB10b`, and `TB5a` shared
folders, read/write for `192.168.80.0/24`, with `sync,no_subtree_check,root_squash`.
It does not change the SMB service/share records, share contents, ownership,
network, or local users. Root squashing means application processes should use
the numeric UID/GID that already owns the files on OMV. The CIFS fstab lines
map the client-visible owner to `1000:1000`; confirm that this matches server
ownership before enabling app writes. The exports are reachable by any client
on VLAN 80, so keep that VLAN restricted to trusted infrastructure.

Flux defines static, retained PV/PVC pairs in namespace `media` for each NFS
export. They mount the existing share root and do not create a subdirectory or
copy data. Use only the claims needed by an application; Sonarr and Radarr can
mount the same media claim when their container UID/GID and path layout are
configured consistently. Keep app configuration/database claims separate.

The pipeline uses the OpenMediaVault JSON-RPC interface used by the Workbench.
It discovers the filesystem by label and resolves the mounted filesystem to
OMV's configured mountpoint reference; it does not hardcode the filesystem's
UUID. If the `Kubernetes` folder is absent, the job creates it at the
`Kubernetes` relative path on `TB4a`. If a folder with that name points to a
different filesystem or path, or if `TB4a` cannot be identified exactly once,
the job stops before changing anything.

NFS settings are reconciled via `NFS.getSettings`/`NFS.setSettings`, the export
via `NFS.getShareList`/`NFS.setShare`, and OMV changes are applied with
`Config.applyChanges`. Generated `/etc/exports`, `/etc/fstab`, and Samba
configuration are never edited directly. The pipeline has a read-only plan,
an explicit `APPLY` parameter, and a Jenkins approval gate. NFS mode applies
only `fstab`, `sharedfolders`, and `nfs`; RESTORE also applies `samba`.

## Automation boundary

Job 011 automates OMV configuration restore only after an operator installs
the OS and recovers network access. It does not install Debian/OMV, change
Proxmox hardware, configure VLAN interfaces/routes, or back up media data.
Keep a separate backup of the actual data. Do not import `config.xml`
wholesale into a clean host.

Passwords cannot be recovered from the inventory. Create a replacement
password secret at `/omv/user-passwords/OMV_PASSWORD_<username>` for each
captured non-admin account. The job preserves account UIDs, custom group GIDs,
and membership, and verifies user primary GIDs match OMV's default `users`
group. Network settings remain a manual
local-console recovery step, and the current VLAN 30/VLAN 70 route behavior is
left unchanged.

## Official references

- [OMV 8 installation choices](https://docs.openmediavault.org/en/8.x/installation/)
  documents supported installation methods and states that LXC/container
  installation is unsupported.
- [OMV ISO installation](https://docs.openmediavault.org/en/latest/installation/via_iso.html)
  warns to disconnect non-OS drives and that the selected OS disk's existing
  data will be lost.
- [OMV 8 RPC tooling](https://docs.openmediavault.org/en/8.x/development/tools/omv_rpc.html)
  documents read-only mounted-filesystem and disk inventory RPC calls.
- [OMV filesystem administration](https://docs.openmediavault.org/en/latest/administration/storage/filesystems.html)
  explains that filesystems must be mounted through OMV to be registered for
  shared-folder configuration.
