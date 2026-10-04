# Proxmox LXC helper

Run the generic launcher from the Proxmox host with the profile you want:

```bash
bash proxmox_helper_script/create-lxc.sh proxmox_helper_script/lxc/ubuntu/controlplane.env
bash proxmox_helper_script/create-lxc.sh proxmox_helper_script/lxc/ubuntu/jenkins-agent.env
bash proxmox_helper_script/create-lxc.sh proxmox_helper_script/lxc/technitium_dns/dns01.env
bash proxmox_helper_script/create-lxc.sh proxmox_helper_script/lxc/technitium_dns/dns02.env
```

Create another `.env` profile for each container. Profiles may use the
`LXC_PROFILE_*` and `LXC_DEFAULT_*` Bash variables; the launcher translates
those defaults to the Community Scripts `var_*` settings, including installer
URL and storage selection. Profiles are trusted Bash configuration files
because they can contain arrays and computed paths.

Keep any post-install hook pointed at a script that exists on the Proxmox host.
The selected Community Script may still prompt for host-specific choices.
