# Infisical integration

During first-time Proxmox controlplane creation, a `whiptail` dialog collects
the project URL, project UUID, environment, optional project slug, and the
existing `jenkins-read` and `jenkins-write` Machine Identity Client IDs and
Client Secrets. The secrets are entered into password dialogs and are not
printed. Both identities are authenticated before the setup proceeds.

The bootstrap generates a dedicated Ed25519 SSH key for Jenkins. It reads the
Proxmox Ed25519 host public key locally, builds a `known_hosts` entry for the
address Jenkins will use, and displays that host-key fingerprint for operator
confirmation. It saves `PVE_SSH_PRIVATE_KEY` and `PVE_SSH_HOST_KEY` in Infisical
under `/proxmox/automation`, verifies the values using the read-only identity,
and adds the matching public key to root's Proxmox `authorized_keys`. The private
key is never put in the public repository.

The same setup passes the Machine Identity credentials and project settings to
the controller installer. Jenkins stores them in its encrypted credentials
store for its Infisical integration and the automation pipelines. Proxmox uses
root-only temporary files for this handoff and removes them after Jenkins has
imported and verified the credentials.

Before creating each new Jenkins LXC, the bootstrap also asks for its root
password and writes `LXC_ROOT_PASSWORD` to that LXC's `/proxmox/lxc/<name>`
folder in Infisical. The password is applied to the container and both host-side
temporary copies are removed. Reused containers are not prompted or rotated.

When a controlplane already exists, an interactive bootstrap offers to rotate
the Infisical/SSH setup or keep the current credentials. Automated reuse skips
the dialog. Recreating the controlplane requires configuring Infisical again.

The current secret layout is:

| Infisical path | Secret names |
| --- | --- |
| `/proxmox/automation` | `PVE_API_TOKEN_ID`, `PVE_API_TOKEN_SECRET`, `PVE_SSH_HOST_KEY`, `PVE_SSH_PRIVATE_KEY` |
| `/proxmox/pve01` | `PROXMOX_ROOT_PASSWORD` |
| `/proxmox/lxc/controlplane`, `/proxmox/lxc/jenkins-agent`, and other LXC folders | `LXC_ROOT_PASSWORD` only |
| `/dns` | `DNS_PUBLIC_FALLBACKS`, `MIKROTIK_DHCP_DNS_MODE`, optional `DNS_HOSTED_ZONES` |
| `/dns/dns01` | `DNS_IPV4`, `DNS_SERVER_ADMIN_PASSWORD` |
| `/dns/dns02` | `DNS_IPV4`, `DNS_SERVER_ADMIN_PASSWORD` |
| `/proxmox/lxc/dns01`, `/proxmox/lxc/dns02` | `LXC_ROOT_PASSWORD` only |
| `/mikrotik/router01` | `MIKROTIK_HOST`, `MIKROTIK_IP`, `MIKROTIK_USERNAME`, `MIKROTIK_PASSWORD`, `MIKROTIK_BOOTSTRAP_USERNAME`, `MIKROTIK_BOOTSTRAP_PASSWORD`, `MIKROTIK_SCRIPT`, `MIKROTIK_SSH_USER`, `MIKROTIK_SSH_PRIVATE_KEY`, `MIKROTIK_SSH_PUBLIC_KEY`, `MIKROTIK_SSH_HOST_KEY`, `MIKROTIK_TLS_CERT_SHA256` |
| `/mikrotik/backup` | `BINARY_BACKUP_PASSWORD` |
| `/mikrotik/wifi_security` | `SEC_GUEST_PASSWORD`, `SEC_IOT_PASSWORD`, `SEC_MGMT_PASSWORD`, `SEC_USERS_PASSWORD` |
| `/cloudflare` | `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_DOMAIN_1`, `CLOUDFLARE_DOMAIN_2`, `CLOUDFLARE_ZONE_ID_1`, `CLOUDFLARE_ZONE_ID_2` |
| `/dockflare` | `DOCKFLARE_ACCESS_EMAILS`, `DOCKFLARE_ADMIN_CIDRS` |

