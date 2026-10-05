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
applications; see the root README for settings and one-time Jenkins setup.
The selected Community Script may still prompt for host-specific choices.
