# Homelab Jenkins 3

Create the controlplane and agent LXCs with Proxmox Community Scripts, then
install the Jenkins-compatible application stack automatically. Run from the
Proxmox host as root after extracting the complete project there.
The host needs Bash, curl, pct, flock and tee; Git is installed inside the
controller and agent, and is not required by this container launcher on Proxmox.

Jenkins pipeline definitions live under [`jenkins/pipelines`](jenkins/pipelines/).
The `proxmox_helper_script` directory is reserved for Proxmox host-side LXC
creation scripts and profiles; those scripts bootstrap containers rather than
define Jenkins jobs.

## Settings and prerequisites

Edit `jenkins/config/install.conf` before deployment. Defaults:

| Setting | Default |
| --- | --- |
| Controller / agent CTID | 100 / 101 |
| Controller URL | http://192.168.20.5:8080 |
| Runtime | Java 25 on both servers |
| Jenkins release | Current LTS; reruns may upgrade it |
| Timezone | Pacific/Auckland |
| Agent name / label | jenkins-agent / homelab-automation |
| Agent account / work directory | jenkins-agent / /var/lib/jenkins-agent |
| GitHub repository / branch | Fouchger/Homelab-Jenkins3 / main |

Container resources, MACs and VLANs remain in the profiles. Both Ubuntu
containers use VLAN 20 and DHCP. Reserve 192.168.20.5 and 192.168.20.6 for the
profile MACs before creation. The bridge must carry VLAN 20.
When changing container IDs, also update their corresponding configuration IDs.
When changing the controller address or port, update `HOMELAB_JENKINS_URL` too.
The default Ubuntu version is 26.04; 24.04 is also accepted, provided its package
repositories offer OpenJDK 25. Missing packages cause a failure rather than a
silent fallback to a different runtime.

The host needs access to the Community Scripts download. Guests need outbound
DNS/HTTPS to Ubuntu APT, Jenkins, GitHub, HashiCorp, OpenTofu, Cloudsmith, PyPI and
Ansible Galaxy. The agent connects outbound to the configured controller HTTP(S)
port over WebSocket; no inbound agent port is needed for Jenkins connectivity.
Community Scripts may prompt for host-specific provisioning choices.

## One-command bootstrap from Proxmox

Push this updated project to the configured GitHub repository's `main` branch
first. Then, in the Proxmox shell as root, run this single line:

```bash
bash -c 'set -e; bootstrap=$(curl -fsSL --retry 3 --connect-timeout 15 --max-time 120 https://raw.githubusercontent.com/Fouchger/Homelab-Jenkins3/refs/heads/main/proxmox_helper_script/controlplane.sh); test -n "$bootstrap"; bash -c "$bootstrap"'
```

The entry point downloads the entire repository using curl, extracts it into a
private temporary folder, installs the controlplane first, then installs the
agent on the same Proxmox host and enrols its WebSocket service. It removes the
host temporary project on success and failure. Failed containers remain available
for diagnosis. It does not require Git on Proxmox.

For a first build, the agent must be created by this bootstrap, rather than by
Jenkins: the controller/agent pair should be ready before it runs automation.
After setup, use Jenkins on the controlplane for ongoing jobs, executed on the
agent. Future Proxmox management jobs still require appropriate API or restricted
SSH credentials; installing Jenkins alone does not grant Proxmox permissions.

