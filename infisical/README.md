# Infisical settings for Jenkins

The values in `jenkins/config/install.conf` are bootstrap defaults. Job 002
collects all five settings and saves the active values in Jenkins credentials.
Do not add Client Secrets, API tokens, or the real project UUID to this public
repository.

```bash
HOMELAB_INFISICAL_CREDENTIAL_ID="infisical-homelab-prod"
HOMELAB_INFISICAL_URL="https://app.infisical.com"
HOMELAB_INFISICAL_ENVIRONMENT="prod"
HOMELAB_INFISICAL_PROJECT_SLUG=""
```

Run `002 - Infisical Credential Setup` and provide all five settings shown
above, plus the existing `jenkins-read` and `jenkins-write` Machine Identity
Client IDs and Client Secrets. The pipeline uses the credential ID for the
read-only Universal Auth credential and saves the URL, project UUID,
environment, and project slug as Jenkins Secret text credentials. Jobs 001 and
003 bind those saved settings when they access Infisical. The project UUID and
Client Secrets use non-stored password parameters and are not printed.

Fake UUID for documentation examples only:

```text
00000000-0000-4000-8000-000000000001
```

The existing project Machine Identities are `jenkins-read` (Viewer) and
`jenkins-write` (Member). The read credential ID is set by
`HOMELAB_INFISICAL_CREDENTIAL_ID` (default `infisical-homelab-prod`); the writer
credential ID is `infisical-homelab-prod-writer`. The API-form credentials are
`infisical-homelab-prod-read-api` and
`infisical-homelab-prod-writer-api`.

The current secret tree is organized under `/proxmox`:

| Infisical path | Secret names |
| --- | --- |
| `/proxmox/automation` | `PVE_API_TOKEN_ID`, `PVE_API_TOKEN_SECRET`, `PVE_SSH_HOST_KEY`, `PVE_SSH_PRIVATE_KEY` |
| `/proxmox/pve01` | `PROXMOX_ROOT_PASSWORD` |
| `/proxmox/lxc/controlplane`, `/proxmox/lxc/jenkins-agent`, and other LXC folders | `LXC_ROOT_PASSWORD` only |

The installer does not create Machine Identities; the two project identities
already exist. The project slug remains configurable because it is not visible
in the provided project URL.

## Proxmox API access setup

The controller seeds three operator jobs:

1. `001 - Update Servers` updates both Jenkins LXCs daily and uses the saved
   Infisical connection settings to retrieve the trusted Proxmox SSH host key.
2. `002 - Infisical Credential Setup` accepts all five settings and both
   Machine Identity credential pairs, saves them into Jenkins, and verifies
   both Universal Auth logins.
3. `003 - Proxmox Access Setup` creates or rotates the Proxmox API token using
   the saved Infisical connection settings and Machine Identity credentials.

Run job 002 before jobs 001 and 003 when setting up or rotating these values.
Job 003 reads `PVE_SSH_PRIVATE_KEY`, `PVE_SSH_HOST_KEY`, and the current API
token values from `/proxmox/automation`. The Proxmox account and role are
pipeline settings. It applies that role to `/vms` and each selected storage
path, then creates or rotates an API token for the existing account. The token
inherits those user ACLs (`privsep` is disabled for the token). It then updates
and verifies `PVE_API_TOKEN_ID` and `PVE_API_TOKEN_SECRET` before removing the
previous token. If saving or verification fails, the previous values are
restored where possible and the old token stays active. Review Proxmox after a
failed run because the newly created token may need manual cleanup. The previous
token is automatically removed only when its stored ID belongs to the configured
`PROXMOX_USER`; otherwise it is left active for review.

The SSH host key secret must be an OpenSSH `known_hosts` line for the Proxmox
host (including its hostname or IP). The private key must authorize root SSH to
that host. LXC password folders under `/proxmox/lxc` are unrelated and are not
read by this pipeline.

Job 001's `pve01-automation-ssh` credential runs the host bootstrap, which
downloads this publicly readable repository without GitHub credentials, reuses
the controller and agent, and refreshes the controller's project snapshot.
