# Homelab Jenkins 3

Create the controlplane and agent LXCs with Proxmox Community Scripts, then
install the Jenkins2-compatible application stack automatically. Run from the
Proxmox host as root after extracting the complete project there.
The host needs Bash, curl, pct, flock and tee; Git is installed inside the
controller and agent, and is not required by this container launcher on Proxmox.

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

## Create and install

From the project directory on Proxmox:

```bash
bash proxmox_helper_script/create-lxc.sh proxmox_helper_script/lxc/ubuntu/controlplane.profile.sh
bash proxmox_helper_script/create-lxc.sh proxmox_helper_script/lxc/ubuntu/jenkins-agent.profile.sh
```

`bash proxmox_helper_script/controlplane.sh` uses the same controlplane profile.
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

The controller installs Git, Pipeline, SSH credentials, credentials binding and
Infisical plugins with their dependencies. Startup hooks create the inbound
`jenkins-agent` node with one executor and exclusive `homelab-automation` label,
and set the controller's executor count to zero.
A `homelab-check` Pipeline job reads the configured GitHub repository and runs
its root `Jenkinsfile`, which verifies the toolchain without changing services.
The job polls Git every five minutes, as in Jenkins2. It needs the GitHub
credential below to read a private repository.

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
installer: supply your GitHub/Infisical identities once.

You can add credentials through Jenkins or use the protected import helper.
For the helper, create a root-owned directory with mode 0700 and place only the
needed files inside it; every file must be root-owned with mode 0600.
Do not put that directory inside this repository.

| Host file name | Credential/import |
| --- | --- |
| homelab-github-readonly.token | GitHub fine-grained read-only PAT; credential ID from configuration. |
| homelab-pve01-automation-key | Optional existing OpenSSH Ed25519 private key; `pve01-automation-ssh`. |
| homelab-infisical-client-id and homelab-infisical-client-secret | Read-only Universal Auth pair; credential ID from configuration. |
| homelab-infisical-writer-client-id and homelab-infisical-writer-client-secret | Optional writer pair; `infisical-homelab-prod-writer`. |

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
The private repository requires Contents read permission.

These scripts import an existing Proxmox SSH key; they do not create the restricted
Proxmox runner, API identity or DNS automation from Jenkins2. This package aligns
Jenkins installation and enrolment, and includes the toolchain verification job.

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
