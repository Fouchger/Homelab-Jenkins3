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
| `network-settings/review-mikrotik-settings.py` | Reads router settings from Infisical, checks the RouterOS HTTPS certificate pin and REST login, verifies SSH backup prerequisites are present, and compares current DNS/DHCP/Wi-Fi configuration with saved settings. It is read-only and reports missing prerequisites and planned differences. |
| `mikrotik-config/Jenkinsfile` | Job `004 - MikroTik Configuration`: verifies the router and displays the planned DNS, DHCP, and supplied Wi-Fi changes before approval; then backs up the router and applies the approved routine changes. |
| `mikrotik-config/apply-mikrotik-config.py` | Reads router settings from Infisical, creates text and binary pre-change backups over pinned SSH/SFTP, encrypts the combined archive on the agent, removes router-side temporary files, then applies and verifies DNS/DHCP/Wi-Fi changes through pinned HTTPS REST. |
| `mikrotik-restore/Jenkinsfile` | Job `005 - MikroTik Full Reset and Restore`: a separate destructive recovery action; verifies saved settings and pinned SSH access before asking for approval. |
| `mikrotik-restore/restore-mikrotik.py` | `--check` validates settings and SSH access without changing the router. The approved run downloads an encrypted pre-reset backup, preserves the pinned SSH server key for import after reset, dry-runs the Infisical script, then waits up to 30 minutes for verified SSH recovery at `MIKROTIK_IP`, reporting progress and distinguishing key mismatch, login rejection, and unavailable SSH. |
| `dns-deploy/Jenkinsfile` | Job `006 - DNS Deployment and Router Sync`: shows a read-only plan for dns01/dns02 and reviews the router's DNS/DHCP differences before one approval. `RECREATE_DNS_SERVER` defaults to `none` and can select only `dns01` or `dns02` for clean replacement; its existing DNS data is permanently deleted without a DNS-container backup, and the other server is reused. Shared policy is read from `/dns`; each server's `DNS_IPV4` and `DNS_SERVER_ADMIN_PASSWORD` are read from its `/dns/<server>` folder. |
| `dns-deploy/deploy-dns.py` | `--plan` checks the pinned Proxmox connection and existing LXC identities without changes, then lists the intended DNS operations, including any selected destructive replacement. The normal run replaces at most one identity-verified CTID through pinned Proxmox SSH and runs DNS management locally inside each guest so passwords do not cross the network in clear text. |
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
RouterOS. Job 004 also uses `MIKROTIK_SSH_USER`, `MIKROTIK_SSH_PRIVATE_KEY`, and
the independently verified `MIKROTIK_SSH_HOST_KEY` for the pre-change backup.
Its SSH account must have `ssh`, `read`, `write`, `ftp`, and `sensitive`
permissions. After approval, Job 004 enables `ssh` for the account's group only
when that group contains no other users; shared groups are never changed.
`sensitive` is required to include passwords and keys in the export. Job 004 creates a RouterOS-password-
protected binary backup and a sensitive text export, downloads both over SFTP,
encrypts them together on the Jenkins agent with `BINARY_BACKUP_PASSWORD`, and
removes their temporary router copies before applying settings. The resulting
`.tar.enc` build artifact contains both backup formats; protect it and its
password. Jenkins archives the file on the controlplane under
`/var/lib/jenkins/jobs/<job-name>/builds/<build-number>/archive/artifacts/<build-number>/`;
it is also downloadable from the build's **Artifacts** section. Jobs 004 and
006 currently retain the latest 20 builds, so older archived backups are
removed when Jenkins discards those builds. The REST account needs `api`,
`rest-api`, `read`, and `write` for
router review and configuration operations; it also needs `policy` to authorize
the SSH key for its RouterOS user. Update saved pins after certificate or SSH
host-key rotation.
The full reset job is intentionally separate from routine router configuration.
It reads the multiline `MIKROTIK_SCRIPT` secret and post-reset address
`MIKROTIK_IP` from `/mikrotik/router01`, requires explicit approval, and relies
on the configuration script restoring SSH reachability over the Proxmox-linked
`ether2` path. Before reset, Job 005 checks that the saved Ed25519 public and
private keys match, downloads the password-protected binary backup to a
temporary agent file, verifies its size, and confirms the temporary router copy
was removed. It also exports the current SSH server host key in encrypted form
and imports that same key after reset, so the saved host-key pin remains valid.
A failed check stops before reset; the Jenkins artifact is retained when it was
already downloaded. Job 006 provisions/configures dns01 and dns02
before changing router DHCP DNS, so a router backup or sync failure can leave
the DNS servers updated while DHCP continues using its previous settings.
