# Proxmox LXC helper

Run the generic launcher from the Proxmox host with the profile you want:

```bash
bash proxmox_helper_script/create-lxc.sh proxmox_helper_script/lxc/ubuntu/controlplane.profile.sh
bash proxmox_helper_script/create-lxc.sh proxmox_helper_script/lxc/ubuntu/jenkins-agent.profile.sh
bash proxmox_helper_script/create-lxc.sh proxmox_helper_script/lxc/technitium_dns/dns01.profile.sh
bash proxmox_helper_script/create-lxc.sh proxmox_helper_script/lxc/technitium_dns/dns02.profile.sh
```

Create another `.profile.sh` file for each container. Profiles may use the
`LXC_PROFILE_*` and `LXC_DEFAULT_*` Bash variables; the launcher translates
those defaults to the Community Scripts `var_*` settings, including storage selection. Profiles are trusted Bash configuration files
because they can contain arrays and computed paths.

Use `LXC_DEFAULT_HOST_POST_INSTALL_SCRIPT` for a hook that exists on the
Proxmox host. The launcher runs it with `CTID` after container creation and
propagates failures. The Ubuntu profiles automatically install their Jenkins
applications. The `controlplane.sh` bootstrap prompts for root passwords
immediately before each new container and configures Infisical plus the
dedicated Proxmox SSH key through `whiptail` before a new controlplane is
created. See the root README for details. The selected Community Script may
still prompt for host-specific choices.

During controlplane bootstrap, `controlplane.sh` ensures the single
`HomelabLxcOperator` Proxmox role includes the VM (including memory and
CD-ROM configuration), storage, SDN, and Talos ISO download privileges. It
grants that role on the configured Proxmox node and ISO datastore, plus the
configured SDN path (default `/sdn/zones/localnetwork/vmbr0/80`). Defaults are
`pve01`, `local`, and `homelab-automation@pve`; override them with
`HOMELAB_PROXMOX_NODE_NAME`, `HOMELAB_PROXMOX_ISO_STORAGE_ID`,
`HOMELAB_PROXMOX_SDN_ACL_PATH`, and `HOMELAB_PROXMOX_AUTOMATION_USER` when
needed. Job 002 grants matching node, storage, and SDN ACLs to the
privilege-separated API token and removes obsolete downloader-role ACLs and
the role during token rotation.

On Proxmox, `/root/.ssh/authorized_keys` is commonly a symlink to the
cluster-managed `/etc/pve/priv/authorized_keys`. The bootstrap follows only
that exact Proxmox target so it can add and rotate its key without replacing
the link or altering unrelated SSH keys.
