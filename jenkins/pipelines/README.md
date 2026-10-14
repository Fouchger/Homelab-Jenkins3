# Jenkins pipelines

Keep Jenkins Pipeline definitions and their pipeline-only helpers under this
directory. Deployment scripts that install Jenkins remain in `../deploy/`, and
Proxmox host-side LXC creation/profile scripts remain in `../../proxmox_helper_script/`.

| File | Purpose |
| --- | --- |
| `toolchain-check.Jenkinsfile` | Periodic read-only check of the Jenkins automation agent toolchain. |
| `kubernetes-infra/Jenkinsfile` | Job `008 - Kubernetes Infrastructure`: accepts the complete cluster configuration in `CLUSTER_TFVARS`, validates and plans Talos VM changes, and applies only when `APPLY` is selected and the exact displayed plan is approved. `RECREATE_NODE` safely replaces one selected VM per build. |
| `kubernetes-infra/run-tofu.py` | Retrieves the Proxmox API token from Infisical, rejects destructive plans without an explicit node selection and multi-VM updates, and runs OpenTofu against the private Kubernetes variable file on the Jenkins agent. Selected recreation temporarily clears and restores Proxmox protection. |
| `server-update/Jenkinsfile` | Job `001 - Update Servers`: updates the controller and agent LXCs daily at 2:00 a.m. Pacific/Auckland. Owner and repository are job parameters; the optional GitHub token is only needed for a private repository. |
| `server-update/update-servers.py` | Retrieves the Proxmox SSH key and trusted host key from Infisical, then runs the host bootstrap over SSH. |
| `proxmox-access/Jenkinsfile` | Job `002 - Proxmox Access Setup`: ensures the selected Proxmox API hostname has a trusted ACME certificate through Cloudflare DNS-01, extends `HomelabLxcOperator` with VM, Talos ISO, and configured VLAN SDN access, rotates the Proxmox API token, and writes it to Infisical. If no usable ACME account exists, account creation reads `/proxmox/acme/ACME_EMAIL` and requires the `ACCEPT_LETSENCRYPT_TOS` build parameter. |
| `proxmox-access/provision-proxmox-access.py` | Infisical API and pinned-SSH helper called by the Proxmox access pipeline; retrieves the Cloudflare token only from `/cloudflare`, configures Proxmox ACME, verifies certificate SAN coverage, manages `HomelabLxcOperator` node/storage/SDN ACLs, removes obsolete downloader-role grants, and rotates the API token. |
| `network-settings/Jenkinsfile` | Job `003 - DNS and Cloudflare Settings`: collects editable settings, stores them in Infisical, verifies the saved pinned RouterOS connection, and reviews managed router settings without applying changes. |
| `network-settings/save-network-settings.py` | Validates, writes, verifies, and rolls back operator-entered DNS, MikroTik, Cloudflare, and DockFlare settings. It can use the saved `MIKROTIK_IP` as a fallback only when `MIKROTIK_HOST:443` refuses connections, still requiring the same HTTPS certificate pin. It saves a scanned RouterOS host key only after it matches the public key fetched over pinned HTTPS and all temporary export files are removed. |
| `network-settings/review-mikrotik-settings.py` | Reads router settings from Infisical, checks the RouterOS HTTPS certificate pin and REST login, verifies SSH backup prerequisites are present, and compares current DNS/DHCP/Wi-Fi settings plus the managed Proxmox API hostname record with desired settings. If `MIKROTIK_HOST:443` refuses, it can try `MIKROTIK_IP` with the same pin. It is read-only and reports missing prerequisites and planned differences. |
| `mikrotik-config/Jenkinsfile` | Job `004 - MikroTik Configuration`: verifies the router and displays the planned DNS, DHCP, Proxmox API hostname, and supplied Wi-Fi changes before approval; then backs up the router and applies the approved routine changes. |
| `mikrotik-config/apply-mikrotik-config.py` | Reads router settings from Infisical, creates text and binary pre-change backups over pinned SSH/SFTP, encrypts the combined archive on the agent, removes router-side temporary files, then applies and verifies DNS/DHCP/Proxmox API hostname/Wi-Fi changes through pinned HTTPS REST. |
| `mikrotik-restore/Jenkinsfile` | Job `005 - MikroTik Full Reset and Restore`: a separate destructive recovery action; verifies saved settings and pinned SSH access before asking for approval. |
| `mikrotik-restore/restore-mikrotik.py` | `--check` validates settings and SSH access without changing the router. The approved run downloads an encrypted pre-reset backup, preserves the pinned SSH server key for import after reset, dry-runs the Infisical script, then waits up to 30 minutes for verified SSH recovery at `MIKROTIK_IP`, reporting progress and distinguishing key mismatch, login rejection, and unavailable SSH. |
| `dns-deploy/Jenkinsfile` | Job `006 - DNS Deployment and Router Sync`: shows a read-only plan for dns01/dns02 and reviews the router's DNS/DHCP differences, including `pve01.fouchger.uk -> 192.168.20.10`, before one approval. It also verifies and updates Jenkins agent CTID 101 to use dns01/dns02 with the `fouchger.uk` search domain. Each DNS server has a `reuse` or `destroy-and-recreate` choice; both may be selected and are handled sequentially with peer health checks and DNS zone data preservation. Full Proxmox container backups are not created. Root passwords come from `/proxmox/lxc/<server>/LXC_ROOT_PASSWORD`; each server's `DNS_IPV4` and `DNS_SERVER_ADMIN_PASSWORD` come from `/dns/<server>`. |
| `dns-deploy/deploy-dns.py` | `--plan` checks the pinned Proxmox connection and existing LXC identities without changes, then lists the intended DNS operations, including any selected destructive replacement. The normal run replaces at most one identity-verified CTID through pinned Proxmox SSH and runs DNS management locally inside each guest so passwords do not cross the network in clear text. |
| `dns-deploy/manage-technitium.py` | Uses Technitium's local HTTP API over loopback to secure the admin login, create configured primary zones, permit zone transfers only from dns02, and create/resync secondary zones on dns02. |
| `infisical-audit/Jenkinsfile` | Job `007 - Infisical Variable Audit`: lists secret folder/name pairs without values, reports names that exist as PRESENT, required omissions as MISSING, and present-but-unused values as UNUSED, then archives a names-only CSV. PRESENT does not validate a value or its permissions. |
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
against the live certificate before saving it and requires the REST account's
`ftp` and `write` policies to remove and verify removal of temporary host-key
export files. RouterOS REST requires `www-ssl` or `www` to be enabled; Job 003
can fall back to the configured `MIKROTIK_IP` only when the management host
refuses port 443 and only when the same saved HTTPS certificate pin matches.
Job 004 also uses `MIKROTIK_SSH_USER`, `MIKROTIK_SSH_PRIVATE_KEY`, and
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
It uses the tracked `router-baseline.rsc` file shipped beside the pipeline and
reads the post-reset address `MIKROTIK_IP` and sensitive credentials from
Infisical. The baseline must be reviewed and committed with code changes;
`MIKROTIK_SCRIPT` is obsolete and can be removed from Infisical after migrating
to this tracked source. Job 005 requires explicit approval and relies on the
baseline restoring SSH reachability over the Proxmox-linked `ether2` path.
Before reset, Job 005 checks that the saved Ed25519 public and
private keys match, downloads the password-protected binary backup to a
temporary agent file, verifies its size, and confirms the temporary router copy
was removed. It also exports the current SSH server host key and the certificate
assigned to `www-ssl` in encrypted form, then restores both after reset and
enables `www-ssl` on port 443 while retaining its source address restrictions.
Before reset and again during recovery,
Job 005 can verify the current SSH key through RouterOS REST over the saved
`MIKROTIK_TLS_CERT_SHA256` pin, compare it with the key scanned at the router,
and update `MIKROTIK_SSH_HOST_KEY` in Infisical. The saved SSH entry includes
both the management hostname and recovery IP so the post-reset follow-up works
through either address. This requires the existing Infisical writer credential
and a RouterOS REST account with `sensitive`, `ftp`, and `write` policies. If
the HTTPS certificate no longer matches its saved pin, Job 005 stops safely.
A failed check stops before reset; the Jenkins artifact is retained when it was
already downloaded. Job 006 provisions/configures dns01 and dns02
before changing router DHCP DNS, so a router backup or sync failure can leave
the DNS servers updated while DHCP continues using its previous settings.
Job 006 asks for an action per DNS server: `reuse` or `destroy-and-recreate`.
Both servers may be selected for recreation in one build. Jenkins handles them
sequentially, checks that the peer is online before each deletion, and
saves/restores dns01 zone files and synchronizes the restored primary zones to
dns02 before taking dns02 offline. Full container backups are not made. For a
protected container, Jenkins temporarily disables Proxmox protection and
restores it if deletion fails. The apply stage stops before deletion if the
peer fails its container, address, Technitium authentication, or DNS listener
health checks. The later router DNS/DHCP stage still makes its normal encrypted
pre-change MikroTik backup.

