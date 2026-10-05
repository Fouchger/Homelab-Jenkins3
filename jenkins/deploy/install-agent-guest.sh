#!/usr/bin/env bash
# Install the isolated Jenkins automation-agent toolchain in an Ubuntu LXC.

set -Eeuo pipefail
umask 022

# `pct exec` can provide a reduced PATH; include the standard system locations
# so commands installed under /usr/local/bin are available during setup.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

INSTALL_ROLE=agent
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/guest-common.sh"
# Avoid changing the running agent's Java before the new runtime and toolchain
# are installed. Select Java 25 explicitly below before restarting the agent.
if command -v java >/dev/null 2>&1; then
  current_java_path="$(readlink -f "$(command -v java)")"
  update-alternatives --set java "$current_java_path"
fi
apt-get install -y ca-certificates curl git gnupg jq openssh-client openjdk-25-jre-headless \
  python3 python3-venv python3-pip unzip rsync zip make shellcheck yamllint
java25_path="$(update-alternatives --list java | awk '/java-25-openjdk/ { print; exit }')"
[[ -x "$java25_path" ]] || { echo "OpenJDK 25 is installed but its java alternative is missing." >&2; exit 1; }
update-alternatives --set java "$java25_path"
java -version

# Use upstream signed APT repositories for OpenTofu and Packer.
install -d -m 0755 /etc/apt/keyrings /usr/share/keyrings
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
  https://get.opentofu.org/opentofu.gpg -o /etc/apt/keyrings/opentofu.gpg
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
  https://packages.opentofu.org/opentofu/tofu/gpgkey \
  | gpg --no-tty --batch --dearmor --yes --output /etc/apt/keyrings/opentofu-repo.gpg
chmod a+r /etc/apt/keyrings/opentofu.gpg /etc/apt/keyrings/opentofu-repo.gpg
cat >/etc/apt/sources.list.d/opentofu.list <<'OPENTOFU_REPO'
deb [signed-by=/etc/apt/keyrings/opentofu.gpg,/etc/apt/keyrings/opentofu-repo.gpg] https://packages.opentofu.org/opentofu/tofu/any/ any main
OPENTOFU_REPO

curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
  https://apt.releases.hashicorp.com/gpg \
  | gpg --no-tty --batch --dearmor --yes --output /usr/share/keyrings/hashicorp-archive-keyring.gpg
chmod a+r /usr/share/keyrings/hashicorp-archive-keyring.gpg
ubuntu_codename="$(. /etc/os-release; printf '%s' "$UBUNTU_CODENAME")"
architecture="$(dpkg --print-architecture)"
printf 'deb [arch=%s signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com %s main\n' \
  "$architecture" "$ubuntu_codename" >/etc/apt/sources.list.d/hashicorp.list

# Use Task's maintained APT repository so the package is installed system-wide.
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
  https://dl.cloudsmith.io/public/task/task/setup.deb.sh -o /tmp/task-repository-setup.sh
bash /tmp/task-repository-setup.sh
rm -f /tmp/task-repository-setup.sh

apt-get update
apt-get install -y packer tofu task
command -v task >/dev/null 2>&1 || {
  echo "Task CLI installation failed: 'task' is not available in PATH." >&2
  exit 1
}

# Keep Ansible and its Python dependencies isolated from Ubuntu's system Python.
python3 -m venv /opt/ansible
/opt/ansible/bin/python -m pip install --upgrade pip ansible-core proxmoxer requests
chmod -R a+rX /opt/ansible
install -d -m 0755 /usr/local/bin /usr/local/share/ansible/collections /etc/ansible
for command_name in ansible ansible-config ansible-doc ansible-galaxy ansible-playbook ansible-vault; do
  [[ -x "/opt/ansible/bin/$command_name" ]] || {
    printf 'Ansible installation is incomplete: %s\n' "$command_name" >&2; exit 1;
  }
  # Real executable launchers avoid reliance on shell aliases or symlinks.
  printf '#!/usr/bin/env bash\n# Isolated Ansible command launcher.\nexec /opt/ansible/bin/%s "$@"\n' \
    "$command_name" > "/usr/local/bin/$command_name"
  chmod 0755 "/usr/local/bin/$command_name"
done
cat >/etc/ansible/ansible.cfg <<'ANSIBLE_CONFIG'
[defaults]
collections_path = /usr/local/share/ansible/collections
interpreter_python = auto_silent
ANSIBLE_CONFIG
chmod 0644 /etc/ansible/ansible.cfg
/opt/ansible/bin/ansible-galaxy collection install \
  community.proxmox community.routeros community.general kubernetes.core \
  --collections-path /usr/local/share/ansible/collections
chmod -R a+rX /usr/local/share/ansible/collections

# The restricted account connects outbound over WebSocket. This installer
# adds no SSH server, root key, Docker socket or sudo access. Existing guest
# SSH settings supplied by the container helper are left in place.
if ! id jenkins-agent >/dev/null 2>&1; then
  useradd --system --user-group --create-home --home-dir /var/lib/jenkins-agent --shell /bin/bash jenkins-agent
fi
passwd --lock jenkins-agent >/dev/null
install -d -o jenkins-agent -g jenkins-agent -m 0750 /var/lib/jenkins-agent
runuser -u jenkins-agent -- /usr/local/bin/ansible --version >/dev/null || {
  echo "The jenkins-agent account cannot execute Ansible after installation." >&2
  exit 1
}
runuser -u jenkins-agent -- /usr/local/bin/ansible-galaxy collection list >/dev/null || {
  echo "The jenkins-agent account cannot read the installed Ansible Galaxy collections." >&2
  exit 1
}
install -d -o root -g root -m 0755 /opt/jenkins-agent
install -d -o root -g root -m 0755 /usr/local/sbin

