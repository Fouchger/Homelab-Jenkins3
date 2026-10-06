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

The Proxmox access pipeline uses the read identity to retrieve the SSH key,
trusted host key, and existing API token values. It uses the writer identity to
save and verify rotated API token values. It does not read LXC password folders.

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
