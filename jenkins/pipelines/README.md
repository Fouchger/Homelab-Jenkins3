# Jenkins pipelines

Keep Jenkins Pipeline definitions and their pipeline-only helpers under this
directory. Deployment scripts that install Jenkins remain in `../deploy/`, and
Proxmox host-side LXC creation/profile scripts remain in `../../proxmox_helper_script/`.

| File | Purpose |
| --- | --- |
| `toolchain-check.Jenkinsfile` | Periodic read-only check of the Jenkins automation agent toolchain. |
| `server-update/Jenkinsfile` | Job `001 - Update Servers`: updates the controller and agent LXCs daily at 2:00 a.m. Pacific/Auckland. Owner and repository are job parameters; the optional GitHub token is only needed for a private repository. |
| `server-update/update-servers.py` | Retrieves the Proxmox SSH key and trusted host key from Infisical, then runs the host bootstrap over SSH. |
| `proxmox-access/Jenkinsfile` | Job `002 - Proxmox Access Setup`: manually rotates the Proxmox API token and writes it to Infisical. |
| `proxmox-access/provision-proxmox-access.py` | Infisical API and SSH helper called by the Proxmox access pipeline. |
| `network-settings/Jenkinsfile` | Job `003 - DNS and Cloudflare Settings`: collects editable settings and stores them in Infisical without applying router/DNS configuration. |
| `network-settings/save-network-settings.py` | Validates, writes, verifies, and rolls back operator-entered DNS, MikroTik, Cloudflare, and DockFlare settings. It generates a missing MikroTik Ed25519 client key and saves a scanned RouterOS host key only after its fingerprint matches the operator-supplied pin. |
| `mikrotik-config/Jenkinsfile` | Job `004 - MikroTik Configuration`: waits for operator approval, backs up the router, then applies Infisical-managed DNS, DHCP DNS, and supplied Wi-Fi security profile passphrases. |
| `mikrotik-config/apply-mikrotik-config.py` | Reads router settings from Infisical, uses pinned SSH only to create/download the encrypted binary backup, then applies and verifies DNS/DHCP/Wi-Fi changes through HTTPS RouterOS REST with a pinned certificate. |
| `mikrotik-restore/Jenkinsfile` | Job `005 - MikroTik Full Reset and Restore`: a separate destructive, manually approved reset using the complete configuration stored in Infisical. |
| `mikrotik-restore/restore-mikrotik.py` | Validates and dry-runs the Infisical script, downloads an encrypted pre-reset backup, resets with a generated account/bootstrap wrapper, then waits for pinned SSH recovery at `MIKROTIK_IP`. |
| `dns-deploy/Jenkinsfile` | Job `006 - DNS Deployment and Router Sync`: after manual approval, creates missing dns01/dns02 LXCs or verifies and reuses existing matches, secures both Technitium admin accounts, creates optional `DNS_HOSTED_ZONES` primaries on dns01, configures zone transfers to dns02, and invokes the DNS-only MikroTik update with an encrypted pre-change backup. Shared policy is read from `/dns`; each server's `DNS_IPV4` and `DNS_SERVER_ADMIN_PASSWORD` are read from its `/dns/<server>` folder. |
| `dns-deploy/deploy-dns.py` | Reads the Proxmox SSH identity and DNS/LXC secrets from Infisical, provisions through the pinned Proxmox SSH connection, and runs DNS management locally inside each guest so passwords do not cross the network in clear text. |
| `dns-deploy/manage-technitium.py` | Uses Technitium's local HTTP API over loopback to secure the admin login, create configured primary zones, permit zone transfers only from dns02, and create/resync secondary zones on dns02. |
| `infisical-audit/Jenkinsfile` | Job `007 - Infisical Variable Audit`: lists secret folder/name pairs without values, reports active values as SET, required omissions as MISSING, and present-but-unused values as UNUSED, then archives a names-only CSV. |
| `infisical-audit/audit-infisical.py` | Uses the Infisical read identity and `viewSecretValue=false`; omits absent optional, conditional, planned, and obsolete entries from its report. |
| `infisical-audit/expected-secrets.json` | Source-controlled declaration of current Infisical folder/name expectations and which jobs use them. |

Infisical identities and project settings are collected with `whiptail` during
first-time Proxmox controlplane creation. That setup generates a dedicated SSH
key, stores its private key and the local Proxmox host key in Infisical, installs
the public key on Proxmox, and imports the configured Infisical credentials into
Jenkins.

The controller startup hooks seed the matching jobs from these repository
paths. Update those script paths in `../deploy/install-controller-guest.sh`
when moving a pipeline file.

The server update job uses Extended Timer Trigger. Timer runs pass
`AUTOMATED_UPDATE=true` to skip the manual confirmation; ordinary manual runs
leave it false and require operator confirmation.

The network settings job treats Infisical as the source of truth. Blank Jenkins
inputs preserve the current Infisical value, so settings can also be changed
directly in Infisical using the folder and variable names in
[`infisical/README.md`](../../infisical/README.md). Cloudflare domains, Access
emails, and DockFlare management CIDRs have no repository defaults.
Routine RouterOS changes require `MIKROTIK_TLS_CERT_SHA256` in
`/mikrotik/router01`, plus the dedicated REST account in `MIKROTIK_USERNAME`
and `MIKROTIK_PASSWORD`. Job 003 checks the entered certificate fingerprint
against the live certificate before saving it. `www-ssl` must be enabled on
RouterOS. Job 004 still needs the independently pinned SSH host key and SSH key
to transfer the encrypted binary backup; only the configuration calls use
HTTPS REST. Update the saved fingerprint after a router certificate rotation.
The full reset job is intentionally separate from routine router configuration.
It reads the multiline `MIKROTIK_SCRIPT` secret and post-reset address
`MIKROTIK_IP` from `/mikrotik/router01`, requires explicit approval, and relies
on the configuration script restoring SSH reachability over the Proxmox-linked
`ether2` path.