# Install a helper to store the inbound-agent secret in a root-only systemd env file.
cat >/usr/local/sbin/configure-jenkins-agent <<'CONFIGURE_AGENT'
#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this helper as root inside the agent LXC." >&2
  exit 1
fi

if [[ "${1:-}" == --secret-file && "$#" -eq 2 ]]; then
  secret_file="$2"
  [[ -r "$secret_file" ]] || { echo "Agent secret file is not readable." >&2; exit 1; }
  IFS= read -r agent_secret <"$secret_file" || {
    echo "Could not read the agent secret file." >&2
    exit 1
  }
  rm -f -- "$secret_file"
elif [[ "$#" -eq 0 ]]; then
  printf 'Paste the secret shown on the Jenkins node page (input is hidden): '
  IFS= read -r -s agent_secret
  printf '\n'
else
  echo "Usage: configure-jenkins-agent [--secret-file PATH]" >&2
  exit 2
fi
[[ "$agent_secret" =~ ^[[:alnum:]]{32,128}$ ]] || {
  echo "The agent secret must be 32–128 letters or numbers. No changes made." >&2
  exit 1
}

agent_url="${HOMELAB_JENKINS_URL:-http://192.168.20.5:8080}"
agent_url="${agent_url%/}"
[[ $agent_url =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || {
  echo 'Controller URL must be an HTTP(S) origin with an optional port.' >&2; exit 2;
}
install -d -o root -g jenkins-agent -m 0750 /etc/jenkins-agent
printf '%s\n' "$agent_secret" >/etc/jenkins-agent/agent.secret
chown root:jenkins-agent /etc/jenkins-agent/agent.secret
chmod 0640 /etc/jenkins-agent/agent.secret
rm -f -- /etc/jenkins-agent/agent.env
unset agent_secret

curl --proto '=http,https' --proto-redir '=http,https' --fail --silent --show-error --location \
  "$agent_url/jnlpJars/agent.jar" --output /opt/jenkins-agent/agent.jar
chown root:root /opt/jenkins-agent/agent.jar
chmod 0644 /opt/jenkins-agent/agent.jar

cat >/etc/systemd/system/jenkins-agent.service <<'AGENT_UNIT'
[Unit]
Description=Jenkins automation agent
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=jenkins-agent
Group=jenkins-agent
WorkingDirectory=/var/lib/jenkins-agent
Environment=HOME=/var/lib/jenkins-agent
Environment=ANSIBLE_CONFIG=/etc/ansible/ansible.cfg
Environment=JENKINS_URL=http://192.168.20.5:8080/
Environment=JENKINS_AGENT_NAME=jenkins-agent
Environment=JENKINS_AGENT_WORKDIR=/var/lib/jenkins-agent
ExecStart=__JAVA25__ -jar /opt/jenkins-agent/agent.jar -url $JENKINS_URL -secret @/etc/jenkins-agent/agent.secret -name $JENKINS_AGENT_NAME -webSocket -workDir $JENKINS_AGENT_WORKDIR
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ReadWritePaths=/var/lib/jenkins-agent /tmp

[Install]
WantedBy=multi-user.target
AGENT_UNIT
sed -i "s|^Environment=JENKINS_URL=.*|Environment=JENKINS_URL=${agent_url}/|" /etc/systemd/system/jenkins-agent.service
java25_path="$(update-alternatives --list java | awk '/java-25-openjdk/ { print; exit }')"
[[ -x $java25_path ]] || { echo 'Java 25 runtime was not found.' >&2; exit 1; }
sed -i "s|__JAVA25__|$java25_path|" /etc/systemd/system/jenkins-agent.service
chmod 0644 /etc/systemd/system/jenkins-agent.service
systemctl daemon-reload
systemctl enable jenkins-agent.service
systemctl restart jenkins-agent.service
systemctl is-active --quiet jenkins-agent.service || {
  systemctl --no-pager --full status jenkins-agent.service
  exit 1
}
echo "Jenkins automation agent service is active."
CONFIGURE_AGENT
chmod 0755 /usr/local/sbin/configure-jenkins-agent

printf '\nInstalled tool versions:\n'
java -version 2>&1 | head -n 1
git --version
tofu version
packer version
task --version
/opt/ansible/bin/ansible --version | head -n 2
printf '\nAutomation agent toolchain is ready for controller registration.\n'

for tool in java git tofu packer task ansible ansible-config ansible-galaxy ansible-playbook \
  ssh python3 jq rsync zip unzip make shellcheck yamllint; do
  runuser -u jenkins-agent -- /bin/bash -c 'command -v "$1" >/dev/null' bash "$tool"
done
/opt/ansible/bin/python -c 'import proxmoxer, requests'
for collection in community.proxmox community.routeros community.general kubernetes.core; do
  runuser -u jenkins-agent -- /opt/ansible/bin/ansible-galaxy collection list "$collection" \
    | grep -F "$collection" >/dev/null
done
install -d -o jenkins-agent -g jenkins-agent -m 0750 /var/lib/jenkins-agent/workspace
date -Is > /var/lib/homelab/jenkins-agent-installed