For each matching existing controlplane or agent LXC, the bootstrap asks whether
to reuse it or destroy and recreate it. Reuse reruns its application installer;
stopped matching LXCs are started. Before offering either choice, it checks the
profile tags, hostname, MAC, VLAN, and IPv4 address. Unrelated or mismatched
container IDs are rejected. Without an interactive terminal, a matching LXC is
reused by default unless an action variable explicitly requests destruction.
For non-interactive runs, set `HOMELAB_CONTROLPLANE_ACTION` and
`HOMELAB_AGENT_ACTION` to `reuse` or `destroy` before launching the bootstrap.
`HOMELAB_EXISTING_LXC_ACTION` can set one action for both. Destruction targets
only the profile-matched CTID: protection is temporarily disabled, then Proxmox
stops and destroys that exact LXC. Protection is restored if deletion fails.
This uses the targeted `pct` commands because the Community Scripts
`guest-delete.sh` tool has an interactive checklist and does not accept a CTID
argument. Destroying permanently removes the LXC and its data.
New LXCs are created by passing all profile settings to the Community Script in
generated mode, which skips its setup menus. To destroy the existing controlplane
and reuse an existing agent without prompts, prefix the one-line bootstrap with
`HOMELAB_CONTROLPLANE_ACTION=destroy HOMELAB_AGENT_ACTION=reuse`.
Reruns may upgrade Jenkins and dependencies and briefly restart services.
A copy of the downloaded project remains inside the controlplane at
`/opt/homelab/bootstrap-project`; no repository checkout remains on Proxmox.
The copy contains host hooks for reference, which must still run on Proxmox.

Defaults are read from `jenkins/config/install.conf` in the downloaded repository.
For a trusted host-specific override, set `HOMELAB_INSTALL_CONFIG` to an existing
configuration file. The source owner/repository/ref can be overridden through
`HOMELAB_BOOTSTRAP_OWNER`, `HOMELAB_BOOTSTRAP_REPOSITORY` and `HOMELAB_BOOTSTRAP_REF`.
The GitHub branch remains mutable; this entry point does not pin it to a commit.

This unauthenticated one-liner requires the raw script and repository to be
publicly readable. For a private repository, the initial raw download also needs
a GitHub credential. `HOMELAB_GITHUB_TOKEN` supplies authenticated archive access
after the entry point starts; it is written only to a protected temporary curl
configuration and is not displayed or passed as a curl command-line argument.
That download token is not automatically imported into Jenkins.

Bootstrap logs persist at `/var/log/homelab/jenkins-bootstrap.log`.
The live GitHub URL could not be verified from this development environment;
publish the supplied update before using the one-liner.

## Install from an extracted local project

From the project directory on Proxmox:

```bash
bash proxmox_helper_script/create-lxc.sh proxmox_helper_script/lxc/ubuntu/controlplane.profile.sh
bash proxmox_helper_script/create-lxc.sh proxmox_helper_script/lxc/ubuntu/jenkins-agent.profile.sh
```

`bash proxmox_helper_script/controlplane.sh` runs the complete download bootstrap
and installs both servers. The two explicit commands above install the local
project one server at a time.
Each creation runs its host installation hook once. The hook copies guest
installers to `/opt/homelab/jenkins/install` and executes them with `pct exec`.
Failures propagate to the caller. Existing container IDs are rejected rather
than overwritten; use the retry commands below for existing containers.
The launcher owns hook execution because upstream hook failures can be masked.

After each successful application installation, enrolment is attempted. It is
explicitly deferred when the other container is missing or not installed yet.
When both are ready, the host reads the controller's one-time inbound secret,
transfers it through protected temporary files, configures the agent systemd
service and removes the handoff files. It never prints the secret.
A running agent service is checked; verify that the node appears online in
Jenkins after the initial administrator setup. Live connection has not been
validated in the development environment.

## Applications per server

| Server | Installed stack |
| --- | --- |
| Controlplane | Jenkins LTS, Java 25 JRE, fontconfig, Git, jq, curl, CA certificates, GnuPG and timezone data. |
| Agent | Java 25 headless JRE, Git, SSH client, Python 3/pip/venv, jq, unzip, OpenTofu, Packer and Task. |
| Agent Ansible | `/opt/ansible` virtual environment with `ansible-core`, `proxmoxer` and `requests`; system-wide executable launchers and collections under `/usr/local/share/ansible/collections`. |
| Agent collections | `community.proxmox`, `community.routeros`, `community.general`, `kubernetes.core`. |
| Additional agent utilities | rsync, zip, make, ShellCheck and yamllint. |

