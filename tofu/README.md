# OpenTofu

Place reusable modules in `modules/` and deployable root configurations in
`environments/`. The first environment, `environments/kubernetes`, manages
Talos VMs on Proxmox and documents its protected local bootstrap state. Move it
to a backed-up, locking remote backend before multi-operator or unattended
production use.
