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
| `network-settings/Jenkinsfile` | Job `003 - DNS and Cloudflare Settings`: collects editable settings, stores them in Infisical, verifies the saved pinned RouterOS connection, and reviews managed router settings without applying changes. |
| `network-settings/save-network-settings.py` | Validates, writes, verifies, and rolls back operator-entered DNS, MikroTik, Cloudflare, and DockFlare settings. It generates a missing MikroTik Ed25519 client key and saves a scanned RouterOS host key only after its fingerprint matches the operator-supplied pin. |
| `network-settings/review-mikrotik-settings.py` | Reads router settings from Infisical, checks the RouterOS HTTPS certificate pin and REST login, and compares current DNS/DHCP/Wi-Fi configuration with saved settings. It is read-only and reports missing prerequisites and planned differences. |
| `mikrotik-config/Jenkinsfile` | Job `004 - MikroTik Configuration`: verifies the router and displays the planned DNS, DHCP, and supplied Wi-Fi changes before approval; then backs up the router and applies the approved routine changes. |
| `mikrotik-config/apply-mikrotik-config.py` | Reads router settings from Infisical, creates a sensitive RouterOS text export through pinned HTTPS REST, encrypts it on the agent, removes the router-side temporary file, then applies and verifies DNS/DHCP/Wi-Fi changes through REST. The `.rsc.enc` artifact is not a binary clone. |
| `mikrotik-restore/Jenkinsfile` | Job `005 - MikroTik Full Reset and Restore`: a separate destructive recovery action; verifies saved settings and pinned SSH access before asking for approval. |
| `mikrotik-restore/restore-mikrotik.py` | `--check` validates settings and SSH access without changing the router. The approved run downloads an encrypted pre-reset backup, dry-runs the Infisical script, resets with a generated account/bootstrap wrapper, then waits for pinned SSH recovery at `MIKROTIK_IP`. |
| `dns-deploy/Jenkinsfile` | Job `006 - DNS Deployment and Router Sync`: shows a read-only plan for dns01/dns02 and reviews the router's DNS/DHCP differences before one approval. The approved run creates or reuses verified DNS LXCs, configures Technitium zone replication, and syncs router DNS with an encrypted pre-change backup. Shared policy is read from `/dns`; each server's `DNS_IPV4` and `DNS_SERVER_ADMIN_PASSWORD` are read from its `/dns/<server>` folder. |
| `dns-deploy/deploy-dns.py` | `--plan` checks the pinned Proxmox connection and existing LXC identities without changes, then lists the intended DNS operations. The normal run provisions through pinned Proxmox SSH and runs DNS management locally inside each guest so passwords do not cross the network in clear text. |
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
RouterOS. Job 004 requires the independently verified HTTPS certificate
fingerprint and a RouterOS account with REST access, write permissions for the
settings it manages, and permissions to export and read files. The sensitive
export includes passwords and keys, so protect the encrypted build artifact and
its `BINARY_BACKUP_PASSWORD`. It is a text export, not a binary clone. Update
the saved fingerprint after certificate rotation.
The full reset job is intentionally separate from routine router configuration.
It reads the multiline `MIKROTIK_SCRIPT` secret and post-reset address
`MIKROTIK_IP` from `/mikrotik/router01`, requires explicit approval, and relies
on the configuration script restoring SSH reachability over the Proxmox-linked
`ether2` path.
