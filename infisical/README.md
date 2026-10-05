# Infisical settings for Jenkins

Configure the Infisical connection in `jenkins/config/install.conf` before
installing or rerunning the Jenkins controller. These values are public
configuration; never put a Client Secret, API token, or other secret in this
file.

```bash
HOMELAB_INFISICAL_CREDENTIAL_ID="infisical-homelab-prod"
HOMELAB_INFISICAL_URL="https://app.infisical.com"
HOMELAB_INFISICAL_PROJECT_ID="535a4605-d859-4fad-b244-656c74a747b4"
HOMELAB_INFISICAL_ENVIRONMENT="prod"
HOMELAB_INFISICAL_PROJECT_SLUG=""
```

`HOMELAB_INFISICAL_CREDENTIAL_ID` is the Jenkins Infisical Universal Auth
credential ID used for secret reads. The Client ID and Client Secret are
imported separately with the protected credential import helper.

Set `HOMELAB_INFISICAL_PROJECT_ID` to the Infisical project UUID and
`HOMELAB_INFISICAL_PROJECT_SLUG` to its slug. The environment is the Infisical
environment slug, such as `prod`. The project ID and slug are both passed to the
controller in `/etc/homelab/project.properties` for Jenkins jobs and setup
automation to use. Keep them consistent with the selected Infisical project.

The existing project Machine Identities are `jenkins-read` (Viewer) and
`jenkins-write` (Member). Keep their Jenkins Universal Auth credentials
separate: `infisical-homelab-prod` for reads and
`infisical-homelab-prod-writer` for writes. The Proxmox setup pipeline also
uses encrypted API credential entries `infisical-homelab-prod-read-api` and
`infisical-homelab-prod-writer-api`; the credential import hook creates these
from the same respective Client ID and Client Secret pairs.

The current secret tree is organized under `/proxmox`:

| Infisical path | Secret names |
| --- | --- |
| `/proxmox/automation` | `PVE_API_TOKEN_ID`, `PVE_API_TOKEN_SECRET`, `PVE_SSH_HOST_KEY`, `PVE_SSH_PRIVATE_KEY` |
| `/proxmox/pve01` | `PROXMOX_ROOT_PASSWORD` |
| `/proxmox/lxc/controlplane`, `/proxmox/lxc/jenkins-agent`, and other LXC folders | `LXC_ROOT_PASSWORD` only |

The installer passes all five settings through to the guest and writes them to
`/etc/homelab/project.properties`. It does not create Machine Identities; the
two project identities above already exist. The project slug remains a
configurable value because it is not visible in the provided project URL.

## Proxmox API access setup

The controller seeds three operator jobs:

1. `001 - Update Servers` updates both Jenkins LXCs daily and uses the read-only
   identity to retrieve the trusted Proxmox SSH host key.
2. `002 - Infisical Credential Setup` pauses while you add the existing read
   and writer Machine Identity credentials in Jenkins, then verifies both
   Universal Auth logins. Add both the Infisical Universal Auth plugin
   credentials (`infisical-homelab-prod` and
   `infisical-homelab-prod-writer`) and the Username with password API
   credentials (`infisical-homelab-prod-read-api` and
   `infisical-homelab-prod-writer-api`). Use each identity's Client ID and
   Client Secret for both types. The job never collects secrets as build input
   or writes Jenkins global credentials.
3. `003 - Proxmox Access Setup` creates or rotates the Proxmox API token.

Run job 002 once before job 001 if its Infisical credentials have not yet been
added.

Import both Machine Identity credential pairs before running job 003. It reads
`PVE_SSH_PRIVATE_KEY`, `PVE_SSH_HOST_KEY`, and the current API token values from
`/proxmox/automation`. The supplied screenshots show the existing account
`homelab-automation@pve` and custom role `HomelabLxcOperator`; these are the
pipeline defaults. It applies that role to `/vms` and each selected storage
path, then creates or rotates an API token for the existing account. The token
inherits those user ACLs (`privsep` is disabled for the token). It then updates
and verifies `PVE_API_TOKEN_ID` and `PVE_API_TOKEN_SECRET` before removing the
previous token. If saving or verification fails, the previous values are
restored where possible and the old token stays active. Review Proxmox after a
failed run because the newly created token may need manual cleanup. The
previous token is automatically removed only when its stored ID belongs to the
configured `PROXMOX_USER`; otherwise it is left active for review.

The SSH host key secret must be an OpenSSH `known_hosts` line for the Proxmox
host (including its hostname or IP). The private key must authorize root SSH
to that host. LXC password folders under `/proxmox/lxc` are unrelated and are
not read by this pipeline.

The `001 - Update Servers` job uses the read-only Machine Identity to retrieve
only `PVE_SSH_HOST_KEY`. Its `pve01-automation-ssh` credential runs the host
bootstrap, which downloads this publicly readable repository without GitHub
credentials, reuses the controller and agent, and refreshes the controller's
project snapshot.
