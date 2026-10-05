# Jenkins pipelines

Keep Jenkins Pipeline definitions and their pipeline-only helpers under this
directory. Deployment scripts that install Jenkins remain in `../deploy/`, and
Proxmox host-side LXC creation/profile scripts remain in `../../proxmox_helper_script/`.

| File | Purpose |
| --- | --- |
| `toolchain-check.Jenkinsfile` | Periodic read-only check of the Jenkins automation agent toolchain. |
| `infisical-setup/Jenkinsfile` | Pauses while the operator adds existing Infisical credentials in Jenkins, then verifies both identities. |
| `infisical-setup/verify-machine-identities.py` | Authenticates both Universal Auth identities without printing their secrets. |
| `proxmox-access/Jenkinsfile` | Manually rotates the Proxmox API token and writes it to Infisical; job is `003 - Proxmox Access Setup`. |
| `proxmox-access/provision-proxmox-access.py` | Infisical API and SSH helper called by the Proxmox access pipeline. |
| `server-update/Jenkinsfile` | Updates the controller and agent LXCs daily at 2:00 a.m. Pacific/Auckland; job is `002 - Update Servers`. |
| `server-update/update-servers.py` | Retrieves the trusted Proxmox host key from Infisical and runs the host bootstrap over SSH. |

The controller startup hooks seed the matching jobs from these repository
paths. Update those script paths in `../deploy/install-controller-guest.sh`
when moving a pipeline file.

The server update job uses Extended Timer Trigger. Timer runs pass
`AUTOMATED_UPDATE=true` to skip the manual confirmation; ordinary manual runs
leave it false and require operator confirmation.