The controller installs Git, Pipeline, SSH credentials, credentials binding,
Infisical and Extended Timer Trigger plugins with their dependencies. Startup
hooks create the inbound `jenkins-agent` node with one executor and exclusive
`homelab-automation` label, and set the controller's executor count to zero.
A `homelab-check` Pipeline job reads the configured GitHub repository and runs
[`jenkins/pipelines/toolchain-check.Jenkinsfile`](jenkins/pipelines/toolchain-check.Jenkinsfile), which verifies the toolchain without changing services.
The job polls Git every five minutes, as in Jenkins2. It needs the GitHub
credential below to read a private repository.
The `002 - Update Servers` job checks out the configured branch, then uses the
Proxmox host bootstrap at that exact commit to rerun the controller and agent
installers and refresh the controller's repository snapshot. It runs daily at
2:00 a.m. Pacific/Auckland. Timer runs proceed unattended; manually started runs
show the commit and prompt for confirmation. Both paths always reuse the verified
containers; the pipeline never chooses the destroy-and-recreate path. Updates
can upgrade Jenkins LTS and system/toolchain dependencies and restart services.
The agent install preserves the current connection, then schedules its service
restart five minutes after completion. Verify the agent reconnects and the
periodic `homelab-check` passes. Public GitHub access needs no GitHub credential.
The job uses the Infisical read identity and `pve01-automation-ssh` credential.

The agent uses a restricted account and a hardened WebSocket systemd service.
This application installer adds no SSH server, sudo access or Docker socket.
Container helper/profile SSH settings and pre-existing SSH installations remain
in place. On an upgrade from the previous SSH-based package, retire the old
SSH node/credential after verifying the new inbound node is online.

## First administrator and credentials

Open the configured controller URL. Retrieve the unlock password privately:

```bash
pct exec 100 -- cat /var/lib/jenkins/secrets/initialAdminPassword
```

Complete Jenkins' first administrator wizard. Required plugins and agent node
configuration are already supplied by the installer. Confirm the agent is online
and labelled `homelab-automation`. Credentials cannot be invented by the
installer: supply your Infisical identities once. A GitHub read token is only
needed if you make the repository private.

You can add credentials through Jenkins or use the protected import helper.
For the helper, create a root-owned directory with mode 0700 and place only the
needed files inside it; every file must be root-owned with mode 0600.
Do not put that directory inside this repository.

| Host file name | Credential/import |
| --- | --- |
| homelab-github-readonly.token | Optional GitHub fine-grained read-only PAT, only for a private repository; credential ID from configuration. |
| homelab-pve01-automation-key | Existing OpenSSH Ed25519 private key; `pve01-automation-ssh` (required by `002 - Update Servers`). |
| homelab-infisical-client-id and homelab-infisical-client-secret | Read-only Universal Auth pair; credential ID from configuration. |
| homelab-infisical-writer-client-id and homelab-infisical-writer-client-secret | Writer pair; `infisical-homelab-prod-writer`. |

Configure the Infisical project connection in
[`jenkins/config/install.conf`](jenkins/config/install.conf): credential ID,
Infisical URL, project ID, environment slug, and project slug. These settings
are copied to `/etc/homelab/project.properties` in the controller.
The existing project identities are `jenkins-read` (Viewer) and
`jenkins-write` (Member); they are not created by the controller installer.
See [`infisical/README.md`](infisical/README.md) for the current secret paths
and credential IDs.

```bash
bash jenkins/deploy/import-controller-credentials-lxc.sh /root/jenkins-bootstrap-secrets 100
```