Job `003 - DNS and Cloudflare Settings` lets an operator supply or update these
values at run time, including each DNS LXC root password, MikroTik management
credentials, backup password, and Wi-Fi passphrases. It validates and writes
nonblank inputs to Infisical,
verifies the saved values, and restores the prior values if a write fails.
Blank inputs leave existing values unchanged. Domain names, Access email
addresses, administrator network ranges, DNS addresses, resolver fallbacks,
and DHCP DNS mode are not embedded in the public repository. The Cloudflare API
token is a masked Jenkins password parameter and is also saved in Infisical;
Jenkins retains password parameters encrypted with their build records. To
avoid a second stored copy, enter or rotate the token directly in Infisical.
DNS LXC root passwords entered as masked parameters are likewise retained in
encrypted Jenkins build records; alternatively, create/update `LXC_ROOT_PASSWORD`
directly in each DNS LXC Infisical folder. Technitium admin passwords are
`/dns/dns01/DNS_SERVER_ADMIN_PASSWORD` and
`/dns/dns02/DNS_SERVER_ADMIN_PASSWORD`.
`DNS_HOSTED_ZONES` is an optional comma-separated list of zones created as
primary zones on dns01 and replicated to dns02; leave it unset until the hosted
domains are chosen. Set admin passwords directly in Infisical to avoid
retaining password parameters in Jenkins build records. Job 006 changes the
default `admin`/`admin` password only on a new Technitium install; it refuses
to reset an unknown existing admin password.
MikroTik administrator/backup passwords and Wi-Fi passphrases entered in Job
003 are also retained in encrypted Jenkins build records. To avoid those
additional copies, enter them directly in the corresponding Infisical secrets.
The complete RouterOS restore script is a multiline secret and must be entered
directly in Infisical as `/mikrotik/router01/MIKROTIK_SCRIPT`. Job 003 accepts
`MIKROTIK_IP`, the IPv4 address to use for SSH after a full reset. The script
must configure that address on the Proxmox-connected `ether2` network and must
not contain another reset command. Job 005 removes the script's `/user`
sections and supplies the configured admin and SSH-user setup itself. Put the
script directly into the Infisical multiline secret; it is not a Jenkins
parameter or a public repository file.

When `MIKROTIK_HOST` is configured and the SSH key pair is absent, job 003
generates an Ed25519 client key pair and stores its private and public keys in
`/mikrotik/router01`. Job `004 - MikroTik Configuration` uses pinned RouterOS
HTTPS REST only; it does not require the SSH key or SSH host-key pin. Enable
`www-ssl` and set
`/mikrotik/router01/MIKROTIK_TLS_CERT_SHA256` to the independently verified
64-character SHA-256 fingerprint of the RouterOS HTTPS certificate. When the
setting is blank, Job 003 displays the fingerprint it sees but does not trust
or save it; compare it with a trusted RouterOS view, then enter it in Job 003.
The separate `MIKROTIK_SSH_HOST_KEY_FINGERPRINT` field is only needed when
Job 003 is adding a new SSH host key. In a trusted WinBox Terminal, run:

```routeros
/ip/ssh/print
```

Copy the `host-key-fingerprint` value exactly, including the `SHA256:` prefix,
into that Jenkins field. Job 003 compares it with the SSH key scanned from
`MIKROTIK_HOST` before saving the key to Infisical. RouterOS documents this
command as the way to display the current SSH host-key fingerprint.

To obtain the HTTPS certificate fingerprint, connect to the router through a
trusted WinBox session, open **New Terminal**, and run:

```routeros
/ip/service/print detail where name=www-ssl
```

Read the `certificate` value from the `www-ssl` entry. Then run this command,
replacing `<certificate name>` with that value:

```routeros
/certificate/print detail where name="<certificate name>"
```

Copy the `fingerprint` value (64 hexadecimal characters) into Job 003's
`MIKROTIK_TLS_CERT_SHA256` field without spaces. The first Job 003 run can
also print the fingerprint it sees at `MIKROTIK_HOST:443`; compare it with the
value shown in the trusted RouterOS terminal. If they match, rerun Job 003 with
the fingerprint. Job 003 checks it against the live HTTPS certificate before
saving it to Infisical. Confirm `www-ssl` is enabled and that its assigned
certificate is the one Jenkins reaches at `MIKROTIK_HOST`.

REST uses `MIKROTIK_USERNAME` and `MIKROTIK_PASSWORD`, falling back to
the bootstrap credentials when the normal account is not configured. To create
the pre-change artifact, Job 004 saves a RouterOS sensitive configuration export
over REST, encrypts it on the Jenkins agent with `BINARY_BACKUP_PASSWORD`, and
deletes the temporary router-side file. The `.rsc.enc` artifact contains
passwords and keys and must be protected. This is a restorable text export, not
a binary clone: RouterOS exports omit system user passwords, installed
certificates, and SSH keys. Update the HTTPS certificate pin after rotation.
SSH keys and the pinned SSH host key remain for the separate full-reset and
recovery workflow, which takes a binary backup.

`MIKROTIK_BOOTSTRAP_USERNAME` and `MIKROTIK_BOOTSTRAP_PASSWORD` are the
existing router administrator credentials used only to bootstrap key access
and connect before a reset. `MIKROTIK_USERNAME` and `MIKROTIK_PASSWORD` are
the administrator credentials created by the reset workflow. The backup
password is stored separately as `BINARY_BACKUP_PASSWORD`.

