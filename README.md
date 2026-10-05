# Homelab Jenkins 3

Create the controlplane and agent LXCs with the Proxmox Community Scripts,
then install their applications automatically. Run from the Proxmox host as root.
The host needs `bash`, `curl`, `pct`, `flock` and `tee`; it does not need Git.
Extract the complete ZIP on the host so that the relative hook paths exist.

## Settings

Edit `jenkins/config/install.conf` before deployment. The defaults are Jenkins
LTS on port 8080, Java 21, Pacific/Auckland, and agent user `jenkins-agent`
with home/work directory `/var/lib/jenkins-agent`.
An optional SSH public key can be supplied in `AGENT_SSH_PUBLIC_KEY`.
Never put passwords or private keys in this file.

Container resources, IDs, VLANs and MACs remain in the existing profiles.
Controlplane is CTID 100 on VLAN 20; agent is CTID 101 on VLAN 20.
Both use DHCP: reserve 192.168.20.5 and 192.168.20.6 respectively on the router
before creation. Ensure guest outbound DNS/HTTPS and Ubuntu APT access work.
The Proxmox bridge must carry VLAN 20. Existing containers are not replaced.
Ubuntu 24.04 and 26.04 are supported by the application installers; an available
Proxmox template for the chosen profile version is still required.
Community Scripts may ask host-specific questions during creation.

## Create and install

From the extracted project directory:

```bash
bash proxmox_helper_script/create-lxc.sh proxmox_helper_script/lxc/ubuntu/controlplane.profile.sh
bash proxmox_helper_script/create-lxc.sh proxmox_helper_script/lxc/ubuntu/jenkins-agent.profile.sh
```

The convenience command `bash proxmox_helper_script/controlplane.sh` uses the
same controlplane profile and installs Jenkins too.
The launcher runs the matching host hook once after successful container
creation. The hook copies the guest installer into `/opt/homelab/jenkins/install`
and runs it inside the container with `pct exec`. Installer failures return
non-zero to the launcher. The hook is deliberately run by the launcher rather
than the upstream helper, whose hook error handling can conceal failures.
Call these profile files through this launcher to get this behaviour.

## Installed applications

| Container | Applications and configuration |
| --- | --- |
| Controlplane | Jenkins LTS from its signed official APT repository; Java 21 runtime; fontconfig; CA certificates; curl; GnuPG; timezone data. Jenkins starts at boot and its HTTP endpoint is checked. |
| Agent | Java 21 JDK; Git; SSH client/server; Python 3, pip and venv; Ansible Core; jq; rsync; unzip/zip; make; ShellCheck; yamllint. Dedicated service user and writable workspace; key authentication only for that user. |

Git is installed in the agent for Jenkins repository checkout, not on Proxmox.
The agent receives neither a Jenkins controller service nor blanket sudo access.
Additional tools such as Docker, kubectl or Task depend on future pipeline needs
and are not included in this baseline.

## First Jenkins setup

Open `http://192.168.20.5:8080` after a successful install. Retrieve the initial
unlock password privately from the Proxmox console; installers never print it:

```bash
pct exec 100 -- cat /var/lib/jenkins/secrets/initialAdminPassword
```

Complete the Jenkins wizard, install suggested plugins and create your admin
account. Install the **SSH Build Agents** plugin if it is not already installed.
Set the built-in node's executors to zero so builds run on the agent.

For the SSH agent, add a Jenkins SSH username/private-key credential using
username `jenkins-agent`. Add the matching public key to `AGENT_SSH_PUBLIC_KEY`
and rerun the agent hook below. In Manage Jenkins > Nodes, create a permanent
node named `jenkins-agent`, one executor, remote root `/var/lib/jenkins-agent`,
label `jenkins-agent`, and launch via SSH to `192.168.20.6` with that credential.
Use a verified host key strategy; compare the agent key fingerprint with:

```bash
pct exec 101 -- ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Controller access to agent TCP 22 and client access to controller TCP 8080 must
be permitted by existing network/firewall policy. Application installation is
automatic; administrator creation and node/credential registration are separate
one-time Jenkins setup steps. Existing credentials, jobs and authorised keys
are preserved on installer reruns.

## Existing containers and failed-install recovery

Run these on Proxmox to install or retry without recreating either container:

```bash
bash jenkins/deploy/install-controller-lxc.sh 100
bash jenkins/deploy/install-agent-lxc.sh 101
```

Containers must already be running. A failed installation leaves the container
available for diagnosis and retry; nothing destroys it automatically.
Controller reruns preserve its installed Jenkins version unless a version is
explicitly set in the configuration. Other APT dependencies may update.
Both hooks restart their service; allow for a brief interruption when rerunning.
Normal backup and planned upgrade arrangements remain your responsibility.

## Logs and checks

Host logs: `/var/log/homelab/jenkins-controller-100.log` and
`/var/log/homelab/jenkins-agent-101.log`.
Guest logs: `/var/log/homelab/jenkins-controller.log` or `jenkins-agent.log`.
Success markers are written only after checks pass under `/var/lib/homelab/`.

```bash
pct exec 100 -- systemctl status jenkins --no-pager
pct exec 100 -- journalctl -u jenkins --no-pager -n 50
pct exec 101 -- systemctl status ssh --no-pager
pct exec 101 -- runuser -u jenkins-agent -- ansible --version
```

The launcher detects failed, empty and syntactically invalid downloads, but
Community Scripts still use upstream `main`; this update does not pin that
provisioning dependency. The Jenkins 2026 repository key fingerprint is checked
before it is trusted. A future signing-key rotation requires a reviewed update.

## References and validation

Installation follows [Jenkins Linux installation guidance](https://www.jenkins.io/doc/book/installing/linux/)
and [Jenkins agent guidance](https://www.jenkins.io/doc/book/using/using-agents/).
Repository signing key: [official Jenkins LTS repository](https://pkg.jenkins.io/debian-stable/).

Validation for this package covers Bash syntax and isolated launcher/hook
integration tests, including failure propagation. No live Proxmox containers or
Jenkins services were provisioned in the development environment.

To rerun the isolated integration tests on a Linux host as root:

```bash
python3 tests/test_bootstrap.py -v
```

The tests replace `pct` and `curl` with mocks; they do not install applications
or create containers. Host hook logs and lock files use their normal paths.