The helper stages supplied files into protected guest `/run` files and restarts
Jenkins. Startup hooks store them in Jenkins' encrypted credentials store, remove
the guest handoff files and test supplied Infisical identities. The helper checks
stored credential IDs; an existing ID alone does not prove a replacement token
was valid. Confirm the GitHub job succeeds and review any startup errors before
removing your protected host source files. It deliberately preserves those
source files if you need to retry. Secret values never appear in helper output.
The public repository does not need GitHub credentials. The controller seeds
three operator jobs in order: `001 - Infisical Credential Setup`, which pauses
while you add the read/write Machine Identity credentials in Jenkins and then
verifies their logins; `002 - Update Servers`, which updates the controller and
agent; and `003 - Proxmox Access Setup`, which rotates the Proxmox API token.
For job 001, create two **Username with password** credentials under **Manage
Jenkins → Credentials → System → Global credentials**. Use the Infisical Client
ID as username and Client Secret as password:

| Credential ID | Machine Identity |
| --- | --- |
| `infisical-homelab-prod-read-api` | `jenkins-read` |
| `infisical-homelab-prod-writer-api` | `jenkins-write` |

Resume the paused build to verify both logins. The pipeline does not collect
secrets as build input or create global Jenkins credentials. Job 003 uses the
read identity to retrieve the Proxmox SSH key and host key from
`/proxmox/automation`, then uses the writer identity to rotate and verify the
Proxmox API token there. It does not read secrets under `/proxmox/lxc`. See
[`infisical/README.md`](infisical/README.md) for secret formats and rotation
behavior.

These scripts import an existing Proxmox SSH key and seed the Proxmox access
setup job. The job applies the existing `HomelabLxcOperator` role to
`homelab-automation@pve`, creates or rotates its API token, and writes the
result to Infisical. This package also aligns Jenkins installation and
enrolment and includes the toolchain verification job.
After the pipeline files are pushed to the configured GitHub branch, restart
Jenkins or rerun the controller installer to execute the startup hook that
seeds the new job.

## Existing containers and recovery

Run on Proxmox against running containers:

```bash
bash jenkins/deploy/install-controller-lxc.sh 100
bash jenkins/deploy/install-agent-lxc.sh 101
bash jenkins/deploy/enrol-agent-lxc.sh 100 101
```

The first two also attempt enrolment automatically. Use the third to retry only
the connection setup. A failed application install leaves its container available
for diagnosis; success markers are written only after checks pass.
Enrolment can restart Jenkins to regenerate its handoff secret. Controller
reruns may upgrade LTS; dependencies, plugins newly installed, pip packages and
collections use current upstream versions. Existing Jenkins jobs/credentials
are retained, but the managed agent node configuration is reapplied.
Plan backups and a brief service interruption before upgrading existing systems.

## Logs, checks and tests

Host installation logs: `/var/log/homelab/jenkins-controller-100.log` and
`/var/log/homelab/jenkins-agent-101.log`. Guest logs use the same role names
without CTID. Runtime logs are in the systemd journal.

```bash
pct exec 100 -- journalctl -u jenkins --no-pager -n 50
pct exec 101 -- systemctl status jenkins-agent --no-pager
pct exec 101 -- journalctl -u jenkins-agent --no-pager -n 50
pct exec 101 -- runuser -u jenkins-agent -- task --version
pct exec 101 -- runuser -u jenkins-agent -- ansible-galaxy collection list
python3 tests/test_bootstrap.py -v
python3 tests/test_download_bootstrap.py -v
```

Tests mock downloads and Proxmox; no containers, packages or live Jenkins
services are installed by them. Host hook logs and lock files use normal paths.
Bash syntax checks also cover the generated agent configuration helper.
No live Proxmox provisioning, plugin startup or remote service connection has
been verified here. Community Scripts still use upstream `main`; the launcher
checks download errors and syntax, but does not pin that provisioning dependency.
The Jenkins repository's 2026 signing-key fingerprint is verified before trust.

## Installation references

- [Jenkins Java support](https://www.jenkins.io/doc/book/platform-information/support-policy-java/)
- [Jenkins Linux installation](https://www.jenkins.io/doc/book/installing/linux/)
- [OpenTofu Debian installation](https://opentofu.org/docs/intro/install/deb/)
- [Packer installation](https://docs.hashicorp.com/packer/install)
- [Task installation](https://taskfile.dev/docs/installation)