Job `005 - MikroTik Full Reset and Restore` is a separate destructive workflow.
It requires manual approval, validates the Infisical script and target address,
dry-runs the script on the running router, creates and downloads an encrypted
backup, then resets RouterOS with the script set to run after reboot. The
startup script creates the configured full-rights admin and SSH users, disables
the default `admin` when a different admin name is configured, imports
`MIKROTIK_SCRIPT` with its `/user` sections omitted, logs a success marker, and
removes its temporary script files on success. Jenkins reconnects to
`MIKROTIK_IP` with the pinned host key and generated SSH key and checks the
import marker, then applies Infisical DNS/DHCP and Wi-Fi settings with a second
encrypted backup. RouterOS limits this
run-after-reset script to two minutes; a failed import or missing `ether2`
address may require local/console recovery. Keep the encrypted artifact
available before running. Do not run this job as a routine configuration update.

Allowed values for `MIKROTIK_DHCP_DNS_MODE` are `router` or `mikrotik` (clients
ask the MikroTik, which forwards to dns01, dns02, then configured public
fallbacks) and `direct` or `technitium` (clients receive the two Technitium
addresses). `DNS_PUBLIC_FALLBACKS`
is a comma-separated IP address list. `DOCKFLARE_ACCESS_EMAILS` is a
comma-separated email list; `DOCKFLARE_ADMIN_CIDRS` is a comma-separated list
of IP networks. Job 003 only stores configuration. Job 004 applies MikroTik DNS,
DHCP DNS, and any supplied Wi-Fi passphrases after manual approval, taking an
encrypted sensitive text export through HTTPS REST before changing settings,
and archiving it as a Jenkins build artifact. `router` mode enables router DNS
requests and adds scoped UDP/TCP 53 input rules for DHCP client subnets;
`direct` mode gives DHCP clients the Technitium addresses and disables RouterOS
DNS requests. The job checks that requested Wi-Fi security profiles exist
before changing settings. It does not replace the full router configuration or
create RouterOS users, firewall policy, VLANs, or DHCP scopes.
Job `006 - DNS Deployment and Router Sync` creates missing DNS guests (or reuses
exact profile matches), configures Technitium, and adds a secondary zone on
dns02 for each non-internal primary zone on dns01. Set `DNS_HOSTED_ZONES` in
Infisical to create selected zones; add records on dns01 and Technitium's zone
transfer keeps dns02 synchronized. No service records are invented.
DHCP clients receive the resolver arrangement selected by
`MIKROTIK_DHCP_DNS_MODE`. DockFlare deployment still has separate
implementation work remaining.

Run `007 - Infisical Variable Audit` to compare visible variable names and
folders in the selected project/environment with
[`expected-secrets.json`](../jenkins/pipelines/infisical-audit/expected-secrets.json).
The job calls Infisical's recursive secrets-list endpoint with
`viewSecretValue=false`; it never requests, prints, or stores secret values.
Its console output and archived CSV show `SET` for configured variables used by
the project, `MISSING` for absent required variables, and `UNUSED` for present
variables that the project does not consume (including untracked names).
Missing optional, conditional, planned, and obsolete variables are omitted.
Missing required entries fail the build. The read identity must have list/read
access to all project folders included in the audit.

The Proxmox access pipeline uses the read identity to retrieve the SSH key,
trusted host key, and existing API token values. It uses the writer identity to
save and verify rotated API token values. It does not read LXC password folders.
Jobs `004 - MikroTik Configuration` and `006 - DNS Deployment and Router Sync`
use the read identity to retrieve `/dns`, `/dns/dns01`, `/dns/dns02`, `/proxmox/automation`, and
`/mikrotik/router01`, `/mikrotik/backup`, and `/mikrotik/wifi_security` values as applicable. Job 006 additionally reads
`/proxmox/lxc/dns01` and `/proxmox/lxc/dns02`. Grant the read identity access to
those paths. Job 004 uses `MIKROTIK_BOOTSTRAP_USERNAME` and
`MIKROTIK_BOOTSTRAP_PASSWORD` as a REST login fallback when the normal MikroTik
credentials are not set; it does not require SSH. It encrypts its sensitive
text-export artifact with `BINARY_BACKUP_PASSWORD`. Job 005 uses the SSH key and
host-key pin and archives the encrypted binary backup. Restrict access to both
jobs and their artifacts.

## Jenkins credentials

| Credential ID | Purpose |
| --- | --- |
| Configured `HOMELAB_INFISICAL_CREDENTIAL_ID` (default `infisical-homelab-prod`) | `jenkins-read` Universal Auth |
| `infisical-homelab-prod-writer` | `jenkins-write` Universal Auth |
| `infisical-homelab-prod-read-api` | Read identity for pipeline API calls |
| `infisical-homelab-prod-writer-api` | Write identity for pipeline API calls |
| `pve01-automation-ssh` | Imported SSH private key for Proxmox administration |
| `homelab-infisical-url`, `homelab-infisical-project-id`, `homelab-infisical-environment`, `homelab-infisical-project-slug`, `homelab-proxmox-host` | Project and host settings used by Jenkins pipelines |

For automated use, run `001 - Update Servers` before `002 - Proxmox Access
Setup`. The first job retrieves the Proxmox SSH key and host key from Infisical;
the second creates or rotates the Proxmox API token and saves the result.
