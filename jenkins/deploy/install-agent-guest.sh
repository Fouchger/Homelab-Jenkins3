#!/usr/bin/env bash
# Install the Java runtime and homelab automation tools inside the Jenkins agent.
set -Eeuo pipefail
INSTALL_ROLE=agent
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/guest-common.sh"
AGENT_USER=${AGENT_USER:-jenkins-agent}
AGENT_HOME=${AGENT_HOME:-/var/lib/jenkins-agent}
[[ $AGENT_USER =~ ^[a-z_][a-z0-9_-]*$ && $AGENT_USER != root ]] || exit 2
[[ $AGENT_HOME =~ ^/(var/lib|home)/[a-zA-Z0-9_-]+$ ]] || exit 2
# Git is for Jenkins SCM checkout inside the agent; Proxmox needs no Git.
apt_install openjdk-21-jdk-headless git openssh-client openssh-server   python3 python3-venv python3-pip ansible-core jq rsync unzip zip   make shellcheck yamllint
if id "$AGENT_USER" >/dev/null 2>&1; then
  existing_home=$(getent passwd "$AGENT_USER" | cut -d: -f6)
  [[ $existing_home == "$AGENT_HOME" ]] || {
    printf 'Existing agent user has a different home; refusing to move it.\n' >&2; exit 1;
  }
else
  useradd --create-home --home-dir "$AGENT_HOME" --shell /bin/bash "$AGENT_USER"
fi
agent_group=$(id -gn "$AGENT_USER")
install -d -o "$AGENT_USER" -g "$agent_group" -m 0750 "$AGENT_HOME" "$AGENT_HOME/workspace"
install -d -o "$AGENT_USER" -g "$agent_group" -m 0700 "$AGENT_HOME/.ssh"
key_path="$AGENT_HOME/.ssh/authorized_keys"
touch "$key_path"
if [[ -n ${AGENT_SSH_PUBLIC_KEY:-} ]]; then
  key_file=$(mktemp)
  trap 'rm -f -- "$key_file"' EXIT
  printf '%s\n' "$AGENT_SSH_PUBLIC_KEY" > "$key_file"
  [[ $AGENT_SSH_PUBLIC_KEY != *$'\n'* ]] || exit 2
  ssh-keygen -l -f "$key_file" >/dev/null
  if ! grep -qxF -- "$AGENT_SSH_PUBLIC_KEY" "$key_path"; then
    # Ensure an existing file without a trailing newline is preserved safely.
    [[ ! -s $key_path ]] || printf '\n' >> "$key_path"
    printf '%s\n' "$AGENT_SSH_PUBLIC_KEY" >> "$key_path"
  fi
fi
chown "$AGENT_USER:$agent_group" "$key_path"
chmod 0600 "$key_path"
# Permit key-authenticated SSH for this locked-password service account.
install -d -m 0755 /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/60-homelab-jenkins-agent.conf <<EOF
# Jenkins agent: key authentication only.
Match User $AGENT_USER
    PubkeyAuthentication yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
Match all
EOF
install -d -m 0755 /run/sshd
/usr/sbin/sshd -t
systemctl enable ssh
systemctl restart ssh
systemctl is-active --quiet ssh
runuser -u "$AGENT_USER" -- test -w "$AGENT_HOME/workspace"
runuser -u "$AGENT_USER" -- java -version
for tool in git ssh python3 ansible ansible-playbook jq rsync unzip zip make shellcheck yamllint; do
  runuser -u "$AGENT_USER" -- /bin/bash -c 'command -v "$1" >/dev/null' bash "$tool"
done
date -Is > /var/lib/homelab/jenkins-agent-installed
printf 'Agent tools verified. SSH user: %s; remote root directory: %s.\n' "$AGENT_USER" "$AGENT_HOME"
printf 'Create the Jenkins SSH node and supply its matching SSH credential after controller setup.\n'