Job 005 takes Wi-Fi passphrases from `/mikrotik/wifi_security` and the RouterOS
scheduled-backup password from `/mikrotik/backup`, then replaces placeholders
in the temporary rendered baseline before uploading it. The tracked baseline
uses `${SEC_USERS_PASSWORD}`, `${SEC_MGMT_PASSWORD}`,
`${SEC_IOT_PASSWORD}`, `${SEC_GUEST_PASSWORD}`, and
`${BINARY_BACKUP_PASSWORD}` placeholders instead of containing password
literals. For example, use `name=sec-users passphrase=${SEC_USERS_PASSWORD}`
and, inside a scheduled backup `on-event`,
`password=\"${BINARY_BACKUP_PASSWORD}\"`. Job 005 also overrides any existing
literal in those named Wi-Fi profiles or scheduled backup commands with the
Infisical value. The `/user` section should be omitted from `router-baseline.rsc`;
Job 005 creates/restores router accounts from `MIKROTIK_USERNAME`,
`MIKROTIK_PASSWORD`, `MIKROTIK_SSH_USER`, and the saved Jenkins key.

The reset import deliberately skips the `www-ssl` service assignment because
certificates are removed by a full reset. After importing the main script,
Job 005 restores the preserved certificate and enables HTTPS using its saved
source restriction (`available-from` on RouterOS 7.24+, or the older
`address` property). It also ensures bridge VLAN table entries exist for the
IoT VLAN 50 and Guest VLAN 60 Wi-Fi interfaces in this homelab configuration.
